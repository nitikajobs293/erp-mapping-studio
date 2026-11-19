"""Reusable rendering blocks for mapping results."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from config.settings import CONFIDENCE
from services import model_service as ms
from services.mapping_engine import MappingResult
from utils.ui import public, repo, show_warnings


def metrics(result: MappingResult):
    rows = result.rows
    n = len(rows)
    by = {}
    for r in rows:
        by[r["_confidence"]] = by.get(r["_confidence"], 0) + 1
    c = st.columns(6)
    c[0].metric("Columns", n)
    c[1].metric("🟢 Standard", by.get("standard", 0))
    c[2].metric("🔵 Uploaded", by.get("uploaded", 0))
    c[3].metric("🟡 Inferred", by.get("inferred", 0))
    c[4].metric("🔴 Validate / 🟣 AI", by.get("validate", 0) + by.get("ai", 0))
    c[5].metric("⚫ Unavailable", by.get("unavailable", 0))


def mapping_table(result: MappingResult, key: str):
    df = public(result.mapping_df())
    only_issues = st.toggle("Show only columns needing attention", key=f"{key}_issues")
    if only_issues:
        df = df[~df["Confidence"].str.contains("Standard|uploaded", regex=True)]
    st.dataframe(df, width="stretch", hide_index=True, height=min(600, 38 + 35 * len(df)),
                 column_config={"Business Column": st.column_config.TextColumn(pinned=True)})
    unavailable = [r for r in result.rows if r["_confidence"] in ("unavailable",) or r["Source Field"] == "TBD"]
    if unavailable:
        st.error("**Not available / not confirmed:** " + ", ".join(r["Business Column"] for r in unavailable) +
                 " — see *Alternative Source* column; nothing was silently dropped.")


def architecture_preview(result: MappingResult, key: str, approve: bool = True):
    erp = repo().get(result.erp_id)
    st.markdown(f"**Source ERP:** {result.erp_name}{f' ({result.erp_version})' if result.erp_version else ''} &nbsp;·&nbsp; **Anchor / grain:** `{result.anchor}` "
                f"&nbsp;·&nbsp; **Model:** {result.model_type}")
    st.info(f"**Dataset grain:** {result.grain}\n\n**Effective grain of generated SQL:** {result.effective_grain}")
    t1, t2, t3, t4, t5 = st.tabs(["🗺️ ER diagram", "📋 Tables & roles", "🔗 Joins & keys", "🧱 Fact / dim & layers", "🧮 Grain & dedup"])
    with t1:
        dot = ms.mapping_dot(result, erp)
        st.graphviz_chart(dot, width="stretch")
        st.caption("Blue header = anchor (grain) table · red edges = 1:N fan-out (grain change) · dashed = inferred/needs validation "
                   "· footer colour = table confidence")
        with st.expander("Text relationship view"):
            lines = [f"{result.anchor}  (anchor)"]
            for j in result.joins:
                lines.append(f"  {j['left']} --[{j['cardinality']}: " + ", ".join(f"{a}={b}" for a, b in j["conditions"]) +
                             f"]--> {j['right']}" + ("   ⚠ FAN-OUT" if j.get("fan_out") else ""))
            st.code("\n".join(lines), language="text")
    with t2:
        st.dataframe(result.tables_df(erp), width="stretch", hide_index=True)
    with t3:
        st.dataframe(result.joins_df(), width="stretch", hide_index=True)
    with t4:
        st.dataframe(ms.classify_tables(result, erp), width="stretch", hide_index=True)
        layers = ms.medallion_design(result, erp, repo())
        st.markdown("**Snowflake layers**")
        st.dataframe(pd.concat([layers["staging"], layers["silver"], layers["gold"]]), width="stretch", hide_index=True)
        st.graphviz_chart(ms.layer_dot(result, erp, repo()), width="stretch")
    with t5:
        st.dataframe(ms.grain_and_keys(result, erp), width="stretch", hide_index=True)
    show_warnings(result.warnings, result.duplicate_risks)
    if approve:
        if st.session_state.get("architecture_approved") and st.session_state.current_result is result:
            st.success("✅ Architecture approved — documents can be generated in **Documentation & Downloads**.")
        elif st.button("✅ Approve architecture & enable document generation", key=f"{key}_approve", type="primary"):
            st.session_state.current_result = result
            st.session_state.architecture_approved = True
            st.rerun()


def confidence_explainer():
    with st.expander("ℹ️ How confidence levels work"):
        for k, c in CONFIDENCE.items():
            st.markdown(f"- {c['icon']} **{c['label']}** — {c['long']}")
