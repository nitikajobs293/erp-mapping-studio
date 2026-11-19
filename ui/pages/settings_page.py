from __future__ import annotations

import dataclasses

import streamlit as st
from sqlalchemy import select

from erpdm.config import get_settings
from erpdm.constants import ROLE_PERMISSIONS
from erpdm.db.base import get_engine
from erpdm.db.models import User
from erpdm.logging_setup import RECENT_ERRORS
from erpdm.services import auth
from erpdm.services.llm import LLMClient, LLMError
from ui import components as ui


def change_password_form() -> None:
    with st.form("pw"):
        old = st.text_input("Current password", type="password")
        new1 = st.text_input("New password", type="password")
        new2 = st.text_input("Confirm new password", type="password")
        if st.form_submit_button("Change password"):
            if new1 != new2:
                st.error("Passwords do not match.")
                return
            try:
                with ui.db() as s:
                    auth.change_password(s, ui.user().id, old, new1)
                st.session_state["user"] = dataclasses.replace(ui.user(), must_change_password=False)
                st.success("Password changed.")
                st.rerun()
            except auth.AuthError as e:
                st.error(str(e))


def render() -> None:
    st.title("⚙️ Settings")
    s_ = get_settings()
    tabs = st.tabs(["Profile", "AI provider", "Users & roles", "System"])
    with tabs[0]:
        u = ui.user()
        st.markdown(f"**{u.username}** · role `{u.role}` · permissions: {', '.join(sorted(ROLE_PERMISSIONS[u.role]))}")
        change_password_form()
    with tabs[1]:
        llm = LLMClient()
        st.markdown(f"Provider: `{s_.llm_provider}` · model: `{llm.model}` · status: **{llm.describe()}**")
        st.caption("Configured only through environment variables (LLM_PROVIDER, LLM_MODEL, LLM_API_KEY, LLM_BASE_URL). "
                   "Keys are never stored in the database or shown in the UI.")
        if llm.enabled and st.button("Test connection"):
            try:
                r = llm.complete("Reply with the single word OK.", [{"role": "user", "content": "ping"}], max_tokens=5)
                st.success(f"{r.provider}:{r.model} answered in {r.latency_ms} ms → {r.text.strip()[:40]}")
            except LLMError as e:
                st.error(str(e))
    with tabs[2]:
        if not ui.can("manage_users"):
            st.info("Only administrators can manage users.")
        else:
            _users()
    with tabs[3]:
        eng = get_engine()
        st.markdown(f"- Environment: `{s_.app_env}`\n- Database: `{eng.dialect.name}` "
                    f"(`{eng.url.render_as_string(hide_password=True)}`)\n- Session timeout: {s_.session_timeout_minutes} min\n"
                    f"- Lockout: {s_.max_failed_logins} failures → {s_.lockout_minutes} min\n"
                    f"- Upload limit: {s_.max_upload_mb} MB")
        st.markdown("**Backup & recovery:** run `scripts/backup.sh` (pg_dump, custom format, retention) on a schedule and "
                    "`scripts/restore.sh <file>` to restore. Metadata packages can also be exported per ERP from "
                    "Metadata Management → Export package.")
        st.subheader("Recent errors (in-process monitor)")
        ui.df(list(RECENT_ERRORS)) if RECENT_ERRORS else st.caption("No errors recorded since start-up.")


def _users() -> None:
    me = ui.user()
    with ui.db() as s:
        users = list(s.scalars(select(User).order_by(User.username)))
        ui.df([{"id": x.id, "Username": x.username, "Name": x.full_name, "Email": x.email, "Role": x.role,
                "Active": x.is_active, "Last login": x.last_login.strftime("%Y-%m-%d %H:%M") if x.last_login else "",
                "Locked until": x.locked_until} for x in users])
        with st.expander("Create user"):
            with st.form("new_user"):
                c1, c2 = st.columns(2)
                un = c1.text_input("Username")
                role = c2.selectbox("Role", list(ROLE_PERMISSIONS))
                fn = c1.text_input("Full name")
                em = c2.text_input("Email")
                pw = st.text_input("Temporary password", type="password")
                if st.form_submit_button("Create"):
                    try:
                        auth.create_user(s, un, pw, role, em or None, fn or None, me.username, must_change_password=True)
                        st.success("User created; they must change the password at first sign-in.")
                    except auth.AuthError as e:
                        st.error(str(e))
        uid = st.selectbox("Manage user", [x.id for x in users], format_func=lambda i: next(x.username for x in users if x.id == i))
        target = next(x for x in users if x.id == uid)
        c1, c2, c3 = st.columns(3)
        new_role = c1.selectbox("Role", list(ROLE_PERMISSIONS), index=list(ROLE_PERMISSIONS).index(target.role), key="role_sel")
        if c1.button("Update role"):
            try:
                auth.set_role(s, uid, new_role, me.username)
                st.success("Role updated.")
            except auth.AuthError as e:
                st.error(str(e))
        if c2.button("Deactivate" if target.is_active else "Activate"):
            try:
                auth.set_active(s, uid, not target.is_active, me.username)
                st.success("Updated.")
            except auth.AuthError as e:
                st.error(str(e))
        pw = c3.text_input("Reset password to", type="password", key="reset_pw")
        if c3.button("Reset password") and pw:
            try:
                auth.reset_password(s, uid, pw, me.username)
                st.success("Password reset; user must change it at next sign-in.")
            except auth.AuthError as e:
                st.error(str(e))
