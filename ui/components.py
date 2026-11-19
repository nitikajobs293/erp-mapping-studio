"""Shared Streamlit helpers: session access, RBAC guards, provenance badges, workflow state."""
from __future__ import annotations

import traceback
from contextlib import contextmanager
from typing import Iterator

import pandas as pd
import streamlit as st
from sqlalchemy.orm import Session

from erpdm.config import get_settings
from erpdm.constants import SOURCE_LABELS, SourceType
from erpdm.db.base import session_scope
from erpdm.logging_setup import get_logger
from erpdm.services.auth import AuthenticatedUser
from erpdm.workflow import WorkflowState

log = get_logger("ui")

STATUS_ICON = {"verified": "🟢", "requires_validation": "🟠", "pending": "🔴", "rejected": "⚫"}


_CONTROL_FLOW = {"RerunException", "StopException"}


@contextmanager
def db() -> Iterator[Session]:
    """Transactional session for UI code. st.rerun()/st.stop() raise control-flow exceptions; work done before
    them must still be committed, so those are treated as success."""
    from erpdm.db.base import get_session_factory
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except BaseException as e:  # noqa: BLE001
        if type(e).__name__ in _CONTROL_FLOW:
            session.commit()
        else:
            session.rollback()
        raise
    finally:
        session.close()


def user() -> AuthenticatedUser:
    return st.session_state["user"]


def can(permission: str) -> bool:
    u = st.session_state.get("user")
    return bool(u and u.can(permission))


def require(permission: str) -> bool:
    if not can(permission):
        st.warning("You do not have permission for this action. Ask an administrator.")
        return False
    return True


def wf() -> WorkflowState:
    if "wf" not in st.session_state:
        st.session_state["wf"] = WorkflowState()
    return st.session_state["wf"]


def status_badge(status: str | None, confidence: float | None = None) -> str:
    icon = STATUS_ICON.get(status or "", "⚪")
    conf = f" · {confidence:.2f}" if confidence is not None else ""
    return f"{icon} {(status or 'unknown').replace('_', ' ')}{conf}"


def provenance_legend() -> None:
    with st.expander("How to read provenance and validation labels", expanded=False):
        st.markdown(
            "| Status | Meaning |\n|---|---|\n"
            "| 🟢 verified | Confirmed by DDL/DDIC constraints or approved by a user |\n"
            "| 🟠 requires validation | Standard ERP knowledge or documentation — check against your system |\n"
            "| 🔴 pending | AI-inferred or parsed from SQL — must be approved before generators use it by default |\n"
            "| ⚫ rejected | Never used |\n\n"
            "**Sources:** " + ", ".join(f"`{k.value}` = {v}" for k, v in SOURCE_LABELS.items()))


def guarded(fn):
    """Render a page; show friendly errors instead of stack traces, and log them."""
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            log.error("page error in %s: %s\n%s", fn.__module__, e, traceback.format_exc())
            st.error(f"Something went wrong: {e}")
            if get_settings().app_env != "production":
                with st.expander("Technical details"):
                    st.code(traceback.format_exc())
    return wrapper


def df(rows, **kw) -> None:
    frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    st.dataframe(frame, width="stretch", hide_index=True, **kw)


def need_erp() -> str | None:
    code = wf().erp_code
    if not code:
        st.info("Select an ERP system in the sidebar to begin.")
    return code


def workflow_banner() -> None:
    w = wf()
    bits = [f"**ERP** {w.erp_code or '—'}"]
    if w.tcode:
        bits.append(f"**T-Code** {w.tcode}")
    if w.use_case_name:
        bits.append(f"**Use case** {w.use_case_name}")
    if w.tables:
        bits.append(f"**Tables** {', '.join(w.tables[:8])}{'…' if len(w.tables) > 8 else ''}")
    if w.columns:
        bits.append(f"**Columns** {len(w.columns)}")
    st.caption("Workflow → " + " · ".join(bits))


def graphviz(dot: str, key: str | None = None) -> None:
    try:
        st.graphviz_chart(dot, width="stretch")
    except Exception as e:  # noqa: BLE001
        st.warning(f"Diagram could not be rendered ({e}). DOT source:")
        st.code(dot, language="dot")


def trusted_label(source_type: str) -> str:
    try:
        return SOURCE_LABELS[SourceType(source_type)]
    except ValueError:
        return source_type
