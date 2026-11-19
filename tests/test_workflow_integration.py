"""End-to-end: SAP NSAP · IQ09 → SQL → mapping → approval → model → dbt → documents (spec §9)."""
import io
import zipfile

import httpx
import pandas as pd
import yaml

from erpdm.services import docgen
from erpdm.services.assistant import answer, unknown_identifiers
from erpdm.services.dbtgen import DbtOptions, generate_dbt_project
from erpdm.services.diagrams import er_diagram_dot, render_png
from erpdm.services.llm import LLMClient
from erpdm.services.mapping import (approve_mapping, create_mapping, mapping_dataframe, resolve_business_columns,
                                    selected_columns_from_mapping, update_mapping_rows)
from erpdm.services.modelling import recommend_model
from erpdm.services.tcode import analyze_transaction
from erpdm.services.usecase import analyze_requirement

BUSINESS = ["Equipment Number", "Serial Number", "Material Number", "Material Description", "Plant",
            "Equipment Category", "Manufacturer"]


def test_iq09_end_to_end(session, nsap):
    a = analyze_transaction(session, nsap, "IQ09")
    assert a and a.plan.root_table == "EQUI"
    assert {"EQUI", "EQBS", "MAKT"} <= set(a.plan.tables)
    assert "ASSUMED" in a.verification[0][1]
    assert "requires_validation" in a.sql.sql or "not verified" in a.sql.sql

    matches = resolve_business_columns(session, nsap, BUSINESS, [t.name for t in a.tables])
    got = {m.business_column: (m.table, m.column) for m in matches}
    assert got["Serial Number"] == ("EQUI", "SERNR")
    assert got["Material Description"] == ("MAKT", "MAKTX")
    assert got["Manufacturer"] == ("EQUI", "HERST")
    assert got["Plant"] == ("EQBS", "B_WERK")

    m = create_mapping(session, nsap, BUSINESS, "modeller", context_tables=[t.name for t in a.tables],
                       root_table="EQUI", transaction_code="IQ09")
    df = mapping_dataframe(m)
    assert len(df) == 7 and df["Source Column"].notna().all()
    df.loc[df["Business Column"] == "Manufacturer", "Business Definition"] = "OEM as entered on equipment"
    assert update_mapping_rows(session, m.id, df, "modeller") >= 1
    approve_mapping(session, m.id, "admin")
    assert m.status == "approved" and all(r.validation_status == "verified" for r in m.rows)

    cols = selected_columns_from_mapping(m)
    rec = recommend_model(session, nsap, a.plan, cols)
    assert rec.models[0].name == "dim_equipment"
    proj = generate_dbt_project(session, nsap, a.plan, cols, rec, DbtOptions(cdc_deleted_column="_deleted"))
    files = proj.files
    assert "models/marts/dim_equipment.sql" in files
    assert any(p.startswith("models/staging/sap_nsap/stg_sap_nsap__equi") for p in files)
    yml = yaml.safe_load(files["models/marts/_marts__models.yml"])
    dim = next(x for x in yml["models"] if x["name"] == "dim_equipment")
    assert {"unique", "not_null"} <= set(dim["columns"][0]["tests"])
    assert "status_flags" in files["models/intermediate/int_equipment__joined.sql"]  # JEST aggregated, not joined raw
    z = zipfile.ZipFile(io.BytesIO(proj.to_zip()))
    assert "dbt_project.yml" in z.namelist()

    xlsx = docgen.mapping_excel({"ERP": nsap.name}, df.drop(columns=["id"]), a.relationships, [], a.sql.sql,
                                a.sql.validation_queries)
    assert pd.read_excel(io.BytesIO(xlsx), sheet_name="Mapping").shape[0] == 7
    png = render_png(er_diagram_dot(session, nsap, a.plan.tables, a.plan))
    secs = docgen.build_design_sections(erp_name=nsap.name, tcode="IQ09", use_case=None, plan=a.plan, gen_sql=a.sql,
                                        rec=rec, mapping_df=df, relationships_df=None, tables_df=None, dbt_files=list(files))
    assert docgen.technical_design_docx("TDD", {"ERP": nsap.name}, secs, png)[:2] == b"PK"
    assert docgen.technical_design_pdf("TDD", {"ERP": nsap.name}, secs, png)[:4] == b"%PDF"


def test_use_case_explorer(session, ecc):
    text = ("I want to build an Equipment Master Data Model that provides equipment details, serial numbers, "
            "material information, plant, location, equipment category, manufacturer and maintenance information.")
    u = analyze_requirement(session, text, ecc)
    assert u.use_case and u.use_case.name == "Equipment Master Data Model"
    assert u.plan and u.plan.root_table == "EQUI"
    assert any(m.table == "EQUI" and m.column == "SERNR" for m in u.matches)


def test_assistant_metadata_mode_and_not_found(session, ecc):
    llm = LLMClient()
    assert not llm.enabled
    a = answer(session, ecc, "How do I join VBAK, VBAP and LIKP?", llm=llm)
    assert a.mode == "metadata" and "LIPS" in a.text and "KNA1" not in a.text.split("### Relationships")[1][:600]
    nf = answer(session, ecc, "Which tables hold warranty campaign reimbursements?", llm=llm)
    assert nf.mode == "not_found"


def test_hallucination_guard(session, ecc):
    assert unknown_identifiers(session, ecc, "Join EQUI to ZFAKE_TABLE on EQUNR") == ["ZFAKE_TABLE"]


def test_llm_mode_with_mock_transport(session, ecc):
    from erpdm.config import Settings

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "test-key"
        body = request.read().decode()
        assert "[TABLE:EQUI]" in body  # grounded context was sent
        return httpx.Response(200, json={"content": [{"type": "text",
                                                      "text": "Join [TABLE:EQUI] to [TABLE:EQKT] and ZMADEUP."}]})

    s = Settings(LLM_PROVIDER="anthropic", LLM_API_KEY="test-key", LLM_MODEL="claude-test")
    llm = LLMClient(s, transport=httpx.MockTransport(handler))
    a = answer(session, ecc, "What is the relationship between EQUI and EQKT?", llm=llm)
    assert a.mode == "llm" and "ZMADEUP" in a.unverified_identifiers
