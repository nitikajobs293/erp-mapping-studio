import sys, shutil, tempfile
from pathlib import Path
import pandas as pd
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.erp_knowledge_service import KnowledgeRepository
from services.mapping_engine import build_mapping
from services.rag_service import VectorStore
from services.sql_service import generate_sql
from services import llm_service
from services.llm_service import LLMConfig


def _repo(tmp):
    return KnowledgeRepository(custom_dir=Path(tmp) / "c", upload_dir=Path(tmp) / "u")


def test_metadata_upload_promotes_confidence():
    tmp = tempfile.mkdtemp()
    (Path(tmp) / "c").mkdir(); (Path(tmp) / "u").mkdir()
    r = _repo(tmp)
    before = build_mapping(r, "sap_s4hana", ["Tracking Number"], "shipment").rows[0]["_confidence"]
    dd = pd.DataFrame({"TABNAME": ["VTTK", "VTTK", "ZTRACK"], "FIELDNAME": ["EXTI1", "TKNUM", "ZTRACKNO"],
                       "DDTEXT": ["External ID 1", "Shipment", "Carrier tracking"], "KEYFLAG": ["", "X", ""]})
    dd = dd.rename(columns={"TABNAME": "table", "FIELDNAME": "field", "DDTEXT": "description", "KEYFLAG": "key"})
    assert r.save_uploaded_metadata("sap_s4hana", dd) == 3
    after = build_mapping(r, "sap_s4hana", ["Tracking Number"], "shipment").rows[0]
    assert before == "inferred" and after["_confidence"] == "uploaded", (before, after["_confidence"])
    assert r.get("sap_s4hana").find_table("ZTRACK")
    shutil.rmtree(tmp)


def test_custom_erp_and_extension():
    tmp = tempfile.mkdtemp()
    (Path(tmp) / "c").mkdir(); (Path(tmp) / "u").mkdir()
    r = _repo(tmp)
    r.add_custom_erp("epicor_kinetic", "Epicor Kinetic", "Epicor", "relational", ["2024"])
    r.add_custom_entry("epicor_kinetic", "tables", "OrderHed", {"description": "Order header", "primary_key": ["Company", "OrderNum"],
                       "confidence": "uploaded", "fields": {"OrderNum": {"description": "Order", "type": "INT"}}})
    r.add_custom_entry("epicor_kinetic", "tables", "OrderDtl", {"description": "Order line", "primary_key": ["Company", "OrderNum", "OrderLine"],
                       "confidence": "uploaded", "fields": {"PartNum": {"description": "Part", "type": "VARCHAR"}}})
    r.add_custom_entry("epicor_kinetic", "relationships", "", {"from": "OrderHed", "to": "OrderDtl", "on": [["Company", "Company"], ["OrderNum", "OrderNum"]],
                       "cardinality": "1:N", "confidence": "uploaded"})
    r.add_custom_entry("epicor_kinetic", "concepts", "sales_order_number", {"table": "OrderHed", "field": "OrderNum", "confidence": "uploaded"})
    r.add_custom_entry("epicor_kinetic", "concepts", "material", {"table": "OrderDtl", "field": "PartNum", "confidence": "uploaded"})
    r.add_custom_entry("epicor_kinetic", "business_objects", "sales_order", {"anchor": "OrderDtl", "header": "OrderHed", "grain": "line",
                       "business_key": ["Company", "OrderNum", "OrderLine"]})
    res = build_mapping(r, "epicor_kinetic", ["Sales Order", "Material"], "sales_order")
    sql = generate_sql(res, r.get("epicor_kinetic"), "Deduplicated query")
    assert "INNER JOIN src_orderhed" in sql and "ORDERNUM" in sql.upper()
    shutil.rmtree(tmp)


def test_rag_retrieval():
    tmp = tempfile.mkdtemp()
    s = VectorStore(Path(tmp))
    s.add_document("sap_s4hana", "notes.md", b"Our carrier tracking number is stored in VTTK-EXTI2 for parcel shipments.\n\nOther text about invoices.")
    hits = s.search("where is the tracking number", "sap_s4hana")
    assert hits and "EXTI2" in hits[0]["text"]
    assert not s.search("tracking number", "jde_e1")
    shutil.rmtree(tmp)


def test_ai_rows_and_mock_llm(monkeypatch):
    r = KnowledgeRepository()
    extra = [{"column": "Pallet Count", "table": "VEKP", "field": "BRGEW", "confidence": "ai", "note": "AI test"}]
    res = build_mapping(r, "sap_s4hana", ["Delivery Number"], "delivery", extra_rows=extra)
    ai = [x for x in res.rows if x["Business Column"] == "Pallet Count"][0]
    assert ai["_confidence"] == "ai" and ai["_sql"]
    monkeypatch.setattr(llm_service, "stream_chat", lambda cfg, m, c="": iter(['[{"column":"X","table":"likp","field":"VSBED","rationale":"r","evidence":"Inferred"}]']))
    out = llm_service.suggest_mappings(LLMConfig(provider="anthropic", api_key="k", model="m"), "SAP S/4HANA", "Delivery", ["X"], "")
    assert out[0]["table"] == "LIKP" and out[0]["confidence"] == "ai"
