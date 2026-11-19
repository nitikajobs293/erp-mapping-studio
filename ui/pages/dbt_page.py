from __future__ import annotations

import streamlit as st

from erpdm.config import get_settings
from erpdm.services.dbtgen import DbtOptions, generate_dbt_project, record_project
from erpdm.services.mapping import get_mapping, selected_columns_from_mapping
from erpdm.services.metadata import get_erp
from erpdm.services.modelling import recommend_model
from erpdm.services.relationships import PlanningError
from erpdm.services.sqlgen import snake
from ui import components as ui
from ui import workflow_ui as wui


def render() -> None:
    st.title("🏗️ Snowflake Data Modelling & dbt Generator")
    code = ui.need_erp()
    if not code:
        return
    w = ui.wf()
    ui.workflow_banner()
    settings = get_settings()
    with ui.db() as s:
        erp = get_erp(s, code)
        columns = w.columns
        source = "workflow selection"
        if w.mapping_id:
            m = get_mapping(s, w.mapping_id)
            if m and m.erp_id == erp.id and st.toggle(f"Use mapping #{m.id} ({m.status}) as the column source",
                                                      value=m.status == "approved"):
                columns = selected_columns_from_mapping(m)
                source = f"mapping #{m.id}"
        if not columns:
            st.info("Select columns first (T-Code Intelligence, Use Case Explorer, SQL Generator or a mapping).")
            return
        try:
            plan = wui.replan(s, erp, columns, w.root_table, include_pending=w.include_pending,
                              prefer=w.prefer_relationship_ids)
        except PlanningError as e:
            st.error(str(e))
            return
        st.caption(f"{len(columns)} columns from {source}; grain {plan.root_table}.")
        st.subheader("1 · Dimensional model recommendation")
        history = st.toggle("Historical tracking required (SCD Type 2)", value=w.history_required)
        w.history_required = history
        rec = recommend_model(s, erp, plan, columns, history_required=history)
        ui.df([{"Model": m.name, "Kind": m.kind, "Grain": m.grain, "SCD": m.scd_type, "Natural key": ", ".join(m.natural_key),
                "Surrogate key": m.surrogate_key, "Attributes": len(m.attributes), "Measures": ", ".join(m.measures),
                "Foreign keys": ", ".join(f"{f['column']}→{f['references']}" for f in m.foreign_keys),
                "Rationale": m.rationale} for m in rec.models])
        st.subheader("2 · Incremental load strategy")
        ui.df([{"Table": x.table, "Strategy": x.strategy, "Unique key": ", ".join(x.unique_key),
                "Watermark": ", ".join(x.watermark) or "—", "Reliable timestamp": x.watermark_reliable,
                "Lookback": x.lookback, "Change tracking": x.change_tracking, "Hard deletes": x.hard_delete,
                "Full refresh": x.full_refresh_cadence, "Notes": " ".join(x.notes)} for x in rec.strategies])
        for n in rec.notes:
            st.caption(f"ℹ️ {n}")
        st.subheader("3 · dbt options")
        c1, c2, c3, c4 = st.columns(4)
        project = c1.text_input("Project name", value=f"{snake(erp.code)}_analytics")
        raw_db = c2.text_input("RAW database", value=settings.raw_database)
        raw_schema = c3.text_input("RAW schema", value=erp.code)
        loaded_at = c4.text_input("Ingestion timestamp column", value=settings.raw_loaded_at_column)
        c5, c6, c7, c8 = st.columns(4)
        mat = c5.selectbox("Mart materialization", ["incremental", "table", "view"])
        cdc_del = c6.text_input("CDC delete flag column (optional)", placeholder="_fivetran_deleted")
        cdc_op = c7.text_input("CDC operation column (optional)", placeholder="_op")
        lookback = c8.number_input("Lookback days", 0, 60, 3)
        excl = st.toggle("Exclude logically deleted records from the mart", value=False)
        if not ui.can("generate"):
            st.info("Viewers cannot generate dbt projects.")
            return
        if st.button("Generate dbt project", type="primary"):
            opts = DbtOptions(project_name=snake(project), raw_database=raw_db, raw_schema=raw_schema,
                              loaded_at_column=loaded_at, cdc_deleted_column=cdc_del or None,
                              cdc_operation_column=cdc_op or None, materialization=mat,
                              scd_type="SCD2" if history else None, lookback_days=int(lookback),
                              exclude_soft_deleted=excl, generated_sql=w.sql)
            proj = generate_dbt_project(s, erp, plan, columns, rec, opts)
            record_project(s, erp, proj, ui.user().username, opts.project_name)
            st.session_state["dbt_proj"] = {"files": proj.files, "models": proj.model_names, "zip": proj.to_zip(),
                                            "name": opts.project_name}
            w.dbt_files, w.dbt_models = sorted(proj.files), proj.model_names
            st.cache_data.clear()
            st.success(f"Generated {len(proj.files)} files · {len(proj.model_names)} models.")
        data = st.session_state.get("dbt_proj")
        if data:
            st.subheader("4 · Preview & download")
            st.download_button("⬇ Download dbt project (.zip)", data["zip"], file_name=f"{data['name']}.zip",
                               mime="application/zip", type="primary")
            f = st.selectbox("File", sorted(data["files"]))
            lang = "sql" if f.endswith(".sql") else "yaml" if f.endswith((".yml", ".yaml")) else "markdown"
            st.code(data["files"][f], language=lang, line_numbers=True)
