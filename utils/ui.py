"""Streamlit UI helpers: styling, status badges, editable tables with write-back."""
from __future__ import annotations

from typing import Any, Callable

import pandas as pd
import streamlit as st

from config import STATUS_ICONS

CSS = """
<style>
.block-container {padding-top: 3.2rem; padding-bottom: 3rem; max-width: 1500px;}
.ems-banner {background: linear-gradient(90deg,#14304f 0%,#1f4e79 60%,#2e75b6 100%); color:#fff;
             padding: 14px 22px; border-radius: 10px; margin-bottom: 14px;}
.ems-banner h1 {color:#fff; font-size: 1.45rem; margin:0; padding:0;}
.ems-banner p {color:#d6e4f5; margin: 2px 0 0 0; font-size: 0.88rem;}
.ems-card {border:1px solid #e3e8ef; border-radius:10px; padding:12px 16px; background:#fafbfd;}
.ems-badge {display:inline-block; padding:2px 9px; border-radius:12px; font-size:0.78rem; font-weight:600; margin-right:4px;}
.b-Confirmed {background:#c8e6c9; color:#1b5e20;}
.b-Suggested {background:#fff59d; color:#5d4c00;}
.b-RequiresValidation {background:#ffcc80; color:#7a3e00;}
.b-Missing {background:#ef9a9a; color:#7f1010;}
div[data-testid="stMetricValue"] {font-size: 1.6rem;}
section[data-testid="stSidebar"] .stRadio label {font-size: 0.95rem;}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def banner(title: str, subtitle: str = "") -> None:
    st.markdown(f'<div class="ems-banner"><h1>{title}</h1><p>{subtitle}</p></div>', unsafe_allow_html=True)


def badge(status: str) -> str:
    cls = "b-" + (status or "Missing").replace(" ", "")
    return f'<span class="ems-badge {cls}">{STATUS_ICONS.get(status, "")} {status}</span>'


def editor_version(key: str) -> int:
    return st.session_state.setdefault("_editor_versions", {}).get(key, 0)


def bump_editor(key: str) -> None:
    v = st.session_state.setdefault("_editor_versions", {})
    v[key] = v.get(key, 0) + 1


def editable_table(frame: pd.DataFrame, key: str, column_config: dict[str, Any] | None = None,
                   disabled: list[str] | None = None, search: bool = True, num_rows: str = "dynamic",
                   height: int | str = "auto", column_order: list[str] | None = None) -> tuple[pd.DataFrame, bool]:
    """Searchable data editor. Returns (full edited frame, changed?).

    Filtering only hides rows in the editor; edits made in the filtered
    visible subset are written back to their original row positions.
    """
    frame = frame.reset_index(drop=True)
    q = ""
    if search:
        q = st.text_input("Search", key=f"{key}_q", placeholder="Filter rows (any column contains...)",
                          label_visibility="collapsed")
    if q:
        mask = frame.astype(str).apply(lambda col: col.str.contains(q, case=False, regex=False)).any(axis=1)
        positions = list(frame.index[mask])
        view = frame.loc[positions].reset_index(drop=True)
        st.caption(f"{len(positions)} of {len(frame)} rows match. Adding/removing rows is disabled while filtering.")
        num_rows = "fixed"
    else:
        positions, view = [], frame
    edited = st.data_editor(view, key=f"{key}_v{editor_version(key)}_{abs(hash(q)) % 10**6}",
                            column_config=column_config or {}, disabled=disabled or [], num_rows=num_rows,
                            width="stretch", height=height, hide_index=True, column_order=column_order)
    if not q:
        full = edited.reset_index(drop=True)
    else:
        full = frame.copy()
        for pos, (_, row) in zip(positions, edited.iterrows()):
            for col in edited.columns:
                full.at[pos, col] = row[col]
    changed = not _same(frame, full)
    return full, changed


def _same(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    if a.shape != b.shape:
        return False
    try:
        return a.astype(str).reset_index(drop=True).equals(b[a.columns].astype(str).reset_index(drop=True))
    except Exception:
        return False


def apply_bar(key: str, changed: bool, on_apply: Callable[[], None], label: str = "Apply changes") -> None:
    c1, c2 = st.columns([1, 4])
    with c1:
        clicked = st.button(label, key=f"{key}_apply", type="primary" if changed else "secondary",
                            disabled=not changed, width="stretch")
    with c2:
        if changed:
            st.caption("⚠️ Unapplied edits - apply them so SQL, documentation and downloads include them.")
    if clicked:
        on_apply()
        bump_editor(key)
        st.rerun()
