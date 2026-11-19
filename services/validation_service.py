"""SQL syntax validation (sqlglot, Snowflake dialect) and data-quality / reconciliation query generation."""
from __future__ import annotations

import re

from services.erp_knowledge_service import ERPKnowledge
from services.mapping_engine import MappingResult
from services.sql_service import SQLBuilder
from services.transform_rules import quote_ident


def validate_sql(sql: str) -> list[dict]:
    """Parse each statement with sqlglot (Snowflake). Returns a list of findings."""
    findings = []
    try:
        import sqlglot
        from sqlglot.errors import ParseError
    except ImportError:
        return [{"level": "info", "message": "sqlglot not installed - syntax check skipped"}]
    if "{{" in sql or "{%" in sql:
        sql = re.sub(r"\{\{\s*config\(.*?\)\s*\}\}", "", sql, flags=re.S)
        sql = re.sub(r"\{\{.*?\}\}", "dbt_ref", sql, flags=re.S)
        sql = re.sub(r"\{%.*?%\}", "", sql, flags=re.S)
        findings.append({"level": "info", "message": "dbt Jinja replaced by placeholders for the syntax check"})
    check_sql = sql.replace("$", "_var_")
    try:
        stmts = sqlglot.parse(check_sql, read="snowflake")
        kinds = [type(s).__name__ for s in stmts if s is not None]
        findings.append({"level": "ok", "message": f"Parsed {len(kinds)} statement(s) with the Snowflake dialect: {', '.join(kinds)}"})
        if "Command" in kinds:
            findings.append({"level": "info", "message": "Some statements (e.g. CREATE VIEW ... COMMENT / SET) are "
                             "parsed as generic commands - syntax is standard Snowflake"})
    except ParseError as exc:
        findings.append({"level": "error", "message": f"Parse error: {str(exc)[:400]}"})
    upper = sql.upper()
    if re.search(r"SELECT\s+DISTINCT", upper):
        findings.append({"level": "warning", "message": "SELECT DISTINCT found - make sure it is not hiding fan-out duplicates"})
    if "SELECT *" in upper and "QUALIFY" not in upper and "FROM (" not in upper:
        findings.append({"level": "warning", "message": "SELECT * in final query - prefer explicit columns"})
    if "FAN-OUT" in upper:
        findings.append({"level": "warning", "message": "Query contains 1:N joins that change the grain (see FAN-OUT comments)"})
    if re.search(r"NULL AS \w+\s+--", sql):
        findings.append({"level": "warning", "message": "Some requested columns have no confirmed source and are emitted as NULL"})
    return findings


def dq_queries(result: MappingResult, erp: ERPKnowledge, settings: dict | None = None) -> dict[str, str]:
    b = SQLBuilder(result, erp, settings)
    a = result.aliases
    out: dict[str, str] = {}
    cli = b.client_col
    client_where = (f"WHERE {quote_ident(cli)} = '{b.s.get('sap_client')}'"
                    if cli and b.s.get("apply_client_filter") else "")

    # 1. primary key uniqueness per source table (after excluding soft deletes)
    parts = []
    for t in b.staged_tables():
        pk = b.pk(t)
        if not pk:
            parts.append(f"-- {t}: primary key unknown - cannot test uniqueness (validate key first)")
            continue
        keys = ", ".join(quote_ident(p) for p in pk)
        parts.append(f"-- {t}: expect 0 rows (raw replication duplicates are expected before staging dedup)\n"
                     f"SELECT '{t}' AS TABLE_NAME, {keys}, COUNT(*) AS ROW_COUNT\nFROM {b.physical(t)}\n"
                     f"{client_where}\nGROUP BY {keys}\nHAVING COUNT(*) > 1\nLIMIT 100;")
    out["1. Primary-key uniqueness"] = "\n\n".join(parts)

    # 2. null checks on business key / mapped key columns
    anchor_pk = [p for p in b.pk(result.anchor) if p != cli]
    checks = [f"SUM(IFF({quote_ident(p)} IS NULL OR TRIM({quote_ident(p)}::VARCHAR) = '', 1, 0)) AS NULL_{p.upper()}"
              for p in anchor_pk]
    if checks:
        out["2. Null / blank key columns"] = (f"SELECT COUNT(*) AS TOTAL_ROWS,\n    " + ",\n    ".join(checks) +
                                             f"\nFROM {b.physical(result.anchor)}\n{client_where};")

    # 3. orphan checks for N:1 joins (child rows whose parent is missing)
    parts = []
    for j in result.joins:
        if j["cardinality"] not in ("N:1", "1:1") or erp.table(j["right"]).get("virtual") or erp.table(j["left"]).get("virtual"):
            continue
        conds = " AND ".join(f"c.{quote_ident(x)} = p.{quote_ident(y)}" for x, y in j["conditions"])
        if cli and b._has_client(j["left"]) and b._has_client(j["right"]):
            conds = f"c.{quote_ident(cli)} = p.{quote_ident(cli)} AND " + conds
        nonnull = " AND ".join(f"NULLIF(TRIM(c.{quote_ident(x)}::VARCHAR), '') IS NOT NULL" for x, _ in j["conditions"])
        parts.append(f"-- Orphans: {j['left']} rows without matching {j['right']} (expect 0 for INNER-join candidates)\n"
                     f"SELECT COUNT(*) AS ORPHAN_ROWS\nFROM {b.physical(j['left'])} c\n"
                     f"WHERE {nonnull}\n  AND NOT EXISTS (SELECT 1 FROM {b.physical(j['right'])} p WHERE {conds});")
    if parts:
        out["3. Referential integrity (orphans)"] = "\n\n".join(parts)

    # 4. grain / fan-out check
    keys = ", ".join(f"{re.sub(r'[^A-Z0-9]', '_', result.anchor.upper())}_{p.upper()}" for p in anchor_pk)
    if keys:
        out["4. Grain check on final dataset"] = (
            "-- Expect 0 rows if the result keeps the anchor grain; rows returned = fan-out from 1:N joins\n"
            f"WITH ds AS (\n"
            f"    SELECT * FROM {b.s.get('target_database')}.{b.s.get('target_schema')}.{b.view_name()}\n)\n"
            f"SELECT {keys}, COUNT(*) AS ROWS_PER_KEY\nFROM ds\nGROUP BY {keys}\nHAVING COUNT(*) > 1\nLIMIT 100;")

    # 5. reconciliation: row counts and quantity totals source vs target
    qty_rows = [r for r in result.rows if r.get("_sql") and str(r.get("Target Data Type", "")).startswith("NUMBER")
                and len(r.get("_tables", [])) == 1 and r["_tables"][0] == result.anchor and r.get("Data Type") in ("QUAN", "CURR", "NUMBER", "DECIMAL")]
    src_sums = ", ".join(f"SUM({quote_ident(r['Source Field'])}) AS {r['Target Snowflake Column']}" for r in qty_rows)
    tgt_sums = ", ".join(f"SUM({r['Target Snowflake Column']}) AS {r['Target Snowflake Column']}" for r in qty_rows)
    del_filter = (f"COALESCE({quote_ident(b.cdc_del)}, FALSE) = FALSE" if b.cdc_del else "1 = 1")
    cw = client_where.replace("WHERE ", "") if client_where else "1 = 1"
    out["5. Source-to-target reconciliation"] = (
        "WITH src AS (\n"
        f"    SELECT COUNT(*) AS ROW_COUNT{', ' + src_sums if src_sums else ''}\n"
        f"    FROM (SELECT * FROM {b.physical(result.anchor)} WHERE {del_filter} AND {cw}\n"
        f"          QUALIFY ROW_NUMBER() OVER (PARTITION BY {', '.join(quote_ident(p) for p in b.pk(result.anchor)) or '1'} "
        f"ORDER BY {quote_ident(b.cdc) if b.cdc else '1'} DESC) = 1)\n),\n"
        f"tgt AS (\n    SELECT COUNT(*) AS ROW_COUNT{', ' + tgt_sums if tgt_sums else ''}\n"
        f"    FROM {b.s.get('target_database')}.{b.s.get('target_schema')}.{b.view_name()}\n)\n"
        "SELECT 'SOURCE' AS SIDE, * FROM src\nUNION ALL\nSELECT 'TARGET', * FROM tgt;")

    # 6. date conversion failures
    date_rows = [r for r in result.rows if r.get("_sql") and r.get("Data Type") in ("DATS", "JULIAN", "YYYYMMDD_NUM")]
    if date_rows:
        parts = []
        for r in date_rows:
            t = r["_tables"][0]
            f = quote_ident(r["Source Field"])
            if r["Data Type"] == "DATS":
                cond = f"NULLIF(NULLIF(TRIM({f}), ''), '00000000') IS NOT NULL AND TRY_TO_DATE({f}, 'YYYYMMDD') IS NULL"
            elif r["Data Type"] == "JULIAN":
                cond = f"{f} <> 0 AND (MOD({f}, 1000) NOT BETWEEN 1 AND 366)"
            else:
                cond = f"{f} <> 0 AND TRY_TO_DATE(TO_VARCHAR({f}), 'YYYYMMDD') IS NULL"
            parts.append(f"SELECT '{t}.{r['Source Field']}' AS COLUMN_NAME, COUNT(*) AS INVALID_DATES\n"
                         f"FROM {b.physical(t)}\nWHERE {cond}")
        out["6. Invalid date values"] = "\nUNION ALL\n".join(parts) + ";"

    # 7. freshness
    if b.cdc:
        out["7. Freshness (latest replicated change)"] = "\nUNION ALL\n".join(
            f"SELECT '{t}' AS TABLE_NAME, MAX({quote_ident(b.cdc)}) AS LAST_SYNC, "
            f"DATEDIFF('minute', MAX({quote_ident(b.cdc)}), CURRENT_TIMESTAMP()) AS MINUTES_BEHIND FROM {b.physical(t)}"
            for t in b.staged_tables()) + ";"
    return out
