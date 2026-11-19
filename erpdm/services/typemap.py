"""Source (ERP) data type → Snowflake target type and staging cast expressions."""
from __future__ import annotations


def snowflake_type(data_type: str | None, length: int | None = None, decimals: int | None = None) -> str:
    t = (data_type or "").upper()
    if t in ("DATS",):
        return "DATE"
    if t in ("TIMS",):
        return "TIME"
    if t in ("DATE",):  # Oracle DATE carries time
        return "TIMESTAMP_NTZ"
    if t.startswith("TIMESTAMP"):
        return "TIMESTAMP_NTZ"
    if t in ("QUAN", "CURR", "DEC", "DECIMAL", "NUMERIC"):
        return f"NUMBER({length or 38},{decimals or 0})"
    if t in ("INT1", "INT2", "INT4", "INT8", "INT", "INTEGER", "BIGINT", "SMALLINT"):
        return "INTEGER"
    if t in ("FLTP", "FLOAT", "DOUBLE"):
        return "FLOAT"
    if t in ("NUMBER",):
        return f"NUMBER(38,{decimals})" if decimals is not None else "NUMBER"
    if t in ("CHAR", "NUMC", "LANG", "CUKY", "UNIT", "CLNT", "VARCHAR", "VARCHAR2", "NVARCHAR", "NCHAR", "STRING", "TEXT", "ACCP"):
        return f"VARCHAR({length})" if length else "VARCHAR"
    if t in ("BOOLEAN", "BOOL"):
        return "BOOLEAN"
    return "VARCHAR"


def staging_expression(col: str, data_type: str | None, length: int | None = None,
                       decimals: int | None = None, quote: bool = False) -> tuple[str, str]:
    """Return (dbt/Snowflake expression, human description of the conversion)."""
    c = f'"{col}"' if quote else col
    t = (data_type or "").upper()
    if t == "DATS":
        return (f"try_to_date(nullif(nullif(trim({c}), ''), '00000000'), 'YYYYMMDD')",
                "SAP DATS (YYYYMMDD string) → DATE; '00000000' and blanks → NULL")
    if t == "TIMS":
        return (f"try_to_time(nullif(trim({c}), ''), 'HH24MISS')", "SAP TIMS (HHMMSS) → TIME")
    if t in ("QUAN", "CURR", "DEC", "DECIMAL", "NUMERIC"):
        tgt = snowflake_type(t, length, decimals)
        return (f"cast({c} as {tgt.lower()})", f"Cast to {tgt}" + ("; currency amounts must be read with their currency key "
                "(SAP stores some currencies with shifted decimals, e.g. JPY — see TCURX)" if t == "CURR" else ""))
    if t == "NUMC":
        return (f"nullif(trim({c}), '')", "NUMC kept as text (leading zeros are significant for keys)")
    if t in ("CHAR", "LANG", "CUKY", "UNIT", "CLNT", "VARCHAR", "VARCHAR2", "NVARCHAR", "NCHAR", "STRING", "TEXT"):
        return (f"nullif(trim({c}), '')", "Trim; blank → NULL")
    if t == "DATE":
        return (f"cast({c} as timestamp_ntz)", "Oracle DATE → TIMESTAMP_NTZ (keeps time component)")
    if t in ("INT1", "INT2", "INT4", "INT8", "INT", "INTEGER", "BIGINT", "SMALLINT"):
        return (f"cast({c} as integer)", "Cast to INTEGER")
    if t == "NUMBER":
        return (f"cast({c} as number(38,{decimals or 0}))" if decimals else f"cast({c} as number)", "Cast to NUMBER")
    return (c, "Passed through")
