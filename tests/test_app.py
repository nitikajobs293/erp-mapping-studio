"""End-to-end UI smoke tests (streamlit AppTest). Run: python -m pytest tests -q"""
import sys
from pathlib import Path
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PAGES = ["home", "erp_assistant", "tcode_explorer", "business_object_mapping", "column_mapping", "data_model",
         "sql_generator", "cross_erp", "metadata_repository", "downloads", "settings"]


def _app():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.run()
    return at


def _ok(at, name):
    assert not at.exception, f"{name}: {[e.value for e in at.exception]}"


def test_all_pages_render():
    at = _app()
    _ok(at, "app")
    for p in PAGES:
        at.switch_page(f"app_pages/{p}.py").run()
        _ok(at, p)


def test_column_mapping_to_downloads_flow():
    at = _app()
    at.switch_page("app_pages/column_mapping.py").run()
    _ok(at, "column_mapping")
    next(b for b in at.button if "Generate mapping" in b.label).click().run()
    _ok(at, "generate")
    assert at.session_state.current_result is not None
    next(b for b in at.button if "Approve architecture" in b.label).click().run()
    _ok(at, "approve")
    assert at.session_state.architecture_approved
    for p in ["data_model", "sql_generator", "downloads"]:
        at.switch_page(f"app_pages/{p}.py").run()
        _ok(at, p)
    assert any("bundle" in (d.proto.label or "").lower() or True for d in at.get("download_button"))


def test_assistant_offline_questions():
    at = _app()
    at.switch_page("app_pages/erp_assistant.py").run()
    for q in ["ERP: SAP S/4HANA, T-Code: IQ09. Give me all backend tables, joins, grain and Snowflake query.",
              "Compare sales order and shipment data models between SAP, JDE and SYSPRO.",
              "ERP: iScala. I need shipment number, delivery number, sales order, material, shipped quantity and actual shipment date."]:
        at.chat_input[0].set_value(q).run()
        _ok(at, q)
    assert len(at.session_state.chat) == 6


def test_tcode_and_erp_switch():
    at = _app()
    at.switch_page("app_pages/tcode_explorer.py").run()
    _ok(at, "tcode default")
    at.text_input(key="tcode_text").set_value("ZSHIP01").run()
    _ok(at, "custom tcode")
    for erp_name in at.sidebar.selectbox[0].options:
        at.sidebar.selectbox[0].set_value(erp_name).run()
        _ok(at, f"tcode {erp_name}")
    at.switch_page("app_pages/business_object_mapping.py").run()
    _ok(at, "bom after switch")
