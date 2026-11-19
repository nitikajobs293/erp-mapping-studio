"""SQL generation from a JoinPlan: Snowflake-first, transpiled with SQLGlot to other dialects.

Every query carries a provenance header and per-join comments; fan-out joins are de-duplicated with
QUALIFY/ROW_NUMBER (or a sub-query on dialects without QUALIFY).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import sqlglot
from sqlalchemy.orm import Session

from erpdm.constants import SQL_DIALECTS
from erpdm.db.models import ERPSystem, ERPTable
from erpdm.schemas import GeneratedSQL, JoinPlan, SelectedColumn
from erpdm.services.metadata import get_column, get_table

QUALIFY_DIALECTS = {"snowflake", "databricks"}


class SQLGenError(Exception):
    pass


def snake(s: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", s or "").strip("_").lower()
    return re.sub(r"_+", "_", s) or "col"


def column_alias(table: ERPTable, column: str, used: set[str]) -> str:
    col = get_column(table, column)
    base = snake(col.business_name) if col and col.business_name else snake(f"{table.name}_{column}")
    alias, i = base, 2
    while alias in used:
        alias, i = f"{base}_{i}", i + 1
    used.add(alias)
    return alias


SAP_TEXT_TYPES = {"CHAR", "LANG", "CUKY", "UNIT", "CLNT", "VARCHAR", "VARCHAR2", "NVARCHAR", "STRING", "TEXT"}


def null_expr(expr: str, data_type: str | None, table_class: str) -> tuple[str, str | None]:
    """Return (expression, null-handling note). Only descriptive text gets a default — keys stay NULL."""
    if table_class == "text" and (data_type or "").upper() in SAP_TEXT_TYPES:
        return f"COALESCE({expr}, '')", "Text lookup may be missing in the requested language → empty string."
    return expr, None


def generate_sql(session: Session, erp: ERPSystem, plan: JoinPlan, columns: list[SelectedColumn],
                 dialect_label: str = "Snowflake", filters: list[str] | None = None,
                 raw_database: str | None = None, raw_schema: str | None = None,
                 apply_default_filters: bool = True, exclude_soft_deleted: bool = False,
                 limit: int | None = None) -> GeneratedSQL:
    dialect = SQL_DIALECTS.get(dialect_label, dialect_label)
    if dialect not in SQL_DIALECTS.values():
        raise SQLGenError(f"Unsupported dialect {dialect_label}")
    if not columns:
        raise SQLGenError("Select at least one column.")
    tables = {t: get_table(session, erp.id, t) for t in plan.tables}
    for c in columns:
        if c.table.upper() not in {t.upper() for t in plan.tables}:
            raise SQLGenError(f"Column {c.table}.{c.column} belongs to a table that is not in the join plan.")
        if get_column(tables[_canon(plan, c.table)], c.column) is None:
            raise SQLGenError(f"Column {c.table}.{c.column} does not exist in the repository.")

    def ref(t: str) -> str:
        parts = [p for p in (raw_database, raw_schema) if p]
        return ".".join(parts + [t])

    used: set[str] = set()
    select_lines, col_map, null_notes = [], [], []
    for c in columns:
        tname = _canon(plan, c.table)
        tbl = tables[tname]
        col = get_column(tbl, c.column)
        alias = snake(c.alias) if c.alias else column_alias(tbl, col.name, used)
        used.add(alias)
        expr, note = null_expr(f"{plan.aliases[tname]}.{col.name}", col.data_type, tbl.table_class)
        if note:
            null_notes.append(f"{alias}: {note}")
        select_lines.append(f"    {expr} AS {alias}")
        col_map.append({"business_column": c.business_name or col.business_name or alias, "target_column": alias,
                        "source_table": tname, "source_column": col.name, "data_type": col.data_type,
                        "description": col.description, "validation_status": col.validation_status})

    join_lines, join_expl = [], []
    for e in plan.edges:
        join_lines.append(f"-- {e.relationship_type} | {e.cardinality} | source={e.source_type} "
                          f"| confidence={e.confidence:.2f} | {e.validation_status}")
        join_lines.append(f"{e.join_type} JOIN {ref(e.right_table)} AS {e.right_alias}\n    ON {e.condition_sql}")
        why = "INNER: the related row is mandatory" if e.join_type == "INNER" else \
              "LEFT: keeps rows even when the related record is missing"
        join_expl.append(
            f"**{e.left_table} → {e.right_table}** ({e.join_type}, {e.cardinality}, {e.relationship_type}): "
            f"{e.description or ''} Purpose: {e.business_purpose or 'n/a'}. Condition `{e.condition_sql}`. {why}. "
            f"Provenance: {e.source_type}, confidence {e.confidence:.2f}, {e.validation_status}."
            + (f" ⚠ May multiply rows (unconstrained key {e.uncovered_keys})." if e.fans_out else ""))

    where, filter_expl = [], []
    if apply_default_filters:
        for t in plan.tables:
            tbl = tables[t]
            if tbl.default_filter:
                cond = tbl.default_filter.replace("{alias}", plan.aliases[t])
                is_root = t == plan.root_table
                if is_root:
                    where.append(cond)
                    filter_expl.append(f"`{cond}` — default filter of {t}: {tbl.notes or 'see table notes'}")
                # non-root default filters belong in the ON clause to keep LEFT joins intact
                else:
                    for i, line in enumerate(join_lines):
                        if f" AS {plan.aliases[t]}\n" in line and cond not in line:
                            join_lines[i] = line + f"\n   AND {cond}"
                            filter_expl.append(f"`{cond}` — applied in the ON clause of {t} so the LEFT JOIN is preserved.")
    for e in plan.edges:
        if "'" in e.condition_sql:
            literals = [p for p in e.condition_sql.split(" AND ") if "'" in p]
            filter_expl.append(f"Join {e.right_table}: constant predicate(s) {literals} restrict the related rows "
                               "(e.g. language key, validity date or document category).")
    if exclude_soft_deleted:
        for t in plan.tables:
            tbl = tables[t]
            if tbl.soft_delete_rule:
                rule = tbl.soft_delete_rule.replace("{alias}", plan.aliases[t])
                if "JEST" in rule and "raw" not in rule.lower():
                    rule = rule.replace("FROM JEST", f"FROM {ref('JEST')}")
                cond = f"NOT ({rule})" if t == plan.root_table else None
                if cond:
                    where.append(cond)
                    filter_expl.append(f"`{cond}` — excludes logically deleted {t} records.")
    for f in filters or []:
        f = f.strip()
        if f:
            _validate_filter(f)
            where.append(f)
            filter_expl.append(f"`{f}` — user filter.")

    # Duplicate handling
    dup_notes, partition = [], []
    root_tbl = tables[plan.root_table]
    root_pk = [f"{plan.aliases[plan.root_table]}.{k}" for k in root_tbl.primary_key]
    fanouts = [e for e in plan.edges if e.fans_out]
    if fanouts:
        partition = root_pk  # grain = root key; fan-outs are collapsed to one row
        order_cols = []
        for e in fanouts:
            for k in e.uncovered_keys:
                order_cols.append(f"{e.right_alias}.{k} DESC")
        dup_notes.append(f"Fan-out joins {[e.right_table for e in fanouts]} could duplicate the {plan.root_table} grain "
                         f"({', '.join(root_tbl.primary_key)}). One row per grain is kept with ROW_NUMBER() ordered by "
                         f"{', '.join(order_cols)} — review whether you instead want a lower grain.")
        order_by = ", ".join(order_cols) or "1"
    else:
        dup_notes.append(f"All joins are N:1/1:1 on full keys (or filtered to one row), so the result keeps the "
                         f"grain of {plan.root_table} ({', '.join(root_tbl.primary_key)}). No de-duplication needed.")
        order_by = "1"

    header = [
        f"-- Generated by ERP Data Modelling Intelligence Platform · {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
        f"-- ERP: {erp.name} ({erp.code}) · dialect: {dialect_label} · root/grain: {plan.root_table}",
        f"-- Lowest join confidence: {plan.min_confidence:.2f}. Validate any join not marked 'verified' before production use.",
    ]
    for w in plan.warnings:
        header.append(f"-- WARNING: {w}")
    body = "SELECT\n" + ",\n".join(select_lines) + f"\nFROM {ref(plan.root_table)} AS {plan.aliases[plan.root_table]}\n"
    body += "\n".join(join_lines)
    if where:
        body += "\nWHERE " + "\n  AND ".join(where)
    if partition:
        if dialect in QUALIFY_DIALECTS:
            body += f"\nQUALIFY ROW_NUMBER() OVER (PARTITION BY {', '.join(partition)} ORDER BY {order_by}) = 1"
        else:
            sel_aliases = [l.rsplit(" AS ", 1)[1] for l in select_lines]
            inner = body.replace("SELECT\n", "SELECT\n    ROW_NUMBER() OVER (PARTITION BY "
                                 f"{', '.join(partition)} ORDER BY {order_by}) AS _rn,\n", 1)
            body = f"SELECT {', '.join(sel_aliases)}\nFROM (\n{inner}\n) AS dedup\nWHERE _rn = 1"
    if limit:
        body += f"\nLIMIT {int(limit)}"
    snowflake_sql = body
    final_sql = snowflake_sql
    if dialect != "snowflake":
        final_sql = transpile(snowflake_sql, "snowflake", dialect)
    sql = "\n".join(header) + "\n" + final_sql + ("" if final_sql.rstrip().endswith(";") else ";")
    parse_check(sql, dialect)

    perf = performance_notes(erp, plan, tables, dialect)
    vq = validation_queries(erp, plan, tables, columns, ref, dialect)
    nulls = null_notes + [
        "Key and code columns are passed through unchanged (NULL means the LEFT-joined record does not exist).",
    ]
    if erp.code.startswith("SAP"):
        nulls.append("SAP stores 'empty' CHAR values as spaces/blank and empty DATS as '00000000' — staging models "
                     "convert these to NULL (see dbt generator).")
    return GeneratedSQL(dialect=dialect_label, sql=sql, join_explanations=join_expl,
                        filter_explanations=filter_expl or ["No filters applied."],
                        duplicate_handling=dup_notes, null_handling=nulls, performance_notes=perf,
                        validation_queries=vq, column_mapping=col_map, warnings=plan.warnings)


def _canon(plan: JoinPlan, name: str) -> str:
    return next(t for t in plan.tables if t.upper() == name.upper())


_FORBIDDEN = re.compile(r";|--|/\*|\b(DROP|DELETE|INSERT|UPDATE|MERGE|ALTER|CREATE|GRANT|TRUNCATE|CALL|EXEC)\b", re.I)


def _validate_filter(f: str) -> None:
    if _FORBIDDEN.search(f):
        raise SQLGenError(f"Filter rejected (statements/comments not allowed): {f}")
    try:
        sqlglot.parse_one(f"SELECT 1 FROM x WHERE {f}", read="snowflake")
    except sqlglot.errors.ParseError as e:
        raise SQLGenError(f"Filter is not valid SQL: {f} ({e})") from e


def transpile(sql: str, read: str, write: str) -> str:
    out = sqlglot.transpile(sql, read=read, write=write, pretty=True, comments=True)
    return ";\n".join(out)


def parse_check(sql: str, dialect: str) -> None:
    try:
        sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.ParseError as e:  # pragma: no cover - would be a generator bug
        raise SQLGenError(f"Generated SQL failed {dialect} parse check: {e}") from e


def performance_notes(erp: ERPSystem, plan: JoinPlan, tables: dict, dialect: str) -> list[str]:
    notes = []
    if erp.has_client_column:
        notes.append(f"Every join includes {erp.client_column}; if RAW holds a single client, filter it once on the root "
                     f"table (e.g. `{plan.aliases[plan.root_table]}.{erp.client_column} = '100'`) to help pruning.")
    big = [t for t, tb in tables.items() if tb.table_class in ("transaction", "document_flow", "status")]
    if big:
        notes.append(f"Large transactional tables {big}: filter by date/document range early and ensure the RAW "
                     "tables are clustered on their key or load date.")
    if dialect == "snowflake":
        notes.append("Snowflake: consider CLUSTER BY on the root key for very large targets; avoid SELECT * on RAW; "
                     "text/lookup joins are small and broadcast well.")
    if any(e.fans_out for e in plan.edges):
        notes.append("De-duplication via window function adds a sort; pre-aggregate 1:N tables (e.g. status) in an "
                     "intermediate model when possible.")
    if any(e.relationship_type == "status" for e in plan.edges):
        notes.append("Status tables (e.g. JEST) are one row per status — aggregate them (BOOLOR_AGG / LISTAGG) "
                     "before joining instead of joining row-level.")
    return notes


def validation_queries(erp: ERPSystem, plan: JoinPlan, tables: dict, columns: list[SelectedColumn], ref,
                       dialect: str) -> dict[str, str]:
    root = plan.root_table
    ra = plan.aliases[root]
    pk = tables[root].primary_key
    q: dict[str, str] = {}
    q["Row count of root table"] = f"SELECT COUNT(*) AS row_count FROM {ref(root)};"
    if pk:
        q[f"Primary key uniqueness of {root}"] = (
            f"SELECT {', '.join(pk)}, COUNT(*) AS cnt FROM {ref(root)}\nGROUP BY {', '.join(pk)}\nHAVING COUNT(*) > 1;")
    for e in plan.edges:
        if e.join_type == "LEFT" and e.join_columns:
            cond = e.condition_sql
            first = e.join_columns[0]["right"]
            q[f"Orphans: {e.left_table} rows without {e.right_table}"] = (
                f"SELECT COUNT(*) AS orphan_rows\nFROM {ref(e.left_table)} AS {e.left_alias}\n"
                f"LEFT JOIN {ref(e.right_table)} AS {e.right_alias}\n    ON {cond}\n"
                f"WHERE {e.right_alias}.{first} IS NULL;")
        if e.fans_out:
            keys = [f"{e.right_alias}.{jc['right']}" for jc in e.join_columns]
            q[f"Fan-out check: {e.right_table} rows per {e.left_table} key"] = (
                f"SELECT {', '.join(keys)}, COUNT(*) AS rows_per_key\nFROM {ref(e.right_table)} AS {e.right_alias}\n"
                f"GROUP BY {', '.join(keys)}\nHAVING COUNT(*) > 1\nORDER BY rows_per_key DESC\nLIMIT 50;")
    if pk:
        keys = ", ".join(f"{ra}.{k}" for k in pk)
        q["Result grain check"] = (
            f"-- Expect zero rows: the generated query must return one row per {root} key.\n"
            f"SELECT {keys}, COUNT(*) AS cnt\nFROM {ref(root)} AS {ra}\n"
            + "\n".join(f"{e.join_type} JOIN {ref(e.right_table)} AS {e.right_alias} ON {e.condition_sql}"
                        for e in plan.edges if not e.fans_out)
            + f"\nGROUP BY {keys}\nHAVING COUNT(*) > 1;")
    if dialect != "snowflake":
        q = {k: transpile(v, "snowflake", dialect) if not v.startswith("--") else v for k, v in q.items()}
    return q


def default_columns_for(session: Session, erp: ERPSystem, table_names: list[str], max_per_table: int = 8) -> list[SelectedColumn]:
    """Sensible starter selection: business keys + columns with business names."""
    out = []
    client = (erp.client_column or "").upper()
    for name in table_names:
        t = get_table(session, erp.id, name)
        if t is None:
            continue
        picked = [c for c in t.columns if c.is_pk and c.name.upper() not in (client, "SPRAS")
                  and t.table_class not in ("text", "config")]
        picked += [c for c in t.columns if c.business_name and c not in picked and not c.is_pk]
        for c in picked[:max_per_table]:
            out.append(SelectedColumn(table=t.name, column=c.name, business_name=c.business_name))
    return out
