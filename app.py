"""ERP Data Modelling Intelligence Platform — Streamlit entry point (login, RBAC navigation, workflow)."""
from __future__ import annotations

import time

import streamlit as st
from sqlalchemy import func, select

from erpdm.config import get_settings
from erpdm.db.base import session_scope
from erpdm.db.init_db import init_database
from erpdm.db.models import User
from erpdm.logging_setup import configure_logging, get_logger
from erpdm.services import auth
from erpdm.services.metadata import list_erps
from ui import components as ui
from ui.pages import (assistant, dashboard, dataflow, dbt_page, erp_explorer, mapping_page, metadata_mgmt,
                      relationships_page, settings_page, sql_page, tcode_page, usecase_page)

settings = get_settings()
st.set_page_config(page_title=settings.app_name, page_icon="🧭", layout="wide", initial_sidebar_state="expanded")
configure_logging()
log = get_logger("app")

st.markdown("""
<style>
  .block-container {padding-top: 1.6rem; max-width: 1500px;}
  [data-testid="stMetricValue"] {font-size: 1.6rem;}
  [data-testid="stSidebar"] {background: #f5f7fa;}
  .small-muted {color:#6b7280; font-size:0.85rem;}
</style>""", unsafe_allow_html=True)


@st.cache_resource(show_spinner="Initialising database…")
def _bootstrap() -> list[str]:
    return init_database()


PAGES = [
    ("Dashboard", "📊", dashboard.render, "view"),
    ("ERP Explorer", "🔎", erp_explorer.render, "view"),
    ("T-Code Intelligence", "🧾", tcode_page.render, "view"),
    ("Business Use Case Explorer", "💡", usecase_page.render, "view"),
    ("Table Relationship Explorer", "🔗", relationships_page.render, "view"),
    ("SQL Generator", "🧮", sql_page.render, "view"),
    ("Column Mapping", "🗂️", mapping_page.render, "view"),
    ("dbt Model Generator", "🏗️", dbt_page.render, "view"),
    ("Data Flow Explorer", "🔀", dataflow.render, "view"),
    ("AI ERP Assistant", "🤖", assistant.render, "use_assistant"),
    ("Metadata Management", "🛠️", metadata_mgmt.render, "manage_metadata"),
    ("Settings", "⚙️", settings_page.render, "view"),
]


def login_screen(notices: list[str]) -> None:
    st.title("🧭 " + settings.app_name)
    st.caption("ERP metadata discovery · join intelligence · SQL · mapping · dbt")
    with session_scope() as s:
        user_count = s.scalar(select(func.count()).select_from(User)) or 0
    if user_count == 0:
        st.warning("No users exist yet. " + (notices[0] if notices else ""))
        if settings.app_env == "production":
            st.stop()
        st.subheader("Create the first administrator")
        with st.form("first_admin"):
            u = st.text_input("Username")
            e = st.text_input("Email")
            p1 = st.text_input("Password", type="password")
            p2 = st.text_input("Confirm password", type="password")
            if st.form_submit_button("Create administrator", type="primary"):
                if p1 != p2:
                    st.error("Passwords do not match.")
                else:
                    try:
                        with session_scope() as s:
                            if (s.scalar(select(func.count()).select_from(User)) or 0) == 0:
                                auth.create_user(s, u, p1, "admin", email=e, full_name="Administrator", actor="first-run")
                        st.success("Administrator created — please sign in.")
                        time.sleep(0.8)
                        st.rerun()
                    except auth.AuthError as ex:
                        st.error(str(ex))
        return
    col, _ = st.columns([1, 1.4])
    with col:
        with st.form("login"):
            st.subheader("Sign in")
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in", type="primary", width="stretch"):
                try:
                    with session_scope() as s:
                        st.session_state["user"] = auth.authenticate(s, username, password)
                    st.session_state["last_activity"] = time.time()
                    st.rerun()
                except auth.AuthError as e:
                    st.error(str(e))


def session_expired() -> bool:
    last = st.session_state.get("last_activity", 0)
    if time.time() - last > settings.session_timeout_minutes * 60:
        return True
    st.session_state["last_activity"] = time.time()
    return False


def sidebar() -> str:
    u = ui.user()
    w = ui.wf()
    with st.sidebar:
        st.markdown(f"### 🧭 ERP Modelling\n<span class='small-muted'>{u.full_name or u.username} · {u.role}</span>",
                    unsafe_allow_html=True)
        with session_scope() as s:
            erps = [(e.code, e.name) for e in list_erps(s)]
        codes = [c for c, _ in erps]
        names = dict(erps)
        current = w.erp_code if w.erp_code in codes else None
        choice = st.selectbox("ERP system", codes, index=codes.index(current) if current else None,
                              format_func=lambda c: names.get(c, c), placeholder="Select ERP…")
        if choice and choice != w.erp_code:
            w.reset_for_erp(choice)
            w.erp_code = choice
        allowed = [p for p in PAGES if u.can(p[3])]
        labels = [f"{icon} {name}" for name, icon, _, _ in allowed]
        if st.session_state.get("nav_radio") not in labels:
            st.session_state["nav_radio"] = labels[0]
        page = st.radio("Navigation", labels, key="nav_radio", label_visibility="collapsed")
        st.divider()
        st.caption("Workflow progress")
        for label, done in w.steps():
            st.markdown(f"{'✅' if done else '▫️'} <span class='small-muted'>{label}</span>", unsafe_allow_html=True)
        if st.button("Reset workflow", width="stretch"):
            code = w.erp_code
            st.session_state["wf"] = type(w)(erp_code=code)
            st.rerun()
        if st.button("Sign out", width="stretch"):
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.rerun()
    return page


def main() -> None:
    notices = _bootstrap()
    if "user" not in st.session_state:
        login_screen(notices)
        return
    if session_expired():
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.warning("Your session expired. Please sign in again.")
        login_screen(notices)
        return
    if ui.user().must_change_password:
        st.warning("Please change your password before continuing.")
        settings_page.change_password_form()
        return
    page = sidebar()
    name = page.split(" ", 1)[1]
    render = next(fn for n, _, fn, _ in PAGES if n == name)
    ui.guarded(render)()


main()
