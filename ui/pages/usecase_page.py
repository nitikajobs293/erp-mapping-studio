from __future__ import annotations

import streamlit as st

from erpdm.constants import SQL_DIALECTS
from erpdm.services.metadata import get_erp
from erpdm.services.modelling import recommend_model
from erpdm.services.sqlgen import generate_sql
from erpdm.services.usecase import analyze_requirement
from ui import components as ui
from ui import workflow_ui as wui

EXAMPLE = ("I want to build an Equipment Master Data Model that provides equipment details, serial numbers, "
           "material information, plant, location, equipment category, manufacturer and maintenance information.")


def render() -> None:
    st.title("💡 Business Use Case Explorer")
    w = ui.wf()
    text = st.text_area("Describe the business requirement in plain language", value=w.use_case_text or EXAMPLE, height=110)
    c1, c2, c3 = st.columns(3)
    include_optional = c1.toggle("Include optional tables of the use case", value=True)
    include_pending = c2.toggle("Include pending joins", value=w.include_pending)
    dialect = c3.selectbox("SQL dialect", list(SQL_DIALECTS), index=list(SQL_DIALECTS).index(w.dialect))
    if not st.button("Analyse requirement", type="primary") and st.session_state.get("uc_text") != text:
        st.caption("The analysis matches your words against stored use cases, the business glossary and column "
                   "business names. It never invents tables or joins.")
        return
    st.session_state["uc_text"] = text
    with ui.db() as s:
        selected = get_erp(s, w.erp_code) if w.erp_code else None
        a = analyze_requirement(s, text, selected, include_optional, include_pending)
        for line in a.explanation:
            st.markdown(f"- {line}")
        if a.erp is None:
            return
        if a.erp.code != w.erp_code:
            st.info(f"The requirement mentions **{a.erp.name}** — switch the sidebar ERP to use it in the workflow.")
        m = st.columns(4)
        m[0].metric("ERP", a.erp.code)
        m[1].metric("Domain", a.domain or "—")
        m[2].metric("Use case", (a.use_case.name if a.use_case else "—")[:26])
        m[3].metric("Tables", len(a.plan.tables) if a.plan else 0)
        if a.alternatives:
            st.caption("Other candidate use cases: " + ", ".join(a.alternatives))
        st.subheader("Requirement → columns")
        ui.df([{"Requirement phrase": x.business_column, "Table": x.table, "Column": x.column, "Match": x.method,
                "Confidence": x.confidence, "Alternatives": ", ".join(x.alternatives), "Note": x.note,
                "Validation": ui.status_badge(x.validation_status)} for x in a.matches])
        st.caption("Mandatory tables: " + (", ".join(a.mandatory_tables) or "—") + " · Optional tables: "
                   + (", ".join(a.optional_tables) or "—"))
        if not a.plan:
            st.warning("No join plan could be built. Map columns manually or onboard more metadata.")
            return
        st.subheader("Adjust columns")
        cols = wui.column_picker(s, a.erp, a.plan.tables, a.columns, key="uc")
        plan = wui.replan(s, a.erp, cols, a.plan.root_table, include_pending=include_pending) if cols else a.plan
        cols = cols or a.columns
        tabs = st.tabs(["Joins & ER diagram", "Business logic", "Source SQL", "Target model"])
        with tabs[0]:
            wui.plan_table(plan)
            wui.er_diagram(s, a.erp, plan, cols)
        with tabs[1]:
            if a.process:
                st.markdown(f"**Process:** {a.process.name}")
                st.markdown(" → ".join(f"`{x}`" for x in (a.process.flow_steps or [])))
            if a.use_case:
                st.markdown(f"**Use case:** {a.use_case.description}  \n**Grain:** {a.use_case.grain}")
            for e in plan.edges:
                st.markdown(f"- **{e.left_table} → {e.right_table}** ({'mandatory' if e.join_type == 'INNER' else 'optional'}): "
                            f"{e.description or ''} — {e.business_purpose or ''}")
        sql = generate_sql(s, a.erp, plan, cols, dialect, raw_schema=a.erp.code)
        with tabs[2]:
            st.code(sql.sql, language="sql")
            st.download_button("Download SQL", sql.sql, file_name=f"{a.erp.code}_use_case.sql")
        with tabs[3]:
            rec = recommend_model(s, a.erp, plan, cols)
            ui.df([{"Model": x.name, "Kind": x.kind, "Grain": x.grain, "SCD": x.scd_type, "Rationale": x.rationale}
                   for x in rec.models])
        if a.erp.code == w.erp_code:
            w.dialect = dialect
            wui.send_to_workflow(plan, cols, use_case_text=text, use_case_name=a.use_case.name if a.use_case else None,
                                 sql=sql.sql, sql_meta=sql.model_dump(), key="uc_send")
