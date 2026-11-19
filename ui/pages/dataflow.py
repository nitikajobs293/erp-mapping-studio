from __future__ import annotations

import streamlit as st
from sqlalchemy import select

from erpdm.db.models import BusinessProcess, Transaction
from erpdm.services.dataflow import flow_for_process, flow_for_transaction
from erpdm.services.diagrams import render_png, render_svg
from erpdm.services.metadata import get_erp, get_transaction
from erpdm.services.relationships import relationship_rows
from ui import components as ui


def render() -> None:
    st.title("🔀 ERP Data Flow & Architecture Explorer")
    code = ui.need_erp()
    if not code:
        return
    w = ui.wf()
    with ui.db() as s:
        erp = get_erp(s, code)
        procs = list(s.scalars(select(BusinessProcess).where(BusinessProcess.erp_id == erp.id).order_by(BusinessProcess.name)))
        txs = list(s.scalars(select(Transaction).where(Transaction.erp_id == erp.id).order_by(Transaction.code)))
        mode = st.radio("Explore by", ["Business process", "Transaction"], horizontal=True,
                        index=1 if w.tcode else 0)
        if mode == "Business process":
            if not procs:
                st.info("No business processes recorded for this ERP. Onboard a metadata package with processes.")
                return
            p = st.selectbox("Process", procs, format_func=lambda x: f"{x.name} · {x.domain or ''}")
            dot, tables = flow_for_process(s, erp, p, w)
            st.markdown(f"**{p.name}** — {p.description or ''}")
        else:
            if not txs:
                st.info("No transactions recorded for this ERP.")
                return
            codes = [t.code for t in txs]
            c = st.selectbox("Transaction", codes, index=codes.index(w.tcode) if w.tcode in codes else 0)
            tx = get_transaction(s, erp.id, c)
            tables = [l.table.name for l in tx.table_links if l.role != "status"]
            dot = flow_for_transaction(s, erp, tx, tables, w)
            st.markdown(f"**{tx.code} — {tx.name}**: {tx.description or ''}")
        ui.graphviz(dot)
        c1, c2, c3 = st.columns(3)
        png, svg = render_png(dot), render_svg(dot)
        if png:
            c1.download_button("⬇ PNG", png, file_name=f"{erp.code}_dataflow.png", mime="image/png")
        if svg:
            c2.download_button("⬇ SVG", svg, file_name=f"{erp.code}_dataflow.svg", mime="image/svg+xml")
        c3.download_button("⬇ DOT", dot, file_name=f"{erp.code}_dataflow.dot")
        st.subheader("Extraction dependencies")
        st.markdown(f"**Extraction method:** {erp.extraction_method or 'not configured'}")
        rows = relationship_rows(s, erp.id, tables)
        if rows:
            st.caption("Tables must be extracted consistently (same snapshot / CDC window) because they are joined:")
            ui.df([{k: r[k] for k in ("Source table", "Target table", "Join columns", "Cardinality", "Validation")} for r in rows])
