from __future__ import annotations

import sqlglot
import streamlit as st

from erpdm.constants import SQL_DIALECTS
from erpdm.db.models import GeneratedArtifact
from erpdm.services.metadata import get_erp
from erpdm.services.relationships import PlanningError
from erpdm.services.sqlgen import SQLGenError, generate_sql
from ui import components as ui
from ui import workflow_ui as wui


def render() -> None:
    st.title("🧮 SQL Query Generator")
    code = ui.need_erp()
    if not code:
        return
    w = ui.wf()
    ui.workflow_banner()
    with ui.db() as s:
        erp = get_erp(s, code)
        if not w.tables:
            st.info("No scope yet. Start from **T-Code Intelligence**, **Business Use Case Explorer** or "
                    "**Table Relationship Explorer**, or pick tables in the relationship explorer.")
            return
        st.subheader("1 · Columns")
        cols = wui.column_picker(s, erp, w.tables, w.columns, key="sql")
        if not cols:
            st.warning("Select at least one column.")
            return
        st.subheader("2 · Options")
        c1, c2, c3, c4 = st.columns(4)
        dialect = c1.selectbox("Dialect", list(SQL_DIALECTS), index=list(SQL_DIALECTS).index(w.dialect))
        raw_db = c2.text_input("Source database", value="RAW")
        raw_schema = c3.text_input("Source schema", value=erp.code)
        limit = c4.number_input("Row limit (0 = none)", min_value=0, value=0, step=100)
        o1, o2, o3 = st.columns(3)
        default_filters = o1.toggle("Apply table default filters (e.g. current validity)", value=True)
        excl_deleted = o2.toggle("Exclude logically deleted records", value=False)
        include_pending = o3.toggle("Include pending joins", value=w.include_pending)
        filters = st.text_area("Additional WHERE conditions (one per line, use table aliases e.g. `equi.EQTYP = 'S'`)",
                               height=70)
        try:
            plan = wui.replan(s, erp, cols, w.root_table, include_pending=include_pending, prefer=w.prefer_relationship_ids)
            gen = generate_sql(s, erp, plan, cols, dialect, [f for f in filters.splitlines() if f.strip()],
                               raw_db or None, raw_schema or None, default_filters, excl_deleted, int(limit) or None)
        except (SQLGenError, PlanningError) as e:
            st.error(str(e))
            return
        w.columns, w.plan, w.dialect, w.include_pending = cols, plan, dialect, include_pending
        w.sql, w.sql_meta = gen.sql, gen.model_dump()
        st.subheader("3 · Generated SQL")
        if plan.min_confidence < 0.9 or any(e.validation_status != "verified" for e in plan.edges):
            st.warning("Some joins are not verified. See the provenance comments in the SQL and validate before production.")
        st.code(gen.sql, language="sql", line_numbers=True)
        d1, d2, d3 = st.columns(3)
        d1.download_button("⬇ Download .sql", gen.sql, file_name=f"{erp.code}_{w.tcode or 'query'}_{SQL_DIALECTS[dialect]}.sql")
        if d2.button("💾 Save to history"):
            s.add(GeneratedArtifact(kind="sql", name=f"{erp.code}_{w.tcode or w.use_case_name or 'query'}", erp_id=erp.id,
                                    content=gen.sql, created_by=ui.user().username, meta={"dialect": dialect}))
            st.toast("Saved")
        with st.expander("✏️ Edit & validate SQL"):
            edited = st.text_area("SQL editor", value=gen.sql, height=320, key="sql_editor")
            if st.button("Validate syntax"):
                try:
                    sqlglot.parse(edited, read=SQL_DIALECTS[dialect])
                    st.success(f"Parses as valid {dialect} SQL.")
                except sqlglot.errors.ParseError as e:
                    st.error(f"Syntax error: {e}")
            d3.download_button("⬇ Download edited", edited, file_name="edited_query.sql")
        tabs = st.tabs(["Column mapping", "Joins", "Filters", "Duplicates", "Nulls", "Performance", "Validation queries"])
        with tabs[0]:
            ui.df(gen.column_mapping)
        with tabs[1]:
            for x in gen.join_explanations:
                st.markdown(f"- {x}")
        with tabs[2]:
            for x in gen.filter_explanations:
                st.markdown(f"- {x}")
        with tabs[3]:
            for x in gen.duplicate_handling:
                st.markdown(f"- {x}")
        with tabs[4]:
            for x in gen.null_handling:
                st.markdown(f"- {x}")
        with tabs[5]:
            for x in gen.performance_notes:
                st.markdown(f"- {x}")
        with tabs[6]:
            for k, v in gen.validation_queries.items():
                st.markdown(f"**{k}**")
                st.code(v, language="sql")
            st.download_button("⬇ Download validation queries", "\n\n".join(f"-- {k}\n{v}" for k, v in gen.validation_queries.items()),
                               file_name=f"{erp.code}_validation_queries.sql")
