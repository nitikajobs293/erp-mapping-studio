from __future__ import annotations

import streamlit as st

from erpdm.services.assistant import answer
from erpdm.services.audit import audit
from erpdm.services.llm import LLMClient
from erpdm.services.metadata import get_erp
from ui import components as ui

EXAMPLES = ["Which tables are used in IQ09?", "What is the relationship between EQUI and EQKT?",
            "How do I join VBAK, VBAP and LIKP?", "Which table contains delivery header information?",
            "What is the difference between VBFA and LIPS?", "Explain the relationship between EKPO and EKBE."]


def render() -> None:
    st.title("🤖 AI ERP Assistant")
    code = ui.need_erp()
    if not code:
        return
    llm = LLMClient()
    st.caption(f"Grounded on the **{code}** metadata repository and indexed documents · LLM: `{llm.describe()}`. "
               "Answers cite repository items; identifiers not found in the repository are flagged.")
    hist_key = f"chat_{code}"
    history = st.session_state.setdefault(hist_key, [])
    cols = st.columns(3)
    pick = None
    for i, ex in enumerate(EXAMPLES):
        if cols[i % 3].button(ex, key=f"ex{i}", width="stretch"):
            pick = ex
    for msg in history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("warn"):
                st.warning(msg["warn"])
    last_tables = st.session_state.get("assistant_tables")
    if last_tables and st.button(f"Use tables from the last answer in the workflow ({', '.join(last_tables[:6])})"):
        w = ui.wf()
        w.tables, w.root_table = list(last_tables), last_tables[0]
        w.plan, w.columns = None, [c for c in w.columns if c.table in last_tables]
        st.success("Workflow tables updated — continue in the Table Relationship Explorer or SQL Generator.")
    q = st.chat_input("Ask about tables, T-codes, joins, models…") or pick
    if not q:
        return
    history.append({"role": "user", "content": q})
    with st.chat_message("user"):
        st.markdown(q)
    with st.chat_message("assistant"):
        with st.spinner("Searching the metadata repository…"), ui.db() as s:
            erp = get_erp(s, code)
            a = answer(s, erp, q, [{"role": m["role"], "content": m["content"]} for m in history[:-1]], llm)
            audit(s, ui.user().username, "assistant.query", "erp_system", erp.id, {"mode": a.mode, "q": q[:200]})
        badge = {"llm": "🤖 AI answer grounded on repository", "metadata": "📚 Repository answer (no AI)",
                 "not_found": "❔ Not in repository"}[a.mode]
        st.caption(badge + (f" · cites {', '.join(a.citations[:8])}" if a.citations else ""))
        st.markdown(a.text)
        warn = None
        if a.unverified_identifiers:
            warn = ("These identifiers are **not in the repository** and may be hallucinated — verify before use: "
                    + ", ".join(f"`{x}`" for x in a.unverified_identifiers))
            st.warning(warn)
        st.session_state["assistant_tables"] = a.context_tables
        history.append({"role": "assistant", "content": a.text, "warn": warn})
    if st.button("Clear conversation"):
        st.session_state[hist_key] = []
        st.rerun()
