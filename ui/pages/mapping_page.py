from __future__ import annotations

import pandas as pd
import streamlit as st

from erpdm.config import get_settings
from erpdm.schemas import GeneratedSQL
from erpdm.services import docgen
from erpdm.services.diagrams import data_flow_dot, er_diagram_dot, render_png
from erpdm.services.mapping import (EDITABLE, MappingError, approve_mapping, create_mapping, get_mapping,
                                    list_mappings, mapping_dataframe, parse_business_column_file,
                                    resolve_business_columns, update_mapping_rows)
from erpdm.services.metadata import get_erp, get_table
from erpdm.services.modelling import recommend_model
from erpdm.services.dataflow import predicted_models
from erpdm.services.relationships import relationship_rows
from erpdm.services.sqlgen import snake
from ui import components as ui

LOCKED = ["id", "ERP", "Source Module", "Data Type", "Data Type Conversion", "Join Condition", "Join Dependency",
          "Match Method", "Confidence", "Source Type", "Target Database", "Target Schema", "Target Table"]


def render() -> None:
    st.title("🗂️ Column Mapping · Source-to-Target")
    code = ui.need_erp()
    if not code:
        return
    w = ui.wf()
    ui.workflow_banner()
    settings = get_settings()
    with ui.db() as s:
        erp = get_erp(s, code)
        tab_new, tab_edit = st.tabs(["Create mapping", "Review · edit · approve · download"])
        with tab_new:
            if not ui.can("generate"):
                st.info("Viewers can review mappings but not create them.")
            else:
                src = st.radio("Business columns from", ["Workflow selection", "Upload file (CSV/XLSX/TXT)", "Type a list"],
                               horizontal=True)
                names: list[str] = []
                if src == "Workflow selection":
                    if not w.columns:
                        st.info("No columns in the workflow yet.")
                    names = [c.business_name or _biz(s, erp, c) for c in w.columns]
                elif src.startswith("Upload"):
                    f = st.file_uploader("Business column list", type=["csv", "xlsx", "txt"])
                    if f:
                        try:
                            names = parse_business_column_file(f.name, f.getvalue())
                        except MappingError as e:
                            st.error(str(e))
                else:
                    names = [x.strip() for x in st.text_area("One business column per line",
                             "Equipment Number\nSerial Number\nMaterial Number\nMaterial Description\nPlant\n"
                             "Manufacturer\nEquipment Category").splitlines() if x.strip()]
                if names:
                    st.caption(f"{len(names)} business columns · context tables: {', '.join(w.tables) or 'none (whole ERP)'}")
                    preview = resolve_business_columns(s, erp, names, w.tables or None)
                    ui.df([{"Business column": m.business_column, "Source": f"{m.table}.{m.column}" if m.table else "—",
                            "Method": m.method, "Confidence": m.confidence, "Alternatives": ", ".join(m.alternatives),
                            "Validation": ui.status_badge(m.validation_status), "Note": m.note} for m in preview])
                    c1, c2, c3 = st.columns(3)
                    root = w.root_table or (w.tables[0] if w.tables else None)
                    entity = snake((get_table(s, erp.id, root).business_entity if root else None) or "entity")
                    tdb = c1.text_input("Target database", settings.target_database)
                    tsch = c2.text_input("Target schema", "MARTS")
                    ttab = c3.text_input("Target table", f"DIM_{entity.upper()}")
                    name = st.text_input("Mapping name", f"{erp.code} {w.tcode or w.use_case_name or entity} mapping")
                    if st.button("Generate mapping document", type="primary"):
                        try:
                            m = create_mapping(s, erp, names, ui.user().username, name=name, context_tables=w.tables or None,
                                               root_table=root, target_database=tdb, target_schema=tsch, target_table=ttab,
                                               transaction_code=w.tcode, use_case=w.use_case_text,
                                               include_pending=w.include_pending, matches=preview)
                            w.mapping_id = m.id
                            st.cache_data.clear()
                            st.success(f"Mapping #{m.id} created. Review it in the next tab.")
                        except MappingError as e:
                            st.error(str(e))
        with tab_edit:
            maps = list_mappings(s, erp.id)
            if not maps:
                st.info("No mappings yet for this ERP.")
                return
            ids = [m.id for m in maps]
            sel = st.selectbox("Mapping", ids, index=ids.index(w.mapping_id) if w.mapping_id in ids else 0,
                               format_func=lambda i: next(f"#{m.id} · {m.name} · {m.status} · {m.created_at:%Y-%m-%d}" for m in maps if m.id == i))
            m = get_mapping(s, sel)
            w.mapping_id = m.id
            st.caption(f"Status **{m.status}** · created by {m.created_by} · version {m.version}"
                       + (f" · approved by {m.approved_by} on {m.approved_at:%Y-%m-%d}" if m.approved_by else ""))
            frame = mapping_dataframe(m)
            editable = ui.can("edit_mapping") and m.status != "approved"
            edited = st.data_editor(
                frame, width="stretch", hide_index=True, disabled=LOCKED if editable else True,
                column_config={"Validation Status": st.column_config.SelectboxColumn(
                    options=["verified", "requires_validation", "pending", "rejected"])}, key=f"ed_{m.id}")
            b1, b2, b3 = st.columns(3)
            if editable and b1.button("💾 Save edits"):
                try:
                    n = update_mapping_rows(s, m.id, edited, ui.user().username)
                    st.success(f"{n} change(s) saved (versioned & audited).")
                    st.rerun()
                except MappingError as e:
                    st.error(str(e))
            if ui.can("approve_mapping") and m.status != "approved" and b2.button("✅ Approve mapping"):
                try:
                    notes = approve_mapping(s, m.id, ui.user().username)
                    st.success("Approved. " + " ".join(notes))
                    st.rerun()
                except MappingError as e:
                    st.error(str(e))
            st.subheader("Downloads")
            key = f"docs_{m.id}_{m.version}_{m.status}"
            if st.button("Prepare Excel / Word / PDF / diagrams", key=f"prep_{m.id}") or key in st.session_state:
                if key not in st.session_state:
                    with st.spinner("Building documents…"):
                        st.session_state[key] = _build_docs(s, erp, m, frame, w)
                docs = st.session_state[key]
                base = snake(m.name)
                d1, d2, d3 = st.columns(3)
                d1.download_button("⬇ Excel mapping (.xlsx)", docs["xlsx"], file_name=f"{base}.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                d2.download_button("⬇ Technical design (.docx)", docs["docx"], file_name=f"{base}_design.docx",
                                   mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                d3.download_button("⬇ Technical design (.pdf)", docs["pdf"], file_name=f"{base}_design.pdf",
                                   mime="application/pdf")
                e1, e2, e3 = st.columns(3)
                if docs.get("er_png"):
                    e1.download_button("⬇ ER diagram (.png)", docs["er_png"], file_name=f"{base}_er.png", mime="image/png")
                if docs.get("flow_png"):
                    e2.download_button("⬇ Data flow (.png)", docs["flow_png"], file_name=f"{base}_flow.png", mime="image/png")
                if docs.get("sql"):
                    e3.download_button("⬇ Source SQL (.sql)", docs["sql"], file_name=f"{base}.sql")


def _build_docs(s, erp, m, frame, w) -> dict:
    meta, mapping_df, joins, tables_rows, gen, rec, er_png, flow_png, plan = _doc_inputs(s, erp, m, frame, w)
    secs = docgen.build_design_sections(
        erp_name=erp.name, tcode=m.transaction_code, use_case=m.use_case, plan=plan, gen_sql=gen, rec=rec,
        mapping_df=mapping_df, relationships_df=pd.DataFrame(joins) if joins else None,
        tables_df=pd.DataFrame(tables_rows) if tables_rows else None, dbt_files=w.dbt_files or None)
    title = f"Technical Design — {m.name}"
    return {
        "xlsx": docgen.mapping_excel(meta, mapping_df, joins, tables_rows, gen.sql if gen else None,
                                     gen.validation_queries if gen else None),
        "docx": docgen.technical_design_docx(title, meta, secs, er_png, flow_png),
        "pdf": docgen.technical_design_pdf(title, meta, secs, er_png, flow_png),
        "er_png": er_png, "flow_png": flow_png, "sql": gen.sql if gen else None,
    }


def _biz(s, erp, c) -> str:
    t = get_table(s, erp.id, c.table)
    col = next((x for x in t.columns if x.name == c.column), None) if t else None
    return (col.business_name if col and col.business_name else f"{c.table}.{c.column}")


def _doc_inputs(s, erp, m, frame, w):
    from erpdm.schemas import JoinPlan
    from erpdm.services.mapping import selected_columns_from_mapping
    from erpdm.services.sqlgen import generate_sql
    plan = JoinPlan.model_validate(m.join_plan) if m.join_plan else None
    meta = {"Mapping": m.name, "Mapping ID": m.id, "ERP": f"{erp.name} ({erp.code})", "T-Code": m.transaction_code,
            "Use case": m.use_case, "Target": f"{m.target_database}.{m.target_schema}.{m.target_table}",
            "Status": m.status, "Created by": m.created_by, "Approved by": m.approved_by, "Version": m.version}
    mapping_df = frame.drop(columns=["id"])
    joins, tables_rows, gen, rec, er_png, flow_png = [], [], None, None, None, None
    if plan:
        joins = relationship_rows(s, erp.id, plan.tables)
        for tname in plan.tables:
            t = get_table(s, erp.id, tname)
            tables_rows.append({"Table": t.name, "Description": t.description, "Class": t.table_class,
                                "Primary key": ", ".join(t.primary_key), "Source": t.source_type,
                                "Confidence": t.confidence, "Validation": t.validation_status})
        cols = selected_columns_from_mapping(m)
        if cols:
            try:
                gen = generate_sql(s, erp, plan, cols, "Snowflake", raw_database="RAW", raw_schema=erp.code)
                rec = recommend_model(s, erp, plan, cols)
            except Exception:  # noqa: BLE001 - doc still useful without SQL
                gen = GeneratedSQL.model_validate(w.sql_meta) if w.sql_meta else None
        er_png = render_png(er_diagram_dot(s, erp, plan.tables, plan))
        root = get_table(s, erp.id, plan.root_table)
        entity = snake(root.business_entity or root.name)
        flow_png = render_png(data_flow_dot(erp, None, [m.use_case or m.transaction_code or "Business process"],
                                            m.transaction_code, None, plan.tables,
                                            predicted_models(erp, plan.tables, entity, w.dbt_models or None)))
    return meta, mapping_df, joins, tables_rows, gen, rec, er_png, flow_png, plan
