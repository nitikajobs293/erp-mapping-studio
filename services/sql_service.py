"""Snowflake SQL generation for a MappingResult.

Design rules (from the spec):
- explicit aliases and join conditions, INNER only where referential integrity is guaranteed (item -> header)
- ROW_NUMBER() only for *justified* deduplication: replication duplicates on the source primary key,
  ordered by the CDC timestamp. Never DISTINCT.
- 1:N joins that change the grain are flagged in comments.
"""
from __future__ import annotations

import re
from datetime import datetime

from config.settings import SQL_DEFAULTS
from services.erp_knowledge_service import ERPKnowledge
from services.mapping_engine import MappingResult
from services.transform_rules import column_ref, quote_ident

SAP_STATUS_FIELDS = {"GBSTK", "GBSTA", "LFSTK", "LFSTA", "KOSTK", "KOSTA", "PKSTK", "PKSTA", "WBSTK", "WBSTA",
                     "ABSTK", "ABSTA"}

CHANGE_COLUMNS = {  # natural change-date columns per ERP family, used when no CDC column exists
    "SAP": ["AEDAT", "ERDAT"],
    "JDE": ["UPMJ"],
    "Oracle": ["LAST_UPDATE_DATE"],
    "Dynamics": ["MODIFIEDDATETIME"],
}


class SQLBuilder:
    def __init__(self, result: MappingResult, erp: ERPKnowledge, settings: dict | None = None):
        self.r = result
        self.erp = erp
        self.s = {**SQL_DEFAULTS, **(settings or {})}
        self.case = self.s.get("identifier_case", "UPPER")
        schema = self.s.get("schema") or "{erp_id}"
        self.schema = schema.replace("{erp_id}", erp.id.upper())
        self.db = self.s.get("database", "RAW_DB")
        self.cdc = self.s.get("cdc_timestamp_column") or ""
        self.cdc_del = self.s.get("cdc_delete_column") or ""
        self.client_col = erp.meta.get("client_column")

    # ------------------------------------------------------------------ naming
    def physical(self, table: str) -> str:
        tmpl = self.erp.meta.get("physical_name_template")
        if tmpl:
            params = {**(self.erp.meta.get("template_params") or {}), **(self.s.get("template_params") or {})}
            try:
                table = tmpl.format(table=table, **params)
            except KeyError:
                pass
        return f"{self.db}.{self.schema}.{quote_ident(table, self.case)}"

    def cte_name(self, table: str) -> str:
        return "src_" + re.sub(r"[^A-Za-z0-9_]", "_", table).lower()

    def col(self, alias: str, field: str) -> str:
        return column_ref(self.erp, alias, field, self.case)

    def pk(self, table: str) -> list[str]:
        return list(self.erp.table(table).get("primary_key", []) or [])

    def _render_filter(self, flt: str, alias_map: dict) -> str:
        if not flt:
            return ""
        out = flt.replace("'E'", f"'{self.s.get('language', 'E')}'") if "SPRAS" in flt else flt
        for t, a in alias_map.items():
            out = out.replace("{" + t + "}", a)
        return out

    def _has_client(self, table: str) -> bool:
        return bool(self.client_col) and self.client_col in self.pk(table)

    # ------------------------------------------------------------------ sources
    def source_expr(self, table: str, mode: str) -> str:
        """FROM-clause source for a table: physical name, a dedup CTE, or a virtual (aggregated) CTE."""
        td = self.erp.table(table)
        if td.get("virtual") or mode in ("dedup", "incremental", "view", "dbt", "historical"):
            return self.cte_name(table)
        return self.physical(table)

    def _raw_ref(self, table: str, mode: str) -> str:
        if mode == "dbt":
            return "{{ source('" + self.erp.id + "', '" + table.lower() + "') }}"
        return self.physical(table)

    def staging_ctes(self, mode: str) -> list[str]:
        ctes = []
        dedup = mode in ("dedup", "incremental", "view", "dbt")
        hist_union = self.r.history_union or {}
        for t in self.staged_tables():
            td = self.erp.table(t)
            if not dedup and mode != "historical":
                continue
            src = self._raw_ref(t, mode)
            if mode == "historical" and t in hist_union:
                h = hist_union[t]
                src = (f"(\n        SELECT *, '{t}' AS _SOURCE_TABLE FROM {self._raw_ref(t, mode)}\n"
                       f"        UNION ALL\n        SELECT *, '{h}' AS _SOURCE_TABLE FROM {self._raw_ref(h, mode)}\n    )")
            pk = self.pk(t)
            lines = [f"{self.cte_name(t)} AS (",
                     f"    -- {t}: {td.get('description', '')}",
                     f"    SELECT *", f"    FROM {src}"]
            where = []
            if self.cdc_del and mode != "historical":
                where.append(f"COALESCE({quote_ident(self.cdc_del, self.case)}, FALSE) = FALSE")
            if self.s.get("apply_client_filter") and self._has_client(t):
                where.append(f"{quote_ident(self.client_col, self.case)} = '{self.s.get('sap_client')}'")
            if where:
                lines.append("    WHERE " + "\n      AND ".join(where))
            if (dedup or (mode == "historical" and t in hist_union)) and pk and self.cdc:
                part = ", ".join(quote_ident(p, self.case) for p in pk)
                lines.append(f"    -- Justified dedup: replication can deliver several versions of the same source PK ({', '.join(pk)});")
                lines.append(f"    -- keep the latest version by CDC timestamp. This does NOT hide business duplicates.")
                lines.append(f"    QUALIFY ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY {quote_ident(self.cdc, self.case)} DESC) = 1")
            elif dedup and not pk:
                lines.append(f"    -- WARNING: primary key of {t} unknown - dedup skipped. Confirm the key before production use.")
            lines.append(")")
            ctes.append("\n".join(lines))
        return ctes

    def _virtual_refs(self) -> list[str]:
        refs = []
        for t in self.r.tables:
            td = self.erp.table(t)
            if td.get("virtual"):
                refs += [x for x in re.findall(r"\{\{(\w+)\}\}", td.get("sql", ""))]
        return refs

    def staged_tables(self) -> list[str]:
        out = [t for t in self.r.tables if not self.erp.table(t).get("virtual")]
        for ref in self._virtual_refs():
            if ref not in out and self.erp.find_table(ref):
                out.append(self.erp.find_table(ref))
        return out

    def virtual_ctes(self, mode: str) -> list[str]:
        staged = mode in ("dedup", "incremental", "view", "dbt", "historical")
        ctes = []
        for t in self.r.tables:
            td = self.erp.table(t)
            if not td.get("virtual"):
                continue
            sql = td.get("sql", "")
            for ref in re.findall(r"\{\{(\w+)\}\}", sql):
                src = self.cte_name(ref) if staged else self._raw_ref(ref, mode)
                sql = sql.replace("{{" + ref + "}}", src)
            body = "\n".join("    " + l for l in sql.strip().splitlines())
            ctes.append(f"{self.cte_name(t)} AS (\n    -- {t}: {td.get('description', '')} (pre-aggregated to avoid fan-out)\n{body}\n)")
        return ctes

    # ------------------------------------------------------------------ select list / joins
    def select_list(self, only_anchor: bool = False) -> list[str]:
        items, seen = [], set()
        a = self.r.aliases
        anchor = self.r.anchor
        if not only_anchor:
            for p in self.pk(anchor):
                if p == self.client_col:
                    continue
                name = f"{re.sub(r'[^A-Z0-9]', '_', anchor.upper())}_{p.upper()}"
                items.append(f"{self.col(a[anchor], p)} AS {quote_ident(name)}  -- grain key")
                seen.add(name)
        for row in self.r.rows:
            sql = row.get("_sql")
            tgt = row["Target Snowflake Column"]
            if only_anchor and row.get("_tables") != [anchor]:
                continue
            if not sql:
                items.append(f"NULL AS {quote_ident(self._unique(tgt, seen))}  -- {row['Business Column']}: "
                             f"{row['Availability']} - {row['Alternative Source'][:80]}")
                continue
            name = self._unique(tgt, seen)
            items.append(f"{sql} AS {quote_ident(name)}")
            fld = str(row.get("Source Field", ""))
            if self.erp.family == "SAP" and fld in SAP_STATUS_FIELDS:
                d = self._unique(name + "_DESC", seen)
                items.append(f"CASE {sql} WHEN 'A' THEN 'Not yet processed' WHEN 'B' THEN 'Partially processed' "
                             f"WHEN 'C' THEN 'Completely processed' ELSE 'Not relevant' END AS {quote_ident(d)}")
        return items

    @staticmethod
    def render_items(items: list[str]) -> str:
        """Join select items, keeping trailing '-- comments' after the comma."""
        out = []
        for i, it in enumerate(items):
            expr, _, comment = it.partition("  -- ")
            sep = "," if i < len(items) - 1 else ""
            out.append(f"    {expr}{sep}" + (f"  -- {comment}" if comment else ""))
        return "\n".join(out)

    @staticmethod
    def _unique(name: str, seen: set) -> str:
        n, i = name, 2
        while n in seen:
            n = f"{name}_{i}"
            i += 1
        seen.add(n)
        return n

    def join_clauses(self, mode: str) -> list[str]:
        out = []
        a = self.r.aliases
        for j in self.r.joins:
            left, right = j["left"], j["right"]
            conds = [f"{self.col(a[left], lf)} = {self.col(a[right], rf)}" for lf, rf in j["conditions"]]
            if self.client_col and self._has_client(left) and self._has_client(right) and \
                    not any(lf == self.client_col for lf, _ in j["conditions"]):
                conds.insert(0, f"{self.col(a[left], self.client_col)} = {self.col(a[right], self.client_col)}")
            flt = self._render_filter(j.get("filter", ""), a)
            if flt:
                conds.append(flt)
            comment = f"-- {left} -> {right} ({j['cardinality']}"
            comment += ", FAN-OUT: grain changes)" if j.get("fan_out") else ")"
            if j.get("label"):
                comment += f" {j['label']}"
            out.append(f"{comment}\n{j['join_type']} {self.source_expr(right, mode)} {a[right]}\n    ON " +
                       "\n   AND ".join(conds))
        return out

    def where_clauses(self, mode: str) -> list[str]:
        a = self.r.aliases
        where = []
        if self.r.base_filter:
            where.append(self._render_filter(self.r.base_filter, a))
        if mode in ("basic", "joined") and self.s.get("apply_client_filter") and self._has_client(self.r.anchor):
            where.append(f"{self.col(a[self.r.anchor], self.client_col)} = '{self.s.get('sap_client')}'")
        return where

    def _change_col(self, table: str) -> str | None:
        fields = (self.erp.table(table).get("fields") or {})
        for c in CHANGE_COLUMNS.get(self.erp.family, []):
            for f in fields:
                if f.upper().endswith(c):
                    return f
        return None

    # ------------------------------------------------------------------ builders
    def header_comment(self, qtype: str) -> str:
        r = self.r
        lines = [
            f"-- {qtype}",
            f"-- ERP: {r.erp_name}{' ' + r.erp_version if r.erp_version else ''} | Business object: {r.business_object_label}",
            f"-- Grain: {r.grain}",
            f"-- Anchor table: {r.anchor} | Business key: {', '.join(r.business_key)}",
            f"-- Generated: {datetime.now():%Y-%m-%d %H:%M} by ERP Logistics Data Architect - review before production use",
        ]
        if any(j.get("fan_out") for j in r.joins):
            lines.append("-- WARNING: contains 1:N joins that change the grain - see join comments.")
        unav = [row["Business Column"] for row in r.rows if not row.get("_sql")]
        if unav:
            lines.append(f"-- Columns without a confirmed source (emitted as NULL): {', '.join(unav)}")
        return "\n".join(lines)

    def basic(self) -> str:
        a = self.r.aliases[self.r.anchor]
        cols = self.select_list(only_anchor=True) or [f"{a}.*"]
        where = self.where_clauses("basic")
        sql = [self.header_comment("Basic SELECT (anchor table only)"),
               "SELECT\n" + self.render_items(cols), f"FROM {self.physical(self.r.anchor)} {a}"]
        if where:
            sql.append("WHERE " + "\n  AND ".join(where))
        return "\n".join(sql) + ";"

    def _body(self, mode: str, extra_where: list[str] | None = None, extra_cols: list[str] | None = None) -> str:
        a = self.r.aliases
        cols = self.select_list() + (extra_cols or [])
        body = ["SELECT\n" + self.render_items(cols),
                f"FROM {self.source_expr(self.r.anchor, mode)} {a[self.r.anchor]}"]
        body += self.join_clauses(mode)
        where = self.where_clauses(mode) + (extra_where or [])
        if where:
            body.append("WHERE " + "\n  AND ".join(where))
        return "\n".join(body)

    def _with(self, mode: str) -> str:
        ctes = self.staging_ctes(mode) + self.virtual_ctes(mode)
        return ("WITH\n" + ",\n".join(ctes) + "\n") if ctes else ""

    def joined(self) -> str:
        return self.header_comment("Complete joined SELECT") + "\n" + self._with("joined") + self._body("joined") + ";"

    def dedup(self) -> str:
        return self.header_comment("Deduplicated query (latest version per source primary key)") + "\n" + \
            self._with("dedup") + self._body("dedup") + ";"

    def _cdc_expr(self) -> str:
        a = self.r.aliases
        tabs = [t for t in [self.r.anchor, self.r.header] if t and not self.erp.table(t).get("virtual")]
        parts = [f"{a[t]}.{quote_ident(self.cdc, self.case)}" for t in tabs if t in a]
        return parts[0] if len(parts) == 1 else f"GREATEST({', '.join(parts)})"

    def incremental(self) -> str:
        target = f"{self.s.get('target_database')}.{self.s.get('target_schema')}.{self.view_name()}"
        cdc = self._cdc_expr()
        pre = (f"-- Watermark = latest source change already loaded into the target table.\n"
               f"SET LAST_WATERMARK = (SELECT COALESCE(MAX(_SOURCE_UPDATED_AT), '1900-01-01'::TIMESTAMP_NTZ) FROM {target});\n\n")
        note = ("-- Header and item changes are both captured (an item must be reloaded when only its header changed).\n"
                "-- Feed the result into MERGE on the grain key columns.\n")
        body = self._body("incremental", extra_where=[f"{cdc} > $LAST_WATERMARK"],
                          extra_cols=[f"{cdc} AS _SOURCE_UPDATED_AT"])
        alt = self._change_col(self.r.anchor)
        if alt:
            note += (f"-- No CDC column? Use the ERP change date instead, e.g. {self.r.anchor}.{alt} "
                     f"(less reliable: item tables often lack change dates, deletions are not captured).\n")
        return self.header_comment("Incremental load query") + "\n" + pre + note + self._with("incremental") + body + ";"

    def _date_column(self) -> str | None:
        prefer = ["order_date", "actual_goods_issue_date", "movement_date", "invoice_date", "shipment_date", "gr_date"]
        rows = [r for r in self.r.rows if r.get("_sql") and r.get("Target Data Type") == "DATE"]
        rows.sort(key=lambda r: prefer.index(r["_concept"]) if r.get("_concept") in prefer else 99)
        return rows[0]["_sql"] if rows else None

    def historical(self) -> str:
        dcol = self._date_column()
        pre = ("SET START_DATE = '2020-01-01'::DATE;\nSET END_DATE = CURRENT_DATE();\n\n")
        extra = [f"{dcol} BETWEEN $START_DATE AND $END_DATE"] if dcol else []
        cols = []
        if self.cdc_del:
            cols.append(f"{self.r.aliases[self.r.anchor]}.{quote_ident(self.cdc_del, self.case)} AS _IS_DELETED_IN_SOURCE")
        notes = ["-- Historical extract: soft-deleted rows are KEPT (flagged) for audit; no latest-version filter on deletes."]
        if self.r.history_union:
            notes.append("-- History tables are UNIONed with the active tables: " +
                         ", ".join(f"{k} + {v}" for k, v in self.r.history_union.items()))
        if not dcol:
            notes.append("-- No date column mapped - add a business date column to filter the history window.")
        notes.append("-- For point-in-time history of master/status data, snapshot daily into SCD2 tables (see ingestion notes).")
        return self.header_comment("Historical data query") + "\n" + pre + "\n".join(notes) + "\n" + \
            self._with("historical") + self._body("historical", extra_where=extra, extra_cols=cols) + ";"

    def view_name(self) -> str:
        return f"V_{self.erp.id.upper()}_{(self.r.business_object or 'DATASET').upper()}"

    def view(self) -> str:
        name = f"{self.s.get('target_database')}.{self.s.get('target_schema')}.{self.view_name()}"
        return (self.header_comment("CREATE OR REPLACE VIEW") + "\n" +
                f"CREATE OR REPLACE VIEW {name}\n    COMMENT = '{self.r.erp_name} {self.r.business_object_label} - grain: "
                f"{self.r.grain.replace(chr(39), '')}'\nAS\n" + self._with("view") + self._body("view") + ";")

    def dbt(self) -> str:
        keys = [f"{re.sub(r'[^A-Z0-9]', '_', self.r.anchor.upper())}_{p.upper()}" for p in self.pk(self.r.anchor)
                if p != self.client_col]
        cdc = self._cdc_expr()
        cfg = ("{{\n  config(\n    materialized = 'incremental',\n    incremental_strategy = 'merge',\n"
               f"    unique_key = {keys},\n    on_schema_change = 'append_new_columns',\n"
               f"    tags = ['{self.erp.id}', '{self.r.business_object}']\n  )\n}}}}\n")
        body = self._body("dbt", extra_cols=[f"{cdc} AS _SOURCE_UPDATED_AT"])
        inc = ("\n{% if is_incremental() %}\n" +
               ("  AND " if "\nWHERE " in body else "WHERE ") +
               f"{cdc} > (SELECT COALESCE(MAX(_SOURCE_UPDATED_AT), '1900-01-01'::TIMESTAMP_NTZ) FROM {{{{ this }}}})\n"
               "{% endif %}")
        header = self.header_comment("dbt model").replace("\n--", "\n--")
        return f"{cfg}\n{header}\n{self._with('dbt')}{body}{inc}\n"

    def dbt_sources_yml(self) -> str:
        lines = ["version: 2", "sources:", f"  - name: {self.erp.id}", f"    database: {self.db}",
                 f"    schema: {self.schema}", "    tables:"]
        for t in self.r.tables:
            td = self.erp.table(t)
            if td.get("virtual"):
                continue
            lines.append(f"      - name: {t.lower()}")
            lines.append(f"        description: \"{td.get('description', '')}\"")
            if self.cdc:
                lines.append(f"        loaded_at_field: {self.cdc}")
        return "\n".join(lines) + "\n"

    def build(self, qtype: str) -> str:
        q = qtype.lower()
        if q.startswith("basic"):
            return self.basic()
        if q.startswith("complete"):
            return self.joined()
        if q.startswith("dedup"):
            return self.dedup()
        if q.startswith("incremental"):
            return self.incremental()
        if q.startswith("historical"):
            return self.historical()
        if "view" in q:
            return self.view()
        if "dbt" in q:
            return self.dbt()
        return self.joined()


def generate_sql(result: MappingResult, erp: ERPKnowledge, query_type: str, settings: dict | None = None) -> str:
    return SQLBuilder(result, erp, settings).build(query_type)


def generate_all(result: MappingResult, erp: ERPKnowledge, settings: dict | None = None) -> dict[str, str]:
    from config.settings import QUERY_TYPES
    b = SQLBuilder(result, erp, settings)
    out = {qt: b.build(qt) for qt in QUERY_TYPES}
    out["dbt sources.yml"] = b.dbt_sources_yml()
    return out
