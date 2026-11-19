"""End-to-end and rule tests. Run:  pytest -q"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sqlglot  # noqa: E402

import export_manager as ex  # noqa: E402
from config import (EVIDENCE_AI, EVIDENCE_METADATA_FK, STATUS_CONFIRMED, STATUS_MISSING,  # noqa: E402
                    STATUS_REQUIRES_VALIDATION, STATUS_SUGGESTED)
from mapping_engine import apply_ai_result, regenerate_outputs, run_analysis  # noqa: E402
from metadata_parser import normalize_source_metadata, normalize_targets, parse_delimited_text, read_any  # noqa: E402
from models import MappingProject  # noqa: E402
from sample_data.build_sample_project import build  # noqa: E402

SAP = "SAP S/4HANA"


@pytest.fixture(scope="module")
def sample() -> MappingProject:
    return build()


def by_target(p, name, erp=SAP):
    return next(m for m in p.column_mappings if m["target_column"] == name and m["source_erp"] == erp)


def parses(sql: str) -> None:
    sqlglot.parse(sql, read="snowflake")


# ----------------------------------------------------------------------------- parsing
def test_dd03l_extract_is_normalised():
    f = SAMPLE = ROOT / "sample_data" / "sap_dd03l_style_extract.csv"
    meta, warns = normalize_source_metadata(read_any(f.name, f.read_bytes())[0], default_erp=SAP)
    assert set(meta["table_name"]) == {"MARA"}
    assert meta.loc[meta.column_name == "MATNR", "is_primary_key"].item() is True
    assert meta.loc[meta.column_name == "MATNR", "data_type"].item() == "CHAR(40)"
    # Check tables become hints only, never references/joins
    assert (meta["references"] == "").all()
    assert any("check table" in w for w in warns)


def test_pasted_tab_text():
    text = "Table Name\tColumn Name\tData Type\tPrimary Key\nLIKP\tVBELN\tCHAR(10)\tY\nLIKP\tLFART\tCHAR(4)\tN"
    meta, _ = normalize_source_metadata(parse_delimited_text(text), default_erp=SAP)
    assert list(meta["column_name"]) == ["VBELN", "LFART"]


def test_s2t_file_becomes_targets_with_hints():
    from metadata_parser import parse_s2t
    f = ROOT / "sample_data" / "source_to_target_template.csv"
    t, _ = parse_s2t(read_any(f.name, f.read_bytes())[0])
    assert t.loc[t.target_column == "DELIVERY_NUMBER", "source_hint"].item() == "LIPS.VBELN"


# ----------------------------------------------------------------------------- traceability rules
def test_joins_are_traceable(sample):
    for j in sample.joins:
        if j["status"] == STATUS_CONFIRMED:
            assert j["evidence"] in (EVIDENCE_METADATA_FK, "User confirmed"), j
        else:
            assert j["status"] == STATUS_REQUIRES_VALIDATION
    sql = sample.sql["integrated"]
    # every JOIN line is preceded by a status comment
    lines = sql.splitlines()
    for i, line in enumerate(lines):
        if " JOIN " in line and not line.strip().startswith("--"):
            context = "\n".join(lines[max(0, i - 3):i])
            assert "[CONFIRMED]" in context or "[REQUIRES VALIDATION]" in context


def test_declared_fk_joins_confirmed(sample):
    pairs = {(j["left_table"], j["right_table"]) for j in sample.joins if j["status"] == STATUS_CONFIRMED}
    assert {("LIPS", "LIKP"), ("VTTP", "LIKP"), ("VTTP", "VTTK")} <= pairs


def test_heuristic_join_requires_validation_and_is_flagged_in_sql(sample):
    j = next(j for j in sample.joins if {j["left_table"], j["right_table"]} == {"LIPS", "T001W"})
    assert j["status"] == STATUS_REQUIRES_VALIDATION
    assert "WARNING" in sample.sql["integrated"] and "src_t001w" in sample.sql["integrated"]
    assert by_target(sample, "PLANT_NAME")["status"] == STATUS_REQUIRES_VALIDATION


def test_no_shortcut_through_guessed_join(sample):
    # LIPS-VTTP share VBELN by name, but the confirmed LIPS->LIKP->VTTP path must be used.
    assert "ON likp.MANDT = vttp.MANDT" in sample.sql["integrated"]


def test_missing_target_not_invented(sample):
    m = by_target(sample, "TRACKING_NUMBER")
    assert m["status"] == STATUS_MISSING and not m["source_column"]
    assert "CAST(NULL AS VARCHAR(35)) AS TRACKING_NUMBER" in sample.sql["integrated"]
    assert any("TRACKING_NUMBER" in q["item"] for q in sample.open_questions)


def test_hint_confirmed_and_suggestions_scored(sample):
    assert by_target(sample, "DELIVERY_NUMBER")["status"] == STATUS_CONFIRMED
    m = by_target(sample, "SALES_ORGANIZATION")
    assert (m["source_table"], m["source_column"]) == ("LIKP", "VKORG")
    assert m["status"] == STATUS_SUGGESTED and 0 < m["confidence"] < 1


def test_grain_and_fanout(sample):
    g = sample.grain[SAP]
    assert g["grain_table"] == "LIPS" and g["grain_keys"] == ["MANDT", "VBELN", "POSNR"]
    assert "one_vttp" in sample.sql["integrated"]          # fan-out controlled
    assert "ROW_NUMBER() OVER (PARTITION BY m._GK_MANDT, m._GK_VBELN, m._GK_POSNR" in sample.sql["deduplication"]
    assert "SELECT DISTINCT" not in sample.sql["deduplication"]


def test_transformations(sample):
    assert "TRY_TO_DATE" in by_target(sample, "DELIVERY_DATE")["transformation_logic"]
    assert "LTRIM" in by_target(sample, "MATERIAL_NUMBER")["transformation_logic"]
    assert by_target(sample, "SOURCE_SYSTEM")["transformation_type"] == "Constant"
    assert by_target(sample, "LAST_CHANGED_DATE")["transformation_logic"].startswith("COALESCE(")


# ----------------------------------------------------------------------------- SQL validity
def test_all_sql_parses(sample):
    for k, sql in sample.sql.items():
        parses(sql)


@pytest.mark.parametrize("history", ["Full history (SCD Type 2 style)", "Snapshot as-of date"])
def test_history_modes_parse(sample, history):
    p = copy.deepcopy(sample)
    p.config["history_requirement"] = history
    regenerate_outputs(p)
    for sql in p.sql.values():
        parses(sql)
    if history.startswith("Full"):
        assert "EFFECTIVE_TO" in p.sql["deduplication"] and "IS_CURRENT" in p.sql["deduplication"]


def test_cross_erp_union():
    p = build()
    other = pd.DataFrame([
        {"erp_name": "TEST ERP", "schema_name": "T", "table_name": "SHIP_LINE", "table_description": "Delivery line",
         "column_name": c, "data_type": t, "column_description": d, "is_primary_key": pk, "is_foreign_key": False,
         "references": "", "business_description": ""}
        for c, t, d, pk in [("DLV_NO", "VARCHAR(20)", "Delivery number", True), ("LINE_NO", "NUMBER", "Delivery item line", True),
                            ("ITEM_CODE", "VARCHAR(30)", "Material number", False),
                            ("QTY_SHIPPED", "NUMBER(15,3)", "Delivered quantity", False),
                            ("UPDATED_AT", "TIMESTAMP_NTZ", "Last change timestamp", False)]])
    meta, _ = normalize_source_metadata(other)
    p.source_metadata += meta.to_dict("records")
    p.config.update({"mapping_type": "Cross ERP", "source_erps": [SAP, "TEST ERP"]})
    run_analysis(p)
    assert "UNION ALL" in p.sql["integrated"]
    assert "SOURCE_ERP_NAME" in p.sql["deduplication"]
    for sql in p.sql.values():
        parses(sql)
    m = by_target(p, "MATERIAL_NUMBER", "TEST ERP")
    assert m["source_column"] == "ITEM_CODE"


# ----------------------------------------------------------------------------- AI guardrails
def test_ai_hallucinations_rejected(sample):
    p = copy.deepcopy(sample)
    result = {
        "column_mappings": [
            {"target_column": "TRACKING_NUMBER", "source_table": "VEKP", "source_columns": ["SPE_IDENT"],
             "confidence": 0.9, "reasoning": "made up"},
            {"target_column": "CARRIER_ID", "source_table": "VTTK", "source_columns": ["TDLNR"],
             "confidence": 0.8, "reasoning": "forwarding agent"},
        ],
        "joins": [{"left_table": "LIPS", "left_columns": ["MANDT", "MATNR"], "right_table": "MARA",
                   "right_columns": ["MANDT", "MATNR"], "confidence": 0.9}],
    }
    rep = apply_ai_result(p, SAP, result)
    assert len(rep["rejected"]) == 2
    assert by_target(p, "TRACKING_NUMBER")["status"] == STATUS_MISSING
    cid = by_target(p, "CARRIER_ID")
    assert cid["evidence"] == EVIDENCE_AI and cid["status"] == STATUS_SUGGESTED
    assert not any(j["right_table"] == "MARA" for j in p.joins)


# ----------------------------------------------------------------------------- edits, versions, exports
def test_user_edit_regenerates_sql(sample):
    p = copy.deepcopy(sample)
    m = by_target(p, "TRACKING_NUMBER")
    m.update({"source_table": "VTTK", "source_column": "TDLNR", "status": STATUS_CONFIRMED, "evidence": "User confirmed"})
    regenerate_outputs(p)
    assert "vttk.TDLNR" in p.sql["integrated"].split("AS TRACKING_NUMBER")[0].splitlines()[-1]


def test_versions_round_trip(sample):
    p = copy.deepcopy(sample)
    v = p.save_version("before")
    p.column_mappings = []
    p.restore_version(v["version"])
    assert p.column_mappings
    q = MappingProject.from_json(p.to_json())
    assert len(q.versions) == len(p.versions)


def test_exports(sample, tmp_path):
    for ext, spec in ex.EXPORTS.items():
        data = spec["fn"](sample)
        assert len(data) > 500, ext
        (tmp_path / f"out.{ext}").write_bytes(data)
    import openpyxl
    wb = openpyxl.load_workbook(tmp_path / "out.xlsx")
    assert "Column Mapping" in wb.sheetnames
    assert json.loads((tmp_path / "out.json").read_text())["column_mappings"]
    assert (tmp_path / "out.pdf").read_bytes()[:4] == b"%PDF"


def test_user_source_not_in_metadata_is_missing_and_sql_safe(sample):
    p = copy.deepcopy(sample)
    m = by_target(p, "TRACKING_NUMBER")
    m.update({"source_table": "", "source_column": "K.TDLNR", "status": STATUS_CONFIRMED, "evidence": "User confirmed"})
    regenerate_outputs(p)
    assert by_target(p, "TRACKING_NUMBER")["status"] == STATUS_MISSING
    assert "SOURCE NOT IN METADATA: K.TDLNR" in p.sql["integrated"]
    assert "(k.TDLNR" not in p.sql["integrated"]
    parses(p.sql["integrated"])
