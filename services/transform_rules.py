"""ERP-type aware transformation rules used for SQL expressions and mapping documentation."""
from __future__ import annotations

import re

from services.erp_knowledge_service import PLACEHOLDER_RE, ERPKnowledge


def make_alias(table: str, used: set[str]) -> str:
    t = re.sub(r"[^A-Za-z0-9_]", "_", table).lower().strip("_")
    if len(t) > 10 and "_" in t:
        t = "".join(p[0] for p in t.split("_") if p)
    t = t or "t"
    if t[0].isdigit():
        t = "t_" + t
    base, i = t, 2
    while t in used:
        t = f"{base}{i}"
        i += 1
    used.add(t)
    return t


def quote_ident(name: str, case: str = "UPPER") -> str:
    """Return a Snowflake identifier. Upper-case simple names stay unquoted; others are quoted."""
    if name is None:
        return "NULL"
    if case == "UPPER":
        n = name.upper()
        return n if re.fullmatch(r"[A-Z_][A-Z0-9_$]*", n) else f'"{n}"'
    return name if re.fullmatch(r"[A-Z_][A-Z0-9_$]*", name) else f'"{name}"'


def column_ref(erp: ERPKnowledge, alias: str, field: str, case: str) -> str:
    variant_col = erp.meta.get("variant_column")
    if erp.storage == "api" and not variant_col:
        variant_col = "RAW"
    if erp.storage == "api":
        return f"{alias}.{variant_col}:{field}::VARCHAR"
    return f"{alias}.{quote_ident(field, case)}"


def date_conversion(field_type: str, col: str, date_mode: str) -> tuple[str, str]:
    """Return (sql, description) to convert an ERP-native date column into a Snowflake DATE."""
    ft = (field_type or "").upper()
    if ft == "DATS":
        if date_mode == "DATE":
            return col, "Already landed as DATE"
        return (f"TRY_TO_DATE(NULLIF(NULLIF(TRIM({col}), ''), '00000000'), 'YYYYMMDD')",
                "SAP DATS (YYYYMMDD) -> DATE; '00000000' / blank -> NULL")
    if ft == "JULIAN":
        return (f"CASE WHEN {col} IS NULL OR {col} = 0 THEN NULL "
                f"ELSE DATEADD(day, MOD({col}, 1000) - 1, DATE_FROM_PARTS(1900 + FLOOR({col} / 1000), 1, 1)) END",
                "JDE Julian CYYDDD -> DATE; 0 -> NULL")
    if ft == "YYYYMMDD_NUM":
        return (f"TRY_TO_DATE(NULLIF(TO_VARCHAR({col}), '0'), 'YYYYMMDD')",
                "Numeric YYYYMMDD -> DATE; 0 -> NULL")
    if ft in ("DATETIME", "TIMESTAMP"):
        return f"TO_DATE({col})", "Timestamp -> DATE"
    if ft == "TIMS":
        return f"TRY_TO_TIME(NULLIF(TRIM({col}), ''), 'HH24MISS')", "SAP TIMS (HHMMSS) -> TIME"
    return col, "Direct"


def is_quantity(field_type: str) -> bool:
    return (field_type or "").upper() in ("QUAN", "CURR", "DEC", "DECIMAL", "NUMBER") or \
        (field_type or "").upper().startswith(("NUMBER", "DECIMAL"))


def transform_for(erp: ERPKnowledge, field_type: str, target_type: str, col: str, date_mode: str) -> tuple[str, str, str, str]:
    """Return (sql_expression, transformation_text, null_handling, default_value)."""
    ft = (field_type or "").upper()
    tt = (target_type or "").upper()
    if tt == "DATE" or ft in ("DATS", "JULIAN", "YYYYMMDD_NUM"):
        sql, desc = date_conversion(ft, col, date_mode)
        if tt == "DATE" and ft not in ("DATS", "JULIAN", "YYYYMMDD_NUM", "DATETIME", "TIMESTAMP"):
            sql, desc = f"TRY_TO_DATE(TO_VARCHAR({col}))", "Cast to DATE (validate source format)"
        return sql, desc, "Empty / zero dates -> NULL", "NULL"
    if tt.startswith("NUMBER") and ft in ("QUAN", "CURR"):
        return (f"CAST({col} AS {tt})", "Cast to NUMBER; quantity in source unit - convert UoM in gold if needed",
                "NULL kept (COALESCE to 0 only in aggregations)", "0 in aggregates")
    if tt.startswith("NUMBER"):
        return col, "Direct (numeric)", "NULL kept", "-"
    if ft.startswith(("CHAR", "NUMC", "VARCHAR", "UNIT", "CUKY", "LANG")) or ft in ("", "VARIANT"):
        if ft.startswith("NUMC") or "NUMC" in ft:
            return (f"NULLIF(TRIM({col}), '')", "Keep as VARCHAR - NUMC keeps leading zeros (do not cast to number)",
                    "Blank -> NULL", "NULL")
        return f"NULLIF(TRIM({col}), '')", "Trim; blank -> NULL", "Blank -> NULL", "NULL"
    return col, "Direct", "NULL kept", "-"


def expression_tables(expr: str) -> list[str]:
    return list(dict.fromkeys(m.group(1) for m in PLACEHOLDER_RE.finditer(expr or "")))


def render_expression(expr: str, alias_map: dict[str, str], erp: ERPKnowledge, case: str) -> str:
    def repl(m):
        t, f = m.group(1), m.group(2)
        key = erp.find_table(t) or t
        alias = alias_map.get(key, key.lower())
        return column_ref(erp, alias, f, case)
    return PLACEHOLDER_RE.sub(repl, expr)
