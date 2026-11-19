"""Snowflake SQL generation.

Produces, per project:
  source_extraction  - one profiling SELECT per source table (only needed columns)
  integrated         - CTE pipeline: extraction -> fan-out control -> joins + transformations
  deduplication      - integrated + ROW_NUMBER() over the grain (or SCD2-style history)
  create_view        - CREATE OR REPLACE VIEW over the deduplicated query
  data_quality       - null / duplicate / RI / reconciliation / type / business-rule checks
  merge              - optional incremental MERGE (+ target DDL)

Every JOIN carries a comment with its id, status and evidence. Joins that are not
Confirmed are flagged "REQUIRES VALIDATION". Tables without a join path are never
joined: their columns are emitted as typed NULLs with a MISSING JOIN comment.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from config import APP_NAME, STATUS_CONFIRMED
from erp_master import get_erp
from grain_analyzer import find_change_columns
from join_analyzer import table_info
from models import FANOUT_ALLOW, FANOUT_PICK_LATEST
from transformation_engine import convert, source_refs
from utils.text_utils import clean_str, qualified, sql_ident, sql_literal, to_bool

IND = "    "
N_ORD = 2  # fixed number of ordering columns so UNION ALL branches align


def _slug(erp: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", erp.lower()).strip("_")


class Branch:
    """Everything needed to emit SQL for one source ERP."""

    def __init__(self, project, erp: str, prefix: str = ""):
        from mapping_engine import join_tree_for, table_aliases

        self.project, self.erp, self.prefix = project, erp, prefix
        self.cfg = project.config
        self.info = table_info(project.metadata_for(erp))
        self.aliases = table_aliases(project, erp)
        self.grain = project.grain.get(erp, {})
        self.driving = self.grain.get("grain_table", "")
        self.tree = join_tree_for(project, erp)
        self.maps = {m["target_column"]: m for m in project.column_mappings if m.get("source_erp") == erp}
        self.profile = get_erp(erp)
        self.reachable = set(self.tree.get("tables", []))
        self.fanout_ctes: dict[str, str] = {}

    # ------------------------------------------------------------- names
    def cte(self, kind: str, table: str) -> str:
        return f"{kind}_{self.prefix}{self.aliases.get(table, table.lower())}"

    def fq(self, table: str) -> str:
        db = clean_str(self.cfg.get("source_database"))
        schema = self.info.get(table, {}).get("schema") or clean_str(self.cfg.get("source_schema"))
        return qualified(db, schema, table)

    # ------------------------------------------------------------- column needs
    def needed_columns(self) -> dict[str, list[str]]:
        need: dict[str, list[str]] = {t: [] for t in self.reachable}

        def add(t, c):
            if t in need and c in self.info.get(t, {}).get("columns", []) and c not in need[t]:
                need[t].append(c)

        for t in self.reachable:
            for c in self.info[t]["pk"]:
                add(t, c)
        for m in self.maps.values():
            for t, c in source_refs(m):
                add(t, c)
        for s in self.tree.get("steps", []):
            for a, b in s["on"]:
                add(s["from"], a)
                add(s["to"], b)
        for t in self.reachable:
            for c in find_change_columns(t, self.info, self.erp):
                add(t, c)
        cc = self.profile.get("client_column")
        if cc and clean_str(self.cfg.get("sap_client")):
            for t in self.reachable:
                add(t, cc)
        return need

    # ------------------------------------------------------------- pieces
    def date_expr(self, table: str, col: str) -> str:
        a = self.aliases[table]
        e, _, _ = convert(f"{a}.{sql_ident(col)}", col, self.info[table]["types"].get(col, ""), "DATE",
                          self.profile, "", False)
        return e

    def source_where(self, table: str, alias_prefix: str) -> list[str]:
        preds = []
        cc, client = self.profile.get("client_column"), clean_str(self.cfg.get("sap_client"))
        if cc and client and cc in self.info[table]["columns"]:
            preds.append(f"{alias_prefix}.{sql_ident(cc)} = {sql_literal(client)}  -- client filter")
        if table == self.driving:
            for line in clean_str(self.cfg.get("source_filters")).splitlines():
                if line.strip():
                    preds.append(f"({line.strip()})  -- business filter (configuration)")
        return preds

    def source_ctes(self) -> list[str]:
        need = self.needed_columns()
        out = []
        for t in self.tree.get("tables", []):
            a = self.aliases[t]
            role = "driving table / grain" if t == self.driving else "joined table"
            cols = (",\n" + IND * 2).join(f"{a}.{sql_ident(c)}" for c in need[t])
            where = self.source_where(t, a)
            wsql = ("\n" + IND + "WHERE " + ("\n" + IND + "  AND ").join(where)) if where else ""
            out.append(f"{self.cte('src', t)} AS (\n{IND}-- {t}: {self.info[t]['description'] or 'no description'} "
                       f"[{role}]\n{IND}SELECT\n{IND * 2}{cols}\n{IND}FROM {self.fq(t)} AS {a}{wsql}\n)")
        # Fan-out control: keep one row per join key on the many side.
        for s in self.tree.get("steps", []):
            j = s["join"]
            if s["fanout"] and j.get("fanout_handling") == FANOUT_PICK_LATEST:
                t = s["to"]
                keys = ", ".join(sql_ident(b) for _, b in s["on"])
                order = [f"{sql_ident(c)} DESC NULLS LAST" for c in find_change_columns(t, self.info, self.erp)]
                order = order or [f"{sql_ident(c)} DESC" for c in self.info[t]["pk"]] or [keys]
                name = self.cte("one", t)
                self.fanout_ctes[t] = name
                cols = ", ".join(sql_ident(c) for c in need[t])
                out.append(
                    f"{name} AS (\n{IND}-- {j['join_id']}: {t} is not unique on ({keys}). To prevent row multiplication\n"
                    f"{IND}-- keep the latest row per join key. BUSINESS RULE REQUIRES CONFIRMATION.\n"
                    f"{IND}SELECT {cols}\n{IND}FROM {self.cte('src', t)}\n"
                    f"{IND}QUALIFY ROW_NUMBER() OVER (PARTITION BY {keys} ORDER BY {', '.join(order)}) = 1\n)")
        return out

    def from_clause(self) -> str:
        d = self.driving
        lines = [f"FROM {self.cte('src', d)} AS {self.aliases[d]}"]
        for s in self.tree.get("steps", []):
            j, t = s["join"], s["to"]
            src = self.fanout_ctes.get(t, self.cte("src", t))
            status = j.get("status")
            tag = "CONFIRMED" if status == STATUS_CONFIRMED else "REQUIRES VALIDATION"
            card = j.get("cardinality", "")
            lines.append(f"-- {j['join_id']} [{tag}] evidence: {j.get('evidence')} | cardinality {card}")
            if status != STATUS_CONFIRMED:
                lines.append(f"-- WARNING: {j.get('evidence')} - this join is a recommendation, not a verified relationship.")
            if s["fanout"] and j.get("fanout_handling") == FANOUT_ALLOW:
                lines.append("-- WARNING: fan-out allowed - output can contain more than one row per grain key.")
            on = ("\n" + IND + "AND ").join(f"{self.aliases[s['from']]}.{sql_ident(a)} = {self.aliases[t]}.{sql_ident(b)}"
                                            for a, b in s["on"])
            lines.append(f"{j.get('join_type') or 'LEFT JOIN'} {src} AS {self.aliases[t]}\n{IND}ON {on}")
        for t in self.tree.get("unreachable", []):
            lines.append(f"-- MISSING JOIN: {t} is mapped but has no join path to {d}. Not joined; its columns are NULL.")
        return "\n".join(lines)

    def select_items(self, targets: list[dict[str, Any]], with_internal: bool) -> list[tuple[str, str]]:
        """Return (sql_expression_with_alias, comment) pairs. Comments are rendered after the comma."""
        items: list[tuple[str, str]] = []
        for tgt in targets:
            name = tgt["target_column"]
            ttype = clean_str(tgt.get("target_data_type")) or "VARCHAR"
            m = self.maps.get(name)
            if not m:
                items.append((f"CAST(NULL AS {ttype}) AS {sql_ident(name)}", f"not mapped for {self.erp}"))
                continue
            refs = source_refs(m)
            unknown = [f"{t}.{c}" for t, c in refs if t not in self.info or c not in self.info[t]["columns"]]
            if unknown:
                items.append((f"CAST(NULL AS {ttype}) AS {sql_ident(name)}",
                              f"SOURCE NOT IN METADATA: {', '.join(unknown)}"))
                continue
            bad = [t for t, _ in refs if t not in self.reachable]
            if bad:
                items.append((f"CAST(NULL AS {ttype}) AS {sql_ident(name)}", f"MISSING JOIN to {', '.join(bad)}"))
                continue
            expr = clean_str(m.get("transformation_logic")) or f"CAST(NULL AS {ttype})"
            src = ", ".join(f"{t}.{c}" for t, c in refs) or ("constant" if m.get("transformation_type") == "Constant" else "no source")
            comment = f"[{m.get('status')}] {src}"
            if m.get("business_rule"):
                comment += f" | rule: {clean_str(m['business_rule'])[:80]}"
            items.append((f"{expr} AS {sql_ident(name)}", comment))
        if self.project.config.get("mapping_type") == "Cross ERP":
            items.append((f"{sql_literal(self.erp)} AS SOURCE_ERP_NAME", "branch identifier"))
        if with_internal and self.driving:
            a = self.aliases[self.driving]
            keys = self.grain.get("grain_keys", [])
            if self.project.config.get("mapping_type") == "Cross ERP":
                items.append((f"{sql_literal(self.erp)} AS _GK_ERP", "grain key (internal)"))
                comp = ", ".join(f"TO_VARCHAR({a}.{sql_ident(k)})" for k in keys) or "NULL"
                items.append((f"CONCAT_WS('|', {comp}) AS _GK_KEY", f"composite grain key {', '.join(keys)} (internal)"))
            else:
                for k in keys:
                    items.append((f"{a}.{sql_ident(k)} AS _GK_{k}", "grain key (internal)"))
            chg_cols = find_change_columns(self.driving, self.info, self.erp)[:N_ORD]
            for i in range(N_ORD):
                if i < len(chg_cols):
                    items.append((f"{self.date_expr(self.driving, chg_cols[i])} AS _ORD_{i + 1}",
                                  f"version ordering by {chg_cols[i]} (internal)"))
                else:
                    items.append((f"CAST(NULL AS DATE) AS _ORD_{i + 1}", "no further ordering column (internal)"))
            chg = self.change_date_items()
            items.append((f"{chg or 'CAST(NULL AS DATE)'} AS _SRC_CHANGE_DATE",
                          "latest change date across joined tables (internal)"))
        return items

    def change_date_items(self) -> str:
        exprs = []
        for t in self.tree.get("tables", []):
            for c in find_change_columns(t, self.info, self.erp)[:1]:
                exprs.append(self.date_expr(t, c))
        if not exprs:
            return ""
        return exprs[0] if len(exprs) == 1 else f"GREATEST_IGNORE_NULLS({', '.join(exprs)})"

    def mapped_cte(self, targets) -> str:
        sel = render_select(self.select_items(targets, True), IND * 2)
        frm = self.from_clause().replace("\n", "\n" + IND)
        return f"{self.mapped_name()} AS (\n{IND}SELECT\n{sel}\n{IND}{frm}\n)"

    def mapped_name(self) -> str:
        return f"mapped_{self.prefix.rstrip('_') or 'rows'}"


def render_select(items: list[tuple[str, str]], indent: str) -> str:
    lines = []
    for i, (expr, comment) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        lines.append(f"{indent}{expr}{comma}" + (f"  -- {comment}" if comment else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- assembly
def _branches(project) -> list[Branch]:
    erps = []
    for m in project.column_mappings:
        if m.get("source_erp") and m["source_erp"] not in erps:
            erps.append(m["source_erp"])
    cross = project.config.get("mapping_type") == "Cross ERP" and len(erps) > 1
    return [Branch(project, e, f"{_slug(e)}_" if cross else "") for e in erps
            if project.grain.get(e, {}).get("grain_table")]


def _header(project, title: str) -> str:
    c = project.config
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (f"/* ==========================================================================\n"
            f"   {title}\n"
            f"   Project : {c.get('project_name')}\n"
            f"   Target  : {_target_fq(project)}\n"
            f"   Source  : {', '.join(project.erps)}\n"
            f"   Generated by {APP_NAME} on {ts}\n"
            f"   Joins marked REQUIRES VALIDATION are recommendations, not verified ERP relationships.\n"
            f"   ========================================================================== */\n")


def _target_fq(project) -> str:
    c = project.config
    return qualified(c.get("target_database", ""), c.get("target_schema", ""), c.get("target_object", "") or "TARGET_OBJECT")


def _all_ctes(project, branches) -> list[str]:
    ctes = []
    for b in branches:
        ctes.extend(b.source_ctes())
        ctes.append(b.mapped_cte(project.target_columns))
    if len(branches) > 1:
        union = ("\n" + IND + "UNION ALL\n" + IND).join(f"SELECT * FROM {b.mapped_name()}" for b in branches)
        ctes.append(f"mapped_all AS (\n{IND}{union}\n)")
    return ctes


def _mapped_source(branches) -> str:
    return "mapped_all" if len(branches) > 1 else branches[0].mapped_name()


def _final_cols(project, alias: str = "") -> list[str]:
    p = f"{alias}." if alias else ""
    cols = [f"{p}{sql_ident(t['target_column'])}" for t in project.target_columns]
    if project.config.get("mapping_type") == "Cross ERP":
        cols.append(f"{p}SOURCE_ERP_NAME")
    return cols


def source_extraction_sql(project, branches) -> str:
    parts = [_header(project, "SOURCE EXTRACTION / PROFILING QUERIES")]
    for b in branches:
        need = b.needed_columns()
        for t in b.tree.get("tables", []):
            a = b.aliases[t]
            cols = (",\n" + IND).join(f"{a}.{sql_ident(c)}" for c in need[t])
            where = [p.split("  --")[0] for p in b.source_where(t, a)]
            w = ("\nWHERE " + "\n  AND ".join(where)) if where else ""
            parts.append(f"-- {b.erp} | {t}: {b.info[t]['description']}\n-- PK: {', '.join(b.info[t]['pk']) or 'not defined'}\n"
                         f"SELECT\n{IND}{cols}\nFROM {b.fq(t)} AS {a}{w};\n")
    return "\n".join(parts)


def integrated_sql(project, branches) -> str:
    ctes = _all_ctes(project, branches)
    cols = (",\n" + IND).join(_final_cols(project, "m"))
    return (_header(project, "INTEGRATED MAPPING QUERY (joins + transformations, before final deduplication)")
            + "WITH\n" + ",\n".join(ctes) + f"\nSELECT\n{IND}{cols}\nFROM {_mapped_source(branches)} AS m;\n")


def _dedup_body(project, branches, force_current: bool = False) -> str:
    ctes = _all_ctes(project, branches)
    hist = "" if force_current else project.config.get("history_requirement", "")
    src = _mapped_source(branches)
    b0 = branches[0]
    cross = project.config.get("mapping_type") == "Cross ERP"
    keys = ["_GK_ERP", "_GK_KEY"] if cross else [f"_GK_{k}" for k in b0.grain.get("grain_keys", [])]
    n_ord = N_ORD if any(find_change_columns(b.driving, b.info, b.erp) for b in branches) else 0
    order = ", ".join(f"m._ORD_{i} DESC NULLS LAST" for i in range(1, n_ord + 1)) or ", ".join(f"m.{k}" for k in keys)
    part = ", ".join(f"m.{k}" for k in keys) or "1"
    cols = (",\n" + IND).join(_final_cols(project, "d"))
    grain_txt = b0.grain.get("grain_description", "")

    if hist.startswith("Full history"):
        eff = "m._ORD_1" if n_ord else "CAST(NULL AS DATE)"
        ctes.append(
            f"versioned AS (\n{IND}-- History: one row per grain key per version. Requires a source that retains\n"
            f"{IND}-- prior versions (e.g. CDC/history tables); ERP base tables hold current state only.\n"
            f"{IND}SELECT\n{IND * 2}m.*,\n{IND * 2}{eff} AS EFFECTIVE_FROM,\n"
            f"{IND * 2}DATEADD(day, -1, LEAD({eff}) OVER (PARTITION BY {part} ORDER BY {order.replace('DESC NULLS LAST', 'ASC NULLS FIRST')})) AS EFFECTIVE_TO,\n"
            f"{IND * 2}IFF(ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY {order}) = 1, TRUE, FALSE) AS IS_CURRENT\n"
            f"{IND}FROM {src} AS m\n"
            f"{IND}-- remove exact repeats of the same version\n"
            f"{IND}QUALIFY ROW_NUMBER() OVER (PARTITION BY {part}{', ' + ', '.join(f'm._ORD_{i}' for i in range(1, n_ord + 1)) if n_ord else ''} ORDER BY {order}) = 1\n)")
        return ("WITH\n" + ",\n".join(ctes) +
                f"\nSELECT\n{IND}{cols},\n{IND}d.EFFECTIVE_FROM,\n{IND}d.EFFECTIVE_TO,\n{IND}d.IS_CURRENT\nFROM versioned AS d")

    as_of = ""
    if hist.startswith("Snapshot") and n_ord:
        as_of = f"\n{IND}WHERE m._ORD_1 <= $AS_OF_DATE  -- snapshot date (SET AS_OF_DATE before running)"
    ctes.append(
        f"deduplicated AS (\n{IND}-- Grain: {grain_txt}\n"
        f"{IND}-- ROW_NUMBER keeps the latest version per grain key; DISTINCT is not used.\n"
        f"{IND}SELECT\n{IND * 2}m.*,\n{IND * 2}ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY {order}) AS _RN\n"
        f"{IND}FROM {src} AS m{as_of}\n)")
    return "WITH\n" + ",\n".join(ctes) + f"\nSELECT\n{IND}{cols}\nFROM deduplicated AS d\nWHERE d._RN = 1"


def dedup_sql(project, branches) -> str:
    pre = ""
    if project.config.get("history_requirement", "").startswith("Snapshot"):
        pre = "SET AS_OF_DATE = CURRENT_DATE();  -- change to the required snapshot date\n\n"
    return _header(project, "FINAL MAPPING QUERY WITH DEDUPLICATION / HISTORY") + pre + _dedup_body(project, branches) + ";\n"


def create_view_sql(project, branches) -> str:
    c = project.config
    kind = "VIEW" if c.get("target_object_type", "VIEW") == "VIEW" else "TABLE"
    lines = [_header(project, f"CREATE {kind}")]
    if c.get("history_requirement", "").startswith("Snapshot"):
        lines.append("-- NOTE: snapshot logic uses session variable $AS_OF_DATE; replace with a literal date for a view.\n")
    lines.append(f"CREATE OR REPLACE {kind} {_target_fq(project)}\n"
                 f"COMMENT = {sql_literal('Generated by ' + APP_NAME + ' - ' + c.get('project_name', ''))}\nAS\n"
                 + _dedup_body(project, branches) + ";\n")
    return "\n".join(lines)


def merge_sql(project, branches) -> str:
    b0 = branches[0]
    tgt = _target_fq(project)
    grain_targets, extra_keys = [], []
    cross = project.config.get("mapping_type") == "Cross ERP"
    if cross:
        extra_keys = ["KEY"]
    else:
        for k in b0.grain.get("grain_keys", []):
            mt = next((m["target_column"] for m in b0.maps.values()
                       if source_refs(m) == [(b0.driving, k)]), None)
            (grain_targets if mt else extra_keys).append(mt or k)
    key_cols = grain_targets + [f"GRAIN_{k}" for k in extra_keys] + (["SOURCE_ERP_NAME"] if cross else [])
    tcols = [t["target_column"] for t in project.target_columns] + (["SOURCE_ERP_NAME"] if cross else [])
    all_cols = tcols + [f"GRAIN_{k}" for k in extra_keys] + ["ETL_SOURCE_CHANGE_DATE", "ETL_LOADED_AT"]
    types = {t["target_column"]: clean_str(t.get("target_data_type")) or "VARCHAR" for t in project.target_columns}
    ddl_cols = render_select(
        [(f"{sql_ident(c)} {types.get(c, 'VARCHAR')}", "") for c in tcols]
        + [(f"GRAIN_{k} VARCHAR", "grain key not mapped to a target column") for k in extra_keys]
        + [("ETL_SOURCE_CHANGE_DATE DATE", "latest source change date (watermark)"),
           ("ETL_LOADED_AT TIMESTAMP_NTZ", "load audit timestamp")], IND)
    has_change = bool(b0.change_date_items())
    body = _dedup_body(project, branches, force_current=True)
    # expose internal columns needed for MERGE
    extra_sel = "".join(f",\n{IND}d._GK_{k} AS GRAIN_{k}" for k in extra_keys)
    extra_sel += f",\n{IND}d._SRC_CHANGE_DATE AS ETL_SOURCE_CHANGE_DATE" if has_change else \
        f",\n{IND}CAST(NULL AS DATE) AS ETL_SOURCE_CHANGE_DATE"
    body = body.replace("\nFROM deduplicated AS d", extra_sel + "\nFROM deduplicated AS d", 1)
    if has_change:
        body += "\n  AND d._SRC_CHANGE_DATE >= $LOAD_WATERMARK  -- incremental window"
    on = "\n    AND ".join(f"tgt.{sql_ident(k)} = src.{sql_ident(k)}" for k in key_cols) or "1 = 0 /* NO GRAIN KEYS */"
    non_key = [c for c in all_cols if c not in key_cols and c != "ETL_LOADED_AT"]
    upd = (",\n" + IND * 2).join(f"tgt.{sql_ident(c)} = src.{sql_ident(c)}" for c in non_key)
    ins_cols = ", ".join(sql_ident(c) for c in all_cols)
    ins_vals = ", ".join(("CURRENT_TIMESTAMP()" if c == "ETL_LOADED_AT" else f"src.{sql_ident(c)}") for c in all_cols)
    hist_note = ""
    if project.config.get("history_requirement", "").startswith("Full history"):
        hist_note = "-- NOTE: history mode selected; MERGE below maintains CURRENT rows only. Use the history query for SCD2 loads.\n"
    lines = [_header(project, "INCREMENTAL LOAD (MERGE)"), hist_note,
             f"-- 1) Target table (run once)\nCREATE TABLE IF NOT EXISTS {tgt} (\n{ddl_cols}\n);\n",
             "-- 2) Watermark with a 3-day lookback for late-arriving changes\n"
             f"SET LOAD_WATERMARK = (SELECT COALESCE(DATEADD(day, -3, MAX(ETL_SOURCE_CHANGE_DATE)), '1900-01-01'::DATE) FROM {tgt});\n"
             if has_change else "-- 2) No change-date column found in metadata: MERGE processes the full source each run.\n",
             f"-- 3) Merge on the grain keys: {', '.join(key_cols)}\nMERGE INTO {tgt} AS tgt\nUSING (\n"
             + body.replace("\n", "\n" + IND) + f"\n) AS src\nON {on}\nWHEN MATCHED THEN UPDATE SET\n{IND * 2}{upd},\n"
             f"{IND * 2}tgt.ETL_LOADED_AT = CURRENT_TIMESTAMP()\nWHEN NOT MATCHED THEN INSERT ({ins_cols})\n"
             f"{IND}VALUES ({ins_vals});\n"]
    if has_change:
        lines.append("-- Change detection uses the latest change date across the driving and joined tables.\n"
                     "-- Deletions in the source are not detected by this MERGE; add a soft-delete process if required.\n")
    return "\n".join(l for l in lines if l)


def data_quality_sql(project, branches) -> str:
    tgt = _target_fq(project)
    out = [_header(project, "DATA QUALITY VALIDATION QUERIES")]
    b0 = branches[0]
    # 1. Record count reconciliation
    out.append("-- DQ-01 Record count reconciliation: driving source rows vs target rows")
    srcs = []
    for b in branches:
        where = b.source_where(b.driving, "s")
        w = (" WHERE " + " AND ".join(p.split("  --")[0].replace(f"{b.aliases[b.driving]}.", "s.") for p in where)) if where else ""
        srcs.append(f"(SELECT COUNT(*) FROM {b.fq(b.driving)} AS s{w})")
    out.append(f"SELECT\n{IND}{' + '.join(srcs)} AS SOURCE_ROWS,\n{IND}(SELECT COUNT(*) FROM {tgt}) AS TARGET_ROWS,\n"
               f"{IND}SOURCE_ROWS - TARGET_ROWS AS DIFFERENCE;\n")
    # 2. Duplicates on grain
    grain_targets = [m["target_column"] for k in b0.grain.get("grain_keys", []) for m in b0.maps.values()
                     if source_refs(m) == [(b0.driving, k)]]
    if project.config.get("mapping_type") == "Cross ERP":
        grain_targets.append("SOURCE_ERP_NAME")
    out.append("-- DQ-02 Duplicate validation on the grain (expect 0 rows)")
    if grain_targets:
        g = ", ".join(sql_ident(c) for c in grain_targets)
        hist = project.config.get("history_requirement", "").startswith("Full history")
        extra = "\nWHERE IS_CURRENT" if hist else ""
        out.append(f"SELECT {g}, COUNT(*) AS ROW_COUNT\nFROM {tgt}{extra}\nGROUP BY {g}\nHAVING COUNT(*) > 1;\n")
        unmapped = [k for k in b0.grain.get("grain_keys", []) if not any(source_refs(m) == [(b0.driving, k)] for m in b0.maps.values())]
        if unmapped:
            out.append(f"-- NOTE: grain key(s) {', '.join(unmapped)} are not target columns; this check may report false duplicates.\n")
    else:
        out.append("-- Grain keys are not mapped to target columns - add them to enable this check.\n")
    # 3. Null validation for mandatory columns
    mand = [t["target_column"] for t in project.target_columns if to_bool(t.get("mandatory"))]
    out.append("-- DQ-03 Null validation for mandatory columns (expect 0 in every column)")
    if mand:
        items = (",\n" + IND).join(f"SUM(IFF({sql_ident(c)} IS NULL, 1, 0)) AS NULL_{c}" for c in mand)
        out.append(f"SELECT\n{IND}COUNT(*) AS TOTAL_ROWS,\n{IND}{items}\nFROM {tgt};\n")
    else:
        out.append("-- No mandatory target columns defined.\n")
    # 4. Referential integrity per join step
    out.append("-- DQ-04 Referential integrity: driving-side rows without a match (orphans)")
    for b in branches:
        for s in b.tree.get("steps", []):
            j = s["join"]
            fa, ta = b.aliases[s["from"]], b.aliases[s["to"]]
            on = " AND ".join(f"{fa}.{sql_ident(a)} = {ta}.{sql_ident(c)}" for a, c in s["on"])
            nullchk = f"{ta}.{sql_ident(s['on'][0][1])} IS NULL"
            out.append(f"-- {b.erp} {j['join_id']} [{j['status']}] {s['from']} -> {s['to']}\n"
                       f"SELECT COUNT(*) AS ORPHAN_ROWS\nFROM {b.fq(s['from'])} AS {fa}\n"
                       f"LEFT JOIN {b.fq(s['to'])} AS {ta}\n{IND}ON {on}\nWHERE {nullchk};\n")
            if s["fanout"]:
                keys = ", ".join(sql_ident(c) for _, c in s["on"])
                out.append(f"-- {j['join_id']} fan-out profile: join keys on {s['to']} with more than one row\n"
                           f"SELECT {keys}, COUNT(*) AS ROWS_PER_KEY\nFROM {b.fq(s['to'])}\nGROUP BY {keys}\n"
                           f"HAVING COUNT(*) > 1\nORDER BY ROWS_PER_KEY DESC\nLIMIT 100;\n")
    # 5. Data type validation for TRY_ conversions
    out.append("-- DQ-05 Data type validation: source values that fail conversion (expect 0)")
    any_type = False
    for b in branches:
        for m in b.maps.values():
            logic = clean_str(m.get("transformation_logic"))
            refs = source_refs(m)
            if "TRY_" in logic and len(refs) == 1 and refs[0][0] in b.info:
                t, c = refs[0]
                a = b.aliases[t]
                any_type = True
                out.append(f"-- {b.erp} {m['target_column']} <- {t}.{c}\nSELECT COUNT(*) AS FAILED_CONVERSIONS\n"
                           f"FROM {b.fq(t)} AS {a}\nWHERE {a}.{sql_ident(c)} IS NOT NULL\n"
                           f"  AND TRIM(TO_VARCHAR({a}.{sql_ident(c)})) NOT IN ('', '00000000')\n  AND ({logic}) IS NULL;\n")
    if not any_type:
        out.append("-- No TRY_ conversions in the mapping.\n")
    # 6. Business rules
    out.append("-- DQ-06 Business rule validation (complete the predicate, expect 0)")
    rules = [t for t in project.target_columns if clean_str(t.get("business_rules"))]
    for t in rules:
        out.append(f"-- {t['target_column']}: {clean_str(t['business_rules'])}\n"
                   f"SELECT COUNT(*) AS RULE_VIOLATIONS\nFROM {tgt}\nWHERE NOT (/* TODO: express rule for "
                   f"{sql_ident(t['target_column'])} */ TRUE);\n")
    if not rules:
        out.append("-- No business rules captured in the target requirements.\n")
    return "\n".join(out)


def generate_all_sql(project) -> dict[str, str]:
    branches = _branches(project)
    if not branches:
        msg = "-- SQL not generated: no ERP branch has a grain/driving table. Load metadata and run the analysis.\n"
        return {k: msg for k in ("source_extraction", "integrated", "deduplication", "create_view", "data_quality", "merge")}
    return {
        "source_extraction": source_extraction_sql(project, branches),
        "integrated": integrated_sql(project, branches),
        "deduplication": dedup_sql(project, branches),
        "create_view": create_view_sql(project, branches),
        "data_quality": data_quality_sql(project, branches),
        "merge": merge_sql(project, branches),
    }


SQL_LABELS = {
    "source_extraction": "1. Source extraction",
    "integrated": "2. Integrated mapping",
    "deduplication": "3. Deduplication (final)",
    "create_view": "3b. Create view/table",
    "data_quality": "4. Data quality checks",
    "merge": "5. Incremental MERGE",
}


def combined_script(sql: dict[str, str]) -> str:
    return "\n\n".join(f"-- ##########  {SQL_LABELS.get(k, k)}  ##########\n{v}" for k, v in sql.items())
