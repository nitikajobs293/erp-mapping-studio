"""Column-level transformation logic for Snowflake.

Builds a SQL expression per target column from source/target data types, ERP
conventions (e.g. SAP DATS dates) and keywords in the expected transformation.
Rows whose transformation type is 'Custom SQL' are never overwritten.
"""
from __future__ import annotations

import re
from typing import Any

from erp_master import get_erp
from utils.text_utils import clean_str, split_list, sql_ident

NUMERIC_SRC = {"QUAN", "CURR", "DEC", "NUMBER", "NUMERIC", "DECIMAL", "INT", "INT1", "INT2", "INT4", "INT8",
               "INTEGER", "BIGINT", "SMALLINT", "FLTP", "FLOAT", "DOUBLE", "REAL", "PREC"}
CHAR_SRC = {"CHAR", "VARCHAR", "STRING", "TEXT", "NVARCHAR", "NCHAR", "CLNT", "CUKY", "UNIT", "LANG", "NUMC",
            "SSTRING", "VARCHAR2"}
DATE_SRC = {"DATE", "DATS"}
TS_SRC = {"TIMESTAMP", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ", "DATETIME", "TIMESTAMPL"}

TRANSFORMATION_TYPES = ["Direct", "Type conversion", "Date conversion", "Derived", "Conditional (CASE)",
                        "Lookup", "Constant", "Custom SQL", "Unmapped"]


def base_type(dtype: str) -> str:
    return clean_str(dtype).upper().split("(")[0].strip()


def _len(dtype: str) -> int | None:
    m = re.search(r"\((\d+)", clean_str(dtype))
    return int(m.group(1)) if m else None


def source_refs(mapping: dict[str, Any]) -> list[tuple[str, str]]:
    """[(table, column)] for a mapping row. Items may be 'COL' or 'TABLE.COL'."""
    table = clean_str(mapping.get("source_table")).upper()
    out = []
    for item in split_list(mapping.get("source_column")):
        if "." in item:
            t, c = item.rsplit(".", 1)
            out.append((t.strip().upper(), c.strip()))
        elif table:
            out.append((table, item))
    return out


def convert(expr: str, col: str, src_type: str, tgt_type: str, erp_profile: dict[str, Any],
            hints: str, strip_zero_default: bool) -> tuple[str, str, str]:
    """Return (expression, transformation_type, explanation) for one column."""
    s, t = base_type(src_type), base_type(tgt_type) or "VARCHAR"
    h = hints.lower()
    tgt_full = clean_str(tgt_type).upper() or "VARCHAR"
    date_fmt = erp_profile.get("date_format", "ISO")

    if t == "DATE":
        if s == "DATS" or (date_fmt == "SAP_DATS" and s in CHAR_SRC and (_len(src_type) in (8, None))):
            return (f"TRY_TO_DATE(NULLIF(NULLIF(TRIM({expr}), '00000000'), ''), 'YYYYMMDD')", "Date conversion",
                    "SAP DATS CHAR(8) YYYYMMDD -> DATE; '00000000' and blanks become NULL.")
        if date_fmt == "JDE_JULIAN" and s in NUMERIC_SRC:
            return (f"CASE WHEN {expr} IS NULL OR {expr} = 0 THEN NULL "
                    f"ELSE DATEADD(day, MOD({expr}, 1000) - 1, DATE_FROM_PARTS(1900 + FLOOR({expr} / 1000), 1, 1)) END",
                    "Date conversion", "JDE Julian CYYDDD -> DATE.")
        if s in DATE_SRC:
            return expr, "Direct", "Already a DATE."
        if s in TS_SRC:
            return f"TO_DATE({expr})", "Date conversion", "Timestamp truncated to DATE."
        return f"TRY_TO_DATE({expr})", "Date conversion", "String -> DATE; invalid values become NULL."

    if t.startswith("TIMESTAMP") or t == "DATETIME":
        if s in TS_SRC:
            return expr, "Direct", "Already a timestamp."
        if s in DATE_SRC or s == "DATS":
            inner = (f"TRY_TO_DATE(NULLIF(TRIM({expr}), '00000000'), 'YYYYMMDD')" if s == "DATS" else expr)
            return f"CAST({inner} AS {tgt_full})", "Date conversion", "Date promoted to timestamp."
        return f"TRY_TO_TIMESTAMP_NTZ({expr})", "Date conversion", "String -> timestamp; invalid values become NULL."

    if t in {"NUMBER", "NUMERIC", "DECIMAL", "INT", "INTEGER", "BIGINT", "FLOAT", "DOUBLE", "REAL"}:
        if s in NUMERIC_SRC:
            return f"CAST({expr} AS {tgt_full})", "Type conversion", f"Numeric cast to {tgt_full}."
        return (f"TRY_CAST(NULLIF(TRIM({expr}), '') AS {tgt_full})", "Type conversion",
                f"String -> {tgt_full} with TRY_CAST; non-numeric values become NULL (see DQ checks).")

    if t == "BOOLEAN":
        if date_fmt == "SAP_DATS" and s in CHAR_SRC:
            return f"(TRIM({expr}) = 'X')", "Type conversion", "SAP flag 'X' -> TRUE, blank -> FALSE."
        return f"TRY_TO_BOOLEAN(TO_VARCHAR({expr}))", "Type conversion", "Value -> BOOLEAN."

    # Character targets
    strip_zero = ("leading zero" in h or "ltrim" in h or "alpha" in h or
                  (strip_zero_default and col.upper() in [c.upper() for c in erp_profile.get("leading_zero_columns", [])]))
    if s in NUMERIC_SRC or s in DATE_SRC or s in TS_SRC:
        out, ttype, why = f"TO_VARCHAR({expr})", "Type conversion", "Converted to text."
    else:
        out, ttype, why = f"NULLIF(TRIM({expr}), '')", "Direct", "Trimmed; blank strings become NULL."
    if strip_zero:
        out = f"NULLIF(LTRIM(TRIM({expr}), '0'), '')"
        ttype, why = "Derived", "Leading zeros removed (SAP ALPHA conversion style); all-zero values become NULL."
    if "upper" in h:
        out, ttype, why = f"UPPER({out})", "Derived", why + " Upper-cased."
    return out, ttype, why


def build_expression(mapping: dict[str, Any], aliases: dict[str, str], types: dict[tuple[str, str], str],
                     erp: str, strip_zero_default: bool = False) -> dict[str, str]:
    """Compute transformation_type / transformation_logic / explanation for a mapping row.

    aliases: table -> SQL alias. types: (table, column) -> source data type.
    """
    if mapping.get("transformation_type") == "Custom SQL" and clean_str(mapping.get("transformation_logic")):
        return {"transformation_type": "Custom SQL", "transformation_logic": mapping["transformation_logic"],
                "explanation": "User-supplied SQL expression (kept as entered)."}

    expected = clean_str(mapping.get("expected_transformation") or mapping.get("business_rule"))
    tgt_type = clean_str(mapping.get("target_data_type")) or "VARCHAR"
    refs = source_refs(mapping)
    profile = get_erp(erp)

    if not refs:
        lit = re.search(r"'([^']*)'", expected)
        if lit and re.search(r"constant|literal|hard[- ]?code|default|always|=", expected, re.I):
            return {"transformation_type": "Constant", "transformation_logic": f"CAST('{lit.group(1)}' AS {tgt_type})",
                    "explanation": f"Constant value '{lit.group(1)}' from target requirement."}
        return {"transformation_type": "Unmapped", "transformation_logic": f"CAST(NULL AS {tgt_type})",
                "explanation": "No source column - emitted as NULL and listed as an open question."}

    # Conditional logic written by the user in the requirement: qualify bare column names.
    if re.search(r"\bCASE\b", expected, re.I):
        sql = expected
        for t, c in refs:
            a = aliases.get(t, t.lower())
            sql = re.sub(rf"(?<![\w.]){re.escape(c)}(?!\w)", f"{a}.{sql_ident(c)}", sql)
        return {"transformation_type": "Conditional (CASE)", "transformation_logic": sql,
                "explanation": "CASE logic taken from the target requirement; columns qualified with table aliases."}

    parts, notes, ttypes = [], [], []
    for t, c in refs:
        a = aliases.get(t, t.lower())
        expr = f"{a}.{sql_ident(c)}"
        e, tt, why = convert(expr, c, types.get((t, c), ""), tgt_type, profile, expected, strip_zero_default)
        parts.append(e)
        notes.append(f"{t}.{c}: {why}")
        ttypes.append(tt)

    if len(parts) == 1:
        return {"transformation_type": ttypes[0], "transformation_logic": parts[0], "explanation": notes[0]}

    if re.search(r"concat|combine|\|\||join with", expected, re.I):
        sep = re.search(r"separator\s*'([^']*)'", expected, re.I)
        s = sep.group(1) if sep else "-"
        logic = f"CONCAT_WS('{s}', {', '.join(parts)})"
        why = "Concatenation of source columns."
    else:
        logic = f"COALESCE({', '.join(parts)})"
        why = "First non-NULL value in the listed order (COALESCE). Confirm the precedence rule."
    return {"transformation_type": "Derived", "transformation_logic": logic,
            "explanation": why + " " + " ".join(notes)}
