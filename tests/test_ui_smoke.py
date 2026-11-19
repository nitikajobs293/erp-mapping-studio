"""Streamlit AppTest smoke test: sign in, open every page, run the T-code → workflow hand-off."""
from pathlib import Path

import pytest

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture()
def admin(db_url):
    from erpdm.db.base import session_scope
    from erpdm.services.auth import AuthError, create_user
    with session_scope(db_url) as s:
        try:
            create_user(s, "uiadmin", "Str0ngEnough!", "admin")
        except AuthError:
            pass
    return "uiadmin", "Str0ngEnough!"


def test_all_pages_render(admin):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()
    at.text_input[0].input(admin[0])
    at.text_input[1].input(admin[1])
    at.button[0].click()
    at.run()
    assert not at.exception
    at.sidebar.selectbox[0].select("SAP_ECC")
    at.run()
    pages = at.sidebar.radio[0].options
    assert len(pages) == 12
    for p in pages:
        at.sidebar.radio[0].set_value(p)
        at.run()
        assert not at.exception, (p, [e.value for e in at.exception])
        assert not at.error, (p, [e.value for e in at.error])
    at.sidebar.radio[0].set_value(next(p for p in pages if "T-Code" in p))
    at.run()
    at.text_input[0].input("IQ09")
    at.run()
    next(b for b in at.button if b.label.startswith("➡️")).click()
    at.run()
    wf = at.session_state["wf"]
    assert wf.tcode == "IQ09" and wf.root_table == "EQUI" and wf.sql
