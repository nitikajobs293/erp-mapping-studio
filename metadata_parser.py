"""Parse source metadata, target requirements and existing mapping files.

Accepted inputs: CSV, TSV, Excel (all sheets), JSON, and pasted text (comma, tab,
pipe or semicolon delimited, with a header row). Header names are normalised via
alias lists, so SAP DD03L/DD03M extracts, INFORMATION_SCHEMA.COLUMNS extracts and
the app's own templates all load without reformatting.
"""
from __future__ import annotations

import io
import json
import re
from typing import Any

import pandas as pd

from models import SOURCE_METADATA_FIELDS, TARGET_FIELDS
from utils.text_utils import clean_str, to_bool

# --------------------------------------------------------------------------- aliases
SOURCE_ALIASES = {
    "erp_name": ["erp", "erp_name", "source_erp", "source_system", "system", "erp_system"],
    "schema_name": ["schema", "schema_name", "table_schema", "source_schema", "owner", "database_schema"],
    "table_name": ["table", "table_name", "tabname", "source_table", "object_name", "entity"],
    "table_description": ["table_description", "table_desc", "table_text", "ddtext_table", "table_comment"],
    "column_name": ["column", "column_name", "fieldname", "field", "field_name", "source_column", "attribute"],
    "data_type": ["data_type", "datatype", "type", "rollname_type", "inttype", "column_type", "data_type_name"],
    "column_description": ["column_description", "description", "ddtext", "scrtext_l", "field_description",
                           "comment", "column_comment", "field_text", "short_description"],
    "is_primary_key": ["is_primary_key", "primary_key", "pk", "keyflag", "key", "pk_indicator", "is_key"],
    "is_foreign_key": ["is_foreign_key", "foreign_key", "fk", "fk_indicator"],
    "references": ["references", "references_to", "fk_reference", "referenced", "references_column",
                   "parent_column", "foreign_key_reference", "fk_references"],
    "business_description": ["business_description", "business_desc", "business_definition_source",
                             "business_meaning", "notes"],
}
# DD03L CHECKTABLE gives a table only; kept separately and turned into a hint.
CHECKTABLE_ALIASES = ["checktable", "check_table", "references_table", "referenced_table", "parent_table"]

TARGET_ALIASES = {
    "target_column": ["target_column", "target_column_name", "target", "target_field", "column", "column_name"],
    "target_data_type": ["target_data_type", "target_type", "data_type", "datatype", "type"],
    "business_definition": ["business_definition", "definition", "description", "business_description"],
    "mandatory": ["mandatory", "mandatory_indicator", "required", "is_mandatory", "not_null", "nullable_flag"],
    "expected_transformation": ["expected_transformation", "transformation", "transformation_logic",
                                "transformation_rule", "logic"],
    "business_rules": ["business_rules", "business_rule", "rules", "rule"],
    "source_hint": ["source_hint", "source", "source_reference", "source_table_column", "expected_source"],
}

S2T_REQUIRED = {"target_column", "source_table", "source_column"}


def _norm_header(h: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(h).strip().lower()).strip("_")


def _rename(frame: pd.DataFrame, aliases: dict[str, list[str]]) -> pd.DataFrame:
    frame = frame.copy()
    frame.columns = [_norm_header(c) for c in frame.columns]
    mapping: dict[str, str] = {}
    for canonical, names in aliases.items():
        for n in names:
            if n in frame.columns and canonical not in mapping.values() and n not in mapping:
                mapping[n] = canonical
                break
    return frame.rename(columns=mapping)


# --------------------------------------------------------------------------- readers
def read_any(file_name: str, content: bytes | str) -> list[pd.DataFrame]:
    """Read an uploaded file (or pasted text) into one or more DataFrames."""
    name = (file_name or "").lower()
    if isinstance(content, str):
        content_bytes = content.encode("utf-8")
    else:
        content_bytes = content
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        sheets = pd.read_excel(io.BytesIO(content_bytes), sheet_name=None, dtype=str)
        return [s for s in sheets.values() if not s.empty]
    if name.endswith(".json"):
        data = json.loads(content_bytes.decode("utf-8-sig"))
        if isinstance(data, dict):
            for key in ("source_metadata", "columns", "metadata", "data", "records"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        return [pd.DataFrame(data).astype(str)]
    text = content_bytes.decode("utf-8-sig", errors="replace")
    return [parse_delimited_text(text)]


def parse_delimited_text(text: str) -> pd.DataFrame:
    text = text.strip()
    if not text:
        return pd.DataFrame()
    first = text.splitlines()[0]
    counts = {d: first.count(d) for d in ["\t", "|", ";", ","]}
    delim = max(counts, key=counts.get)
    if counts[delim] == 0:
        raise ValueError("Could not detect a delimiter. Paste rows with a header, separated by tab, comma, | or ;")
    frame = pd.read_csv(io.StringIO(text), sep=delim, dtype=str, skipinitialspace=True, engine="python")
    frame.columns = [c.strip() for c in frame.columns]
    return frame.apply(lambda s: s.str.strip() if s.dtype == object else s)


# --------------------------------------------------------------------------- source metadata
def normalize_source_metadata(frame: pd.DataFrame, default_erp: str = "", default_schema: str = "") -> tuple[pd.DataFrame, list[str]]:
    """Return metadata in canonical columns + a list of warnings."""
    warnings: list[str] = []
    if frame is None or frame.empty:
        return pd.DataFrame(columns=SOURCE_METADATA_FIELDS), ["No rows found."]
    f = _rename(frame, SOURCE_ALIASES)
    length_col = next((c for c in ("leng", "length", "character_maximum_length", "data_length") if c in f.columns), None)
    if length_col and "data_type" in f.columns:
        lens = f[length_col].fillna("").map(clean_str).str.replace(r"\.0$", "", regex=True)
        add = (lens != "") & ~f["data_type"].fillna("").str.contains(r"\(")
        f.loc[add, "data_type"] = f.loc[add, "data_type"] + "(" + lens[add] + ")"
    check_col = next((c for c in CHECKTABLE_ALIASES if c in f.columns), None)

    missing_core = [c for c in ("table_name", "column_name") if c not in f.columns]
    if missing_core:
        raise ValueError(f"Metadata is missing required column(s): {', '.join(missing_core)}. "
                         f"Found: {', '.join(frame.columns)}")
    for col in SOURCE_METADATA_FIELDS:
        if col not in f.columns:
            f[col] = ""
    f = f.fillna("")
    for col in SOURCE_METADATA_FIELDS:
        f[col] = f[col].map(clean_str)

    if default_erp:
        f.loc[f["erp_name"] == "", "erp_name"] = default_erp
    if default_schema:
        f.loc[f["schema_name"] == "", "schema_name"] = default_schema
    if (f["erp_name"] == "").any():
        warnings.append(f"{(f['erp_name'] == '').sum()} row(s) have no ERP name; select a default ERP when importing.")

    f["table_name"] = f["table_name"].str.upper()
    f["column_name"] = f["column_name"].map(lambda c: c.upper() if re.match(r"^[A-Za-z0-9_]+$", c) else c)
    f["is_primary_key"] = f["is_primary_key"].map(to_bool)
    f["is_foreign_key"] = f["is_foreign_key"].map(to_bool)

    # A DD03L-style check table becomes a *hint* in business_description, never a join.
    if check_col:
        ct = frame.rename(columns=lambda c: _norm_header(c))[check_col].fillna("").map(clean_str)
        has = (ct != "") & (f["references"] == "")
        f.loc[has, "business_description"] = (f.loc[has, "business_description"] + " [Check table: " + ct[has] + "]").str.strip()
        if has.any():
            warnings.append(f"{int(has.sum())} column(s) have a check table but no column-level reference; "
                            "these are kept as hints and will NOT generate confirmed joins.")

    # references: normalise "TABLE.COLUMN; TABLE2.COLUMN"
    def _norm_ref(r: str) -> str:
        parts = [p.strip() for p in re.split(r"[;\n]", r) if p.strip()]
        out = []
        for p in parts:
            if "." in p:
                t, c = p.rsplit(".", 1)
                out.append(f"{t.strip().upper()}.{c.strip().upper()}")
            else:
                warnings.append(f"Reference '{p}' is not in TABLE.COLUMN form and was ignored.")
        return ";".join(out)

    f["references"] = f["references"].map(_norm_ref)
    f.loc[f["references"] != "", "is_foreign_key"] = True
    fk_no_ref = f["is_foreign_key"] & (f["references"] == "")
    if fk_no_ref.any():
        warnings.append(f"{int(fk_no_ref.sum())} FK-flagged column(s) have no reference target; "
                        "joins for them will be suggestions only.")

    # Table description: fill down within table if provided once
    f["table_description"] = f.groupby(["erp_name", "table_name"])["table_description"].transform(
        lambda s: s.replace("", pd.NA).ffill().bfill().fillna(""))

    before = len(f)
    f = f.drop_duplicates(subset=["erp_name", "table_name", "column_name"], keep="last")
    if len(f) < before:
        warnings.append(f"Removed {before - len(f)} duplicate column definition(s).")
    f = f[f["column_name"] != ""]
    return f[SOURCE_METADATA_FIELDS].reset_index(drop=True), warnings


def merge_metadata(existing: list[dict[str, Any]], new: pd.DataFrame) -> list[dict[str, Any]]:
    old = pd.DataFrame(existing) if existing else pd.DataFrame(columns=SOURCE_METADATA_FIELDS)
    combined = pd.concat([old, new], ignore_index=True)
    combined = combined.drop_duplicates(subset=["erp_name", "table_name", "column_name"], keep="last")
    return combined[SOURCE_METADATA_FIELDS].to_dict(orient="records")


# --------------------------------------------------------------------------- targets
def normalize_targets(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    if frame is None or frame.empty:
        return pd.DataFrame(columns=TARGET_FIELDS), ["No rows found."]
    f = _rename(frame, TARGET_ALIASES)
    if "target_column" not in f.columns:
        raise ValueError("Target file needs a 'Target Column' header.")
    for col in TARGET_FIELDS:
        if col not in f.columns:
            f[col] = ""
    f = f.fillna("")
    for col in TARGET_FIELDS:
        f[col] = f[col].map(clean_str)
    f["target_column"] = f["target_column"].str.upper().str.replace(r"[^A-Z0-9_]", "_", regex=True)
    f["mandatory"] = f["mandatory"].map(to_bool)
    f.loc[f["target_data_type"] == "", "target_data_type"] = "VARCHAR"
    f = f[f["target_column"] != ""].drop_duplicates(subset=["target_column"], keep="last")
    return f[TARGET_FIELDS].reset_index(drop=True), warnings


# --------------------------------------------------------------------------- S2T files
def detect_kind(frame: pd.DataFrame) -> str:
    cols = {_norm_header(c) for c in frame.columns}
    if {"target_column", "source_column"} <= cols or {"target_column", "source_table"} <= cols:
        return "source_to_target"
    if "target_column" in cols or "target_column_name" in cols:
        return "target"
    return "source_metadata"


def parse_s2t(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Existing source-to-target file -> target requirements with source hints.

    Hints from a user-supplied mapping document are treated as user-confirmed
    sources, but are still checked against metadata by the mapping engine.
    """
    f = frame.copy()
    f.columns = [_norm_header(c) for c in f.columns]
    if not S2T_REQUIRED <= set(f.columns):
        raise ValueError("Source-to-target files need Target Column, Source Table and Source Column headers.")
    f["source_hint"] = f.apply(
        lambda r: ";".join(f"{clean_str(r['source_table']).upper()}.{c.strip().upper()}"
                           for c in str(r["source_column"]).replace(",", ";").split(";")
                           if c.strip() and c.strip().lower() != "nan"), axis=1)
    return normalize_targets(f.drop(columns=["source_table", "source_column"]))
