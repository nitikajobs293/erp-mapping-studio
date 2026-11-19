"""ERP Mapping Studio - Streamlit entry point.

Run locally:  streamlit run app.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

import config as cfg
from ai_service import get_provider, recommend
from documentation_generator import build_document, to_markdown
from erp_master import DATE_FORMATS, ERP_FIELDS, erp_names, load_erps, save_erps
from export_manager import EXPORTS, safe_name, to_project_file
from grain_analyzer import duplicate_risks, find_change_columns
from join_analyzer import join_diagram_dot, join_diagram_text, table_info
from mapping_engine import apply_ai_result, join_tree_for, regenerate_outputs, run_analysis
from metadata_parser import (detect_kind, merge_metadata, normalize_source_metadata, normalize_targets, parse_s2t,
                             read_any)
from models import (FANOUT_OPTIONS, JOIN_FIELDS, MAPPING_FIELDS, OPEN_QUESTION_FIELDS, SOURCE_METADATA_FIELDS,
                    TABLE_MAPPING_FIELDS, TARGET_FIELDS, MappingProject)
from snowflake_sql_generator import SQL_LABELS, combined_script
from transformation_engine import TRANSFORMATION_TYPES
from utils.text_utils import clean_str, split_list, to_bool
from utils.ui import apply_bar, badge, banner, bump_editor, editable_table, inject_css
from validation_engine import summary_counts

st.set_page_config(page_title=cfg.APP_NAME, page_icon="🧭", layout="wide", initial_sidebar_state="expanded")
inject_css()

SAMPLE_PROJECT = cfg.SAMPLE_DATA_DIR / "sample_sap_s4hana_delivery_project.json"
PAGES = ["🏠 Projects", "⚙️ Configuration", "📝 Business Context", "🗂️ Source Metadata", "🎯 Target Columns",
         "🤖 Generate Mapping", "🔍 Review & Preview", "⬇️ Export", "🏭 ERP Master", "🔑 AI Settings"]


# =========================================================================== state
def project() -> MappingProject:
    if "project" not in st.session_state:
        st.session_state.project = MappingProject()
    return st.session_state.project


def set_project(p: MappingProject) -> None:
    st.session_state.project = p
    st.session_state.pop("_editor_versions", None)


def load_sample() -> MappingProject:
    if SAMPLE_PROJECT.exists():
        return MappingProject.from_json(SAMPLE_PROJECT.read_text(encoding="utf-8"))
    from sample_data.build_sample_project import build
    return build()


def go(page: str) -> None:
    """Navigate on the next run (the nav widget cannot be changed after it is drawn)."""
    st.session_state["_goto"] = page
    st.rerun()


def mapped() -> bool:
    return bool(project().column_mappings)


# =========================================================================== sidebar
def sidebar() -> str:
    p = project()
    with st.sidebar:
        st.markdown(f"### 🧭 {cfg.APP_NAME}")
        st.caption(f"v{cfg.APP_VERSION} · Multi-ERP mapping & Snowflake SQL")
        st.markdown(f"**Project:** {p.config.get('project_name')}")
        steps = [("Configuration", bool(p.config.get("target_object"))),
                 ("Source metadata", bool(p.source_metadata)),
                 ("Target columns", bool(p.target_columns)),
                 ("Mapping generated", mapped()),
                 ("Open questions resolved", mapped() and all(q.get("resolution_status") != "Open"
                                                              for q in p.open_questions if q.get("severity") == "High"))]
        for label, done in steps:
            st.markdown(f"{'✅' if done else '⬜'} {label}")
        st.divider()
        if "_goto" in st.session_state:
            st.session_state.nav = st.session_state.pop("_goto")
        page = st.radio("Navigate", PAGES, key="nav", label_visibility="collapsed")
        st.divider()
        if mapped():
            c = summary_counts(p)
            st.markdown(" ".join(f"{cfg.STATUS_ICONS[k]} {v}" for k, v in c.items()))
            st.caption("Confirmed · Suggested · Requires Validation · Missing")
        st.download_button("💾 Download project file", to_project_file(p),
                           file_name=f"{safe_name(p.config.get('project_name'))}.erpmap.json",
                           mime="application/json", width="stretch")
    return page


# =========================================================================== pages
def page_projects() -> None:
    p = project()
    banner("Projects", "Create, open, save and version mapping projects. No database required.")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("#### ➕ New project")
        name = st.text_input("Project name", value="New Mapping Project", key="new_name")
        if st.button("Create empty project", width="stretch"):
            np_ = MappingProject()
            np_.config["project_name"] = name
            set_project(np_)
            st.success("Project created.")
            go("⚙️ Configuration")
    with c2:
        st.markdown("#### 📦 Sample project")
        st.caption("SAP S/4HANA delivery + shipment (LIKP, LIPS, VTTK, VTTP, T001W) mapped to a delivery-item target view.")
        if st.button("Load SAP S/4HANA sample", type="primary", width="stretch"):
            set_project(load_sample())
            go("🔍 Review & Preview")
    with c3:
        st.markdown("#### 📂 Open project")
        up = st.file_uploader("Project file (.json)", type=["json"], key="open_proj", label_visibility="collapsed")
        if up is not None and st.button("Open uploaded project", width="stretch"):
            try:
                set_project(MappingProject.from_json(up.getvalue().decode("utf-8")))
                st.success("Project opened.")
                go("🔍 Review & Preview")
            except Exception as e:
                st.error(f"Could not open project: {e}")
        saved = sorted(cfg.PROJECTS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        if saved:
            pick = st.selectbox("Or reopen a project saved on this server", [f.name for f in saved])
            if st.button("Reopen saved project", width="stretch"):
                set_project(MappingProject.from_json((cfg.PROJECTS_DIR / pick).read_text(encoding="utf-8")))
                go("🔍 Review & Preview")

    st.divider()
    st.markdown("#### 🗃️ Save & versions")
    c1, c2 = st.columns([2, 3])
    with c1:
        note = st.text_input("Version note", placeholder="e.g. After review with SD functional team")
        if st.button("Save version snapshot", width="stretch"):
            v = p.save_version(note)
            st.success(f"Saved version {v['version']}.")
        if st.button("Save project on server", width="stretch"):
            path = cfg.PROJECTS_DIR / f"{safe_name(p.config.get('project_name'))}_{p.project_id}.json"
            path.write_text(p.to_json(), encoding="utf-8")
            st.success(f"Saved to {path.relative_to(cfg.BASE_DIR)}. On Streamlit Cloud the server disk is "
                       "temporary - use 'Download project file' to keep a copy.")
    with c2:
        if p.versions:
            vdf = pd.DataFrame([{k: v for k, v in ver.items() if k != "snapshot"} for ver in p.versions])
            st.dataframe(vdf, hide_index=True, width="stretch")
            r1, r2 = st.columns([2, 1])
            sel = r1.selectbox("Restore version", [v["version"] for v in p.versions], label_visibility="collapsed")
            if r2.button("Restore", width="stretch"):
                p.restore_version(sel)
                bump_all_editors()
                st.success(f"Restored version {sel}.")
                st.rerun()
        else:
            st.info("No versions saved yet. Snapshots capture configuration, mappings, joins, grain, questions and SQL.")


def bump_all_editors() -> None:
    st.session_state.pop("_editor_versions", None)


def page_configuration() -> None:
    p = project()
    c = p.config
    banner("Mapping Configuration", "Source ERP, module, target object, history and grain.")
    erps = erp_names()
    with st.form("cfg_form"):
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("Project name", c.get("project_name"))
        mtype = c2.radio("Mapping type", cfg.MAPPING_TYPES, horizontal=True,
                         index=cfg.MAPPING_TYPES.index(c.get("mapping_type", "Single ERP")))
        author = c3.text_input("Author / owner", c.get("author"))
        default_erps = [e for e in c.get("source_erps", []) if e in erps]
        sel_erps = st.multiselect("Source ERP system(s) - first is primary for Single ERP", erps, default=default_erps)
        c1, c2 = st.columns(2)
        modules = sorted(set(cfg.DEFAULT_MODULES + [c.get("source_module") or "Delivery"]))
        module = c1.selectbox("Source module", modules, index=modules.index(c.get("source_module") or "Delivery"))
        process = c2.text_input("Business process", c.get("business_process"), placeholder="e.g. Outbound delivery & shipment")
        st.markdown("**Source**")
        c1, c2, c3 = st.columns(3)
        sdb = c1.text_input("Source database", c.get("source_database"))
        ssch = c2.text_input("Default source schema (if not in metadata)", c.get("source_schema"))
        client = c3.text_input("SAP client filter (MANDT, optional)", c.get("sap_client"))
        st.markdown("**Target**")
        c1, c2, c3, c4 = st.columns([2, 2, 3, 1])
        tdb = c1.text_input("Target database", c.get("target_database"))
        tsch = c2.text_input("Target schema", c.get("target_schema"))
        tobj = c3.text_input("Target table / view name", c.get("target_object"))
        ttype = c4.selectbox("Type", ["VIEW", "TABLE"], index=["VIEW", "TABLE"].index(c.get("target_object_type", "VIEW")))
        st.markdown("**Grain, history and load**")
        c1, c2, c3 = st.columns(3)
        grain = c1.text_input("Expected data grain", c.get("expected_grain"), placeholder="e.g. One row per delivery item")
        hist = c2.selectbox("Historical data requirement", cfg.HISTORY_OPTIONS,
                            index=cfg.HISTORY_OPTIONS.index(c.get("history_requirement", cfg.HISTORY_OPTIONS[0])))
        load = c3.selectbox("Load type", cfg.LOAD_TYPES, index=cfg.LOAD_TYPES.index(c.get("load_type", "Full load")))
        filters = st.text_area("Business filters on the driving table (one SQL predicate per line, use the table alias)",
                               c.get("source_filters"), height=80, placeholder="lips.LFIMG <> 0")
        strip = st.checkbox("Strip leading zeros by default on ERP key columns listed in ERP Master",
                            c.get("strip_leading_zeros_default", False))
        if st.form_submit_button("Save configuration", type="primary"):
            c.update({"project_name": name, "mapping_type": mtype, "author": author, "source_erps": sel_erps,
                      "source_module": module, "business_process": process, "source_database": sdb,
                      "source_schema": ssch, "sap_client": client, "target_database": tdb, "target_schema": tsch,
                      "target_object": tobj, "target_object_type": ttype, "expected_grain": grain,
                      "history_requirement": hist, "load_type": load, "source_filters": filters,
                      "strip_leading_zeros_default": strip})
            p.touch()
            if mapped():
                regenerate_outputs(p)
            st.success("Configuration saved." + (" SQL regenerated." if mapped() else ""))
    if mtype == "Cross ERP" and len(sel_erps) < 2:
        st.info("Cross ERP mapping produces one branch per ERP combined with UNION ALL. Select two or more ERPs "
                "and load metadata for each.")


def page_context() -> None:
    p = project()
    banner("Business Context", "Requirements in plain language. Used to rank relevant tables - never to invent them.")
    examples = [
        "Need to map delivery information across SAP and iScala.",
        "Identify source tables for shipment quantity.",
        "Map material, plant, sales organization, delivery number, shipment number, and movement type.",
        "Identify relationships between delivery header, delivery item, shipment header, and shipment item.",
    ]
    with st.expander("Examples"):
        for e in examples:
            st.markdown(f"- {e}")
    text = st.text_area("Business requirements / mapping context", p.business_context, height=320)
    if st.button("Save context", type="primary"):
        p.business_context = text
        p.touch()
        st.success("Saved.")
    if p.source_metadata:
        st.markdown("#### Table relevance from this context")
        from mapping_engine import table_relevance
        rows = []
        for erp in p.erps:
            info = table_info(p.metadata_for(erp))
            for t, s in table_relevance(info, text, p.target_columns).items():
                rows.append({"ERP": erp, "Table": t, "Description": info[t]["description"], "Relevance": s})
        st.dataframe(pd.DataFrame(rows).sort_values("Relevance", ascending=False), hide_index=True, width="stretch",
                     column_config={"Relevance": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})


def page_metadata() -> None:
    p = project()
    banner("Source Metadata", "Upload or paste table/column metadata. Only supplied metadata is used for mappings and joins.")
    erps = erp_names()
    primary = (p.config.get("source_erps") or [erps[0]])[0]
    c1, c2 = st.columns(2)
    default_erp = c1.selectbox("Default ERP for rows without an ERP column", erps,
                               index=erps.index(primary) if primary in erps else 0)
    default_schema = c2.text_input("Default schema for rows without a schema", p.config.get("source_schema", ""))

    t_up, t_paste, t_manual, t_view = st.tabs(["📤 Upload files", "📋 Paste", "✍️ Manual entry", "📚 Loaded metadata"])
    with t_up:
        st.caption("CSV, TSV, Excel (all sheets), JSON. Recognised headers include the app template, SAP DD03L/DD03M "
                   "extracts (TABNAME, FIELDNAME, KEYFLAG, DATATYPE, LENG, DDTEXT, CHECKTABLE) and "
                   "INFORMATION_SCHEMA.COLUMNS extracts. A 'References' column (TABLE.COLUMN; ...) declares FK joins.")
        files = st.file_uploader("Metadata files", type=["csv", "tsv", "txt", "xlsx", "xls", "json"],
                                 accept_multiple_files=True, key="meta_files")
        if files and st.button("Import files", type="primary"):
            import_metadata([(f.name, f.getvalue()) for f in files], default_erp, default_schema)
        with st.expander("Templates and samples"):
            for f in ["source_metadata_template.csv", "sap_s4hana_delivery_shipment_metadata.csv",
                      "sap_dd03l_style_extract.csv"]:
                path = cfg.SAMPLE_DATA_DIR / f
                st.download_button(f"⬇️ {f}", path.read_bytes(), file_name=f, key=f"dl_{f}")
    with t_paste:
        txt = st.text_area("Paste rows with a header (tab, comma, | or ; separated)", height=220,
                           placeholder="Table Name\tColumn Name\tData Type\tColumn Description\tPrimary Key\nLIKP\tVBELN\tCHAR(10)\tDelivery\tY")
        if txt and st.button("Import pasted metadata", type="primary"):
            import_metadata([("pasted.txt", txt)], default_erp, default_schema)
    with t_manual:
        st.caption("Add rows below, then apply. Use 'References' as TABLE.COLUMN to declare a foreign key.")
        blank = pd.DataFrame([{f: "" for f in SOURCE_METADATA_FIELDS} | {"erp_name": default_erp,
                                                                        "schema_name": default_schema,
                                                                        "is_primary_key": False, "is_foreign_key": False}])
        edited, _ = editable_table(blank, "meta_manual", search=False, column_config=_meta_cc(erps))
        if st.button("Add rows to metadata"):
            rows = edited[(edited["table_name"].astype(str).str.strip() != "") & (edited["column_name"].astype(str).str.strip() != "")]
            if rows.empty:
                st.warning("Enter at least a table and column name.")
            else:
                norm, warns = normalize_source_metadata(rows, default_erp, default_schema)
                p.source_metadata = merge_metadata(p.source_metadata, norm)
                p.touch()
                bump_editor("meta_manual")
                st.success(f"Added {len(norm)} row(s).")
                st.rerun()
    with t_view:
        if not p.source_metadata:
            st.info("No metadata loaded yet.")
        else:
            df = p.df("source_metadata", SOURCE_METADATA_FIELDS)
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("ERPs", df["erp_name"].nunique())
            m2.metric("Tables", df.groupby(["erp_name", "table_name"]).ngroups)
            m3.metric("Columns", len(df))
            m4.metric("Declared FK columns", int((df["references"].astype(str) != "").sum()))
            edited, changed = editable_table(df, "meta_all", column_config=_meta_cc(erps), height=460)

            def _apply():
                norm, warns = normalize_source_metadata(edited)
                p.source_metadata = norm.to_dict("records")
                p.touch()
                for w in warns:
                    st.toast(w)
            apply_bar("meta_all", changed, _apply)
            if st.button("🗑️ Clear all metadata"):
                p.source_metadata = []
                bump_editor("meta_all")
                st.rerun()


def _meta_cc(erps):
    return {
        "erp_name": st.column_config.SelectboxColumn("ERP", options=erps + ["Other"]),
        "is_primary_key": st.column_config.CheckboxColumn("PK"),
        "is_foreign_key": st.column_config.CheckboxColumn("FK"),
        "references": st.column_config.TextColumn("References (TABLE.COLUMN)"),
    }


def import_metadata(items, default_erp, default_schema) -> None:
    p = project()
    total, msgs = 0, []
    for name, content in items:
        try:
            for frame in read_any(name, content):
                kind = detect_kind(frame)
                if kind == "source_to_target":
                    norm, warns = parse_s2t(frame)
                    _merge_targets(norm)
                    msgs.append(f"{name}: source-to-target file - {len(norm)} target column(s) with source hints added "
                                "to Target Columns.")
                elif kind == "target":
                    norm, warns = normalize_targets(frame)
                    _merge_targets(norm)
                    msgs.append(f"{name}: target requirements - {len(norm)} column(s) added to Target Columns.")
                else:
                    norm, warns = normalize_source_metadata(frame, default_erp, default_schema)
                    p.source_metadata = merge_metadata(p.source_metadata, norm)
                    total += len(norm)
                    msgs.append(f"{name}: {len(norm)} column(s), {norm['table_name'].nunique()} table(s).")
                msgs += [f"{name}: {w}" for w in warns]
        except Exception as e:
            st.error(f"{name}: {e}")
    p.touch()
    bump_editor("meta_all")
    for m in msgs:
        st.info(m)
    if total:
        st.success(f"Imported {total} metadata column(s).")


def _merge_targets(frame: pd.DataFrame) -> None:
    p = project()
    old = p.df("target_columns", TARGET_FIELDS)
    merged = pd.concat([old, frame], ignore_index=True).drop_duplicates(subset=["target_column"], keep="last")
    p.target_columns = merged[TARGET_FIELDS].to_dict("records")
    bump_editor("targets")


def page_targets() -> None:
    p = project()
    banner("Target Column Requirements", "Target columns, types, definitions, mandatory flags and rules.")
    t1, t2, t3 = st.tabs(["📚 Target columns", "📤 Upload / paste", "ℹ️ Source hints"])
    with t1:
        df = p.df("target_columns", TARGET_FIELDS)
        if df.empty:
            df = pd.DataFrame([{f: "" for f in TARGET_FIELDS} | {"mandatory": False, "target_data_type": "VARCHAR"}])
        df["mandatory"] = df["mandatory"].map(to_bool)
        edited, changed = editable_table(df, "targets", height=520, column_config={
            "target_column": st.column_config.TextColumn("Target column", required=True),
            "mandatory": st.column_config.CheckboxColumn("Mandatory"),
            "source_hint": st.column_config.TextColumn("Source hint (TABLE.COLUMN; ...)",
                                                      help="Optional. A hint that exists in metadata becomes a Confirmed mapping."),
        })

        def _apply():
            norm, _ = normalize_targets(edited)
            p.target_columns = norm.to_dict("records")
            p.touch()
        apply_bar("targets", changed, _apply)
    with t2:
        files = st.file_uploader("Target requirements or source-to-target mapping file", type=["csv", "xlsx", "json", "txt"],
                                 accept_multiple_files=True, key="tgt_files")
        txt = st.text_area("...or paste rows with a header", height=160,
                           placeholder="Target Column,Target Data Type,Business Definition,Mandatory\nDELIVERY_NUMBER,VARCHAR(10),Delivery number,Y")
        if st.button("Import targets", type="primary"):
            items = [(f.name, f.getvalue()) for f in files or []] + ([("pasted.txt", txt)] if txt.strip() else [])
            for name, content in items:
                try:
                    for frame in read_any(name, content):
                        norm, _ = parse_s2t(frame) if detect_kind(frame) == "source_to_target" else normalize_targets(frame)
                        _merge_targets(norm)
                        st.success(f"{name}: {len(norm)} target column(s).")
                except Exception as e:
                    st.error(f"{name}: {e}")
            p.touch()
        for f in ["target_columns_template.csv", "sap_delivery_target_columns.csv", "source_to_target_template.csv"]:
            st.download_button(f"⬇️ {f}", (cfg.SAMPLE_DATA_DIR / f).read_bytes(), file_name=f, key=f"dlt_{f}")
    with t3:
        st.markdown("""
- **Source hint** (`TABLE.COLUMN`, several separated by `;`) records a source the business has already agreed.
  If it exists in supplied metadata the mapping is **Confirmed**; if not, it is **Missing** - it is never invented.
- **Expected transformation** keywords the engine understands: *strip leading zeros*, *upper*, *concat / combine*
  (with `separator '-'`), *first non-null* (COALESCE), a full `CASE WHEN ... END`, or *Constant 'VALUE'*.
- A **source-to-target file** (Target Column, Source Table, Source Column, ...) is converted to targets with hints.
""")


def page_generate() -> None:
    p = project()
    banner("Generate Mapping", "Metadata-driven matching, join analysis, grain, transformations and SQL - with optional AI.")
    ready = bool(p.source_metadata) and bool(p.target_columns)
    c1, c2, c3 = st.columns(3)
    c1.metric("Metadata columns", len(p.source_metadata))
    c2.metric("Target columns", len(p.target_columns))
    c3.metric("ERPs with metadata", sum(1 for e in p.erps if not p.metadata_for(e).empty))
    if not ready:
        st.warning("Load source metadata and target columns first.")
        return
    keep = st.checkbox("Keep mappings already Confirmed or edited by a user", True)
    provider_name = st.session_state.get("ai_provider", cfg.AI_PROVIDERS[0])
    provider = get_provider(provider_name, st.session_state.get("ai_key"), st.session_state.get("ai_model"))
    use_ai = st.checkbox(f"Also ask AI for recommendations ({provider_name})", False, disabled=not provider.available(),
                         help="AI output is verified against metadata; unverifiable tables/columns/joins are rejected.")
    if not provider.available():
        st.caption("AI is off: no API key configured (see 🔑 AI Settings). The metadata engine works without AI.")
    st.info("Rules: only supplied metadata is used · joins are Confirmed only when declared in metadata or by a user · "
            "everything else is a recommendation with a confidence score.")
    if st.button("🚀 Generate mapping", type="primary"):
        bar = st.progress(0.0, "Starting...")
        with st.status("Generating mapping", expanded=True) as status:
            def prog(x, msg):
                bar.progress(min(x, 1.0), msg)
                st.write(msg)
            run_analysis(p, keep_confirmed=keep, progress=prog)
            if use_ai:
                erps = sorted({m["source_erp"] for m in p.column_mappings})
                for erp in erps:
                    st.write(f"{erp}: requesting AI recommendations...")
                    try:
                        result = recommend(p, erp, provider)
                        rep = apply_ai_result(p, erp, result)
                        st.write(f"AI: {rep['accepted_mappings']} mapping(s) and {rep['accepted_joins']} join(s) accepted "
                                 f"after verification; {len(rep['rejected'])} item(s) rejected.")
                    except Exception as e:
                        st.error(f"AI step failed for {erp}: {e}")
                regenerate_outputs(p)
            p.save_version("Auto: after mapping generation")
            status.update(label="Mapping generated", state="complete")
        bump_all_editors()
        st.success("Done. Review and edit the results before exporting.")
        if st.button("Open Review & Preview →"):
            go("🔍 Review & Preview")


# --------------------------------------------------------------------------- review
def page_review() -> None:
    p = project()
    banner("Review & Preview", "Inspect and edit every output. Downloads always reflect the applied state shown here.")
    if not mapped():
        st.info("No mapping yet. Load the sample project or run 🤖 Generate Mapping.")
        return
    tabs = st.tabs(["Mapping Summary", "Source Table Mapping", "Column-Level Mapping", "Join Conditions", "Data Grain",
                    "Transformation Rules", "Snowflake SQL", "Data Quality Checks", "Technical Documentation",
                    "Open Questions"])
    with tabs[0]:
        tab_summary(p)
    with tabs[1]:
        tab_tables(p)
    with tabs[2]:
        tab_columns(p)
    with tabs[3]:
        tab_joins(p)
    with tabs[4]:
        tab_grain(p)
    with tabs[5]:
        tab_transform(p)
    with tabs[6]:
        tab_sql(p)
    with tabs[7]:
        tab_dq(p)
    with tabs[8]:
        tab_doc(p)
    with tabs[9]:
        tab_questions(p)


def tab_summary(p: MappingProject) -> None:
    counts = summary_counts(p)
    cols = st.columns(5)
    cols[0].metric("Target columns", len(p.column_mappings))
    for i, (k, v) in enumerate(counts.items(), 1):
        cols[i].metric(f"{cfg.STATUS_ICONS[k]} {k}", v)
    total = max(len(p.column_mappings), 1)
    st.progress(counts[cfg.STATUS_CONFIRMED] / total, f"{counts[cfg.STATUS_CONFIRMED] / total:.0%} confirmed")
    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown("##### Grain")
        for erp, g in p.grain.items():
            st.markdown(f"**{erp}** - {g.get('grain_description')} {badge(g.get('status', ''))}", unsafe_allow_html=True)
        st.markdown("##### Joins")
        jd = pd.DataFrame(p.joins)
        if not jd.empty:
            jd = jd[jd["include"].map(to_bool)]
            st.markdown(" ".join(f"{badge(s)} × {n}" for s, n in jd["status"].value_counts().items()), unsafe_allow_html=True)
        st.markdown("##### Validation")
        issues = pd.DataFrame(p.validation_issues)
        if issues.empty:
            st.success("No validation issues.")
        else:
            st.dataframe(issues, hide_index=True, width="stretch")
    with c2:
        st.markdown("##### Status by source table")
        md = pd.DataFrame(p.column_mappings)
        md["source_table"] = md["source_table"].replace("", "(none)").fillna("(none)")
        pivot = md.pivot_table(index="source_table", columns="status", values="target_column", aggfunc="count", fill_value=0)
        palette = {cfg.STATUS_CONFIRMED: "#2e7d32", cfg.STATUS_SUGGESTED: "#f9a825",
                   cfg.STATUS_REQUIRES_VALIDATION: "#ef6c00", cfg.STATUS_MISSING: "#c62828"}
        st.bar_chart(pivot, horizontal=True, height=260, color=[palette.get(c, "#90a4ae") for c in pivot.columns])
        hi = [q for q in p.open_questions if q.get("severity") == "High" and q.get("resolution_status") == "Open"]
        st.metric("High-severity open questions", len(hi))


def tab_tables(p: MappingProject) -> None:
    st.caption("Tables in the join path (driving table first). Description and purpose are editable.")
    df = p.df("table_mapping", TABLE_MAPPING_FIELDS)
    edited, changed = editable_table(df, "tables", num_rows="fixed",
                                     disabled=[c for c in df.columns if c not in ("table_description", "table_purpose")],
                                     column_config={"relevance": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})

    def _apply():
        p.set_records("table_mapping", edited)
    apply_bar("tables", changed, _apply)


def tab_columns(p: MappingProject) -> None:
    st.caption("Edit sources, status or SQL. Editing the transformation logic marks it 'Custom SQL'; changing a "
               "source records it as a user correction (evidence = User confirmed).")
    df = p.df("column_mappings", MAPPING_FIELDS)
    tables = sorted({r["table_name"] for r in p.source_metadata})
    order = ["target_column", "status", "confidence", "source_erp", "source_table", "source_column",
             "transformation_type", "transformation_logic", "target_data_type", "source_data_type", "mandatory",
             "join_dependency", "business_rule", "evidence", "reasoning", "source_schema"]
    edited, changed = editable_table(df, "columns", height=520, column_order=order, column_config={
        "status": st.column_config.SelectboxColumn("Status", options=cfg.MAPPING_STATUSES, required=True),
        "confidence": st.column_config.ProgressColumn("Confidence", min_value=0, max_value=1, format="%.2f"),
        "source_table": st.column_config.SelectboxColumn("Source table", options=[""] + tables),
        "source_column": st.column_config.TextColumn("Source column(s)", help="COL, or TABLE.COL for other tables; comma-separated"),
        "transformation_type": st.column_config.SelectboxColumn("Transformation", options=TRANSFORMATION_TYPES),
        "transformation_logic": st.column_config.TextColumn("SQL expression", width="large"),
        "mandatory": st.column_config.CheckboxColumn("Mandatory"),
        "reasoning": st.column_config.TextColumn("Reasoning", width="large"),
    }, disabled=["target_column", "source_erp", "confidence", "evidence", "join_dependency", "source_data_type",
                 "source_schema"])

    def _apply():
        old = {(m["source_erp"], m["target_column"]): m for m in p.column_mappings}
        recs = edited.astype(object).where(pd.notna(edited), None).to_dict("records")
        for r in recs:
            prev = old.get((r.get("source_erp"), r.get("target_column")), {})
            for k in ("transformation_note",):
                if k in prev:
                    r[k] = prev[k]
            if prev and clean_str(r.get("transformation_logic")) != clean_str(prev.get("transformation_logic")) \
                    and r.get("transformation_type") == prev.get("transformation_type"):
                r["transformation_type"] = "Custom SQL"
            src_changed = prev and (clean_str(r.get("source_table")) != clean_str(prev.get("source_table")) or
                                    clean_str(r.get("source_column")) != clean_str(prev.get("source_column")))
            status_changed = prev and r.get("status") != prev.get("status")
            if src_changed:
                r["evidence"], r["confidence"] = cfg.EVIDENCE_USER, 1.0
                if not status_changed:
                    r["status"] = cfg.STATUS_CONFIRMED if clean_str(r.get("source_column")) else cfg.STATUS_MISSING
                r["reasoning"] = "Manually corrected by user. " + clean_str(prev.get("reasoning"))
                if r.get("transformation_type") != "Custom SQL":
                    r["transformation_logic"] = ""
            elif status_changed and r.get("status") == cfg.STATUS_CONFIRMED:
                r["evidence"] = cfg.EVIDENCE_USER
                r["reasoning"] = "Confirmed by user. " + clean_str(prev.get("reasoning"))
            r["source_table"] = clean_str(r.get("source_table")).upper()
        p.column_mappings = recs
        regenerate_outputs(p)
    apply_bar("columns", changed, _apply, "Apply & regenerate SQL")

    with st.expander("🔎 Mapping details and reasoning"):
        sel = st.selectbox("Target column", [m["target_column"] + f"  ({m['source_erp']})" for m in p.column_mappings])
        m = p.column_mappings[[m["target_column"] + f"  ({m['source_erp']})" for m in p.column_mappings].index(sel)]
        st.markdown(f"{badge(m.get('status'))} confidence **{float(m.get('confidence') or 0):.2f}** · evidence: "
                    f"*{m.get('evidence')}*", unsafe_allow_html=True)
        st.markdown(f"**Reasoning:** {m.get('reasoning')}")
        st.code(m.get("transformation_logic") or "", language="sql")
        st.caption(m.get("transformation_note", ""))


def tab_joins(p: MappingProject) -> None:
    erps = sorted({j["source_erp"] for j in p.joins} | set(p.grain))
    for erp in erps:
        tree = join_tree_for(p, erp)
        g = p.grain.get(erp, {})
        with st.expander(f"Join diagram - {erp}", expanded=True):
            c1, c2 = st.columns([3, 2])
            with c1:
                tables = tree.get("tables", []) + tree.get("unreachable", [])
                used = [j for j in p.joins if j["source_erp"] == erp and
                        (j["left_table"] in tables and j["right_table"] in tables)]
                st.graphviz_chart(join_diagram_dot(tables, used, g.get("grain_table")))
                st.caption("Solid green = Confirmed (metadata FK / user) · dashed orange = recommendation requiring validation.")
            with c2:
                st.code(join_diagram_text(tree), language="text")
    st.markdown("##### Join definitions")
    st.caption("Untick 'include' to stop using a join. Set status to Confirmed only after functional validation. "
               "Add a row to define a join yourself (evidence = User confirmed).")
    df = p.df("joins", JOIN_FIELDS)
    df["include"] = df["include"].map(to_bool)
    edited, changed = editable_table(df, "joins", height=330, column_config={
        "join_type": st.column_config.SelectboxColumn("Join type", options=cfg.JOIN_TYPES),
        "cardinality": st.column_config.SelectboxColumn("Cardinality", options=cfg.CARDINALITIES),
        "status": st.column_config.SelectboxColumn("Status", options=cfg.MAPPING_STATUSES),
        "fanout_handling": st.column_config.SelectboxColumn("Fan-out handling", options=FANOUT_OPTIONS),
        "include": st.column_config.CheckboxColumn("Include"),
        "confidence": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f"),
        "reasoning": st.column_config.TextColumn("Reasoning", width="large"),
    }, disabled=["join_id", "evidence", "confidence"])

    def _apply():
        old = {j["join_id"]: j for j in p.joins}
        recs = edited.astype(object).where(pd.notna(edited), None).to_dict("records")
        for r in recs:
            prev = old.get(r.get("join_id"))
            r["left_table"] = clean_str(r.get("left_table")).upper()
            r["right_table"] = clean_str(r.get("right_table")).upper()
            if not prev:  # new user-defined join
                r.update({"source_erp": r.get("source_erp") or (p.config.get("source_erps") or [""])[0],
                          "evidence": cfg.EVIDENCE_USER, "confidence": 1.0,
                          "status": r.get("status") or cfg.STATUS_CONFIRMED, "include": True,
                          "join_type": r.get("join_type") or "LEFT JOIN",
                          "fanout_handling": r.get("fanout_handling") or FANOUT_OPTIONS[0],
                          "reasoning": r.get("reasoning") or "Defined by user."})
            elif r.get("status") == cfg.STATUS_CONFIRMED and prev.get("status") != cfg.STATUS_CONFIRMED:
                r["evidence"] = cfg.EVIDENCE_USER
                r["reasoning"] = "Confirmed by user. " + clean_str(prev.get("reasoning"))
        from join_analyzer import number_joins
        p.joins = number_joins(recs)
        regenerate_outputs(p)
    apply_bar("joins", changed, _apply, "Apply & regenerate SQL")


def tab_grain(p: MappingProject) -> None:
    for erp, g in p.grain.items():
        info = table_info(p.metadata_for(erp))
        tree = join_tree_for(p, erp)
        st.markdown(f"#### {erp} {badge(g.get('status', ''))}", unsafe_allow_html=True)
        st.markdown(f"**{g.get('grain_description')}** · confidence {float(g.get('confidence') or 0):.2f}")
        st.caption(g.get("reasoning", ""))
        for w in g.get("warnings", []):
            st.warning(w)
        with st.form(f"grain_{erp}"):
            tables = list(info)
            c1, c2 = st.columns(2)
            gt = c1.selectbox("Grain (driving) table", tables, index=tables.index(g["grain_table"]) if g.get("grain_table") in tables else 0)
            cols = info[gt]["columns"]
            keys = c2.multiselect("Grain / ROW_NUMBER partition keys", cols,
                                  default=[k for k in g.get("grain_keys", []) if k in cols] if gt == g.get("grain_table") else info[gt]["pk"])
            st.caption(f"ROW_NUMBER order: {', '.join(c + ' DESC' for c in find_change_columns(gt, info, erp)) or 'key columns (no change date found)'}")
            confirm = st.checkbox("Confirm this grain", g.get("status") == cfg.STATUS_CONFIRMED)
            if st.form_submit_button("Apply grain & regenerate SQL", type="primary"):
                chg = find_change_columns(gt, info, erp)
                g.update({"grain_table": gt, "grain_keys": keys, "dedup_partition": keys,
                          "dedup_order": [f"{c} DESC" for c in chg] or [f"{k} DESC" for k in keys],
                          "change_columns": chg,
                          "grain_description": f"One row per {info[gt]['description'] or gt} ({', '.join(keys)})",
                          "status": cfg.STATUS_CONFIRMED if confirm else cfg.STATUS_REQUIRES_VALIDATION,
                          "confidence": 1.0 if confirm else g.get("confidence", 0.5),
                          "reasoning": ("Confirmed by user. " if confirm else "Changed by user. ") + clean_str(g.get("reasoning")),
                          "warnings": [] if confirm else g.get("warnings", [])})
                if gt != tree.get("tables", [None])[0]:
                    for j in p.joins:
                        if j.get("source_erp") == erp:
                            j["fanout_handling"] = FANOUT_OPTIONS[0]
                regenerate_outputs(p)
                bump_all_editors()
                st.rerun()
        st.markdown("**Duplicate and row-multiplication risks**")
        for r in duplicate_risks(erp, p.metadata_for(erp), tree, g):
            st.markdown(f"- {r}")
        st.markdown(f"**History handling:** {p.config.get('history_requirement')} - change in ⚙️ Configuration.")


def tab_transform(p: MappingProject) -> None:
    st.caption("Transformation logic per target column, grouped by type. Edit expressions in Column-Level Mapping.")
    df = pd.DataFrame([{"target_column": m["target_column"], "source_erp": m.get("source_erp"),
                        "type": m.get("transformation_type"), "expression": m.get("transformation_logic"),
                        "explanation": m.get("transformation_note", "")} for m in p.column_mappings])
    for t, grp in df.groupby("type", sort=False):
        with st.expander(f"{t} ({len(grp)})", expanded=t in ("Date conversion", "Derived", "Conditional (CASE)", "Custom SQL")):
            st.dataframe(grp, hide_index=True, width="stretch",
                         column_config={"expression": st.column_config.TextColumn(width="large")})


def tab_sql(p: MappingProject) -> None:
    c1, c2 = st.columns([3, 1])
    which = c1.radio("Script", list(SQL_LABELS), format_func=lambda k: SQL_LABELS[k], horizontal=True)
    if c2.button("🔄 Regenerate SQL", width="stretch"):
        regenerate_outputs(p)
        st.rerun()
    st.code(p.sql.get(which, ""), language="sql", line_numbers=True)
    st.download_button("⬇️ Download all scripts (.sql)", combined_script(p.sql).encode(),
                       file_name=f"{safe_name(p.config.get('project_name'))}.sql", mime="text/plain")


def tab_dq(p: MappingProject) -> None:
    st.markdown("Checks: **DQ-01** record counts · **DQ-02** duplicates on grain · **DQ-03** mandatory nulls · "
                "**DQ-04** referential integrity & fan-out profile · **DQ-05** failed type conversions · **DQ-06** business rules")
    st.code(p.sql.get("data_quality", ""), language="sql", line_numbers=True)


def tab_doc(p: MappingProject) -> None:
    doc = build_document(p)
    st.markdown(f"### {doc['title']}")
    for s in doc["sections"]:
        with st.expander(s["title"], expanded=s["title"].startswith("1.")):
            for b in s["blocks"]:
                if b["type"] == "h3":
                    st.markdown(f"**{b['text']}**")
                elif b["type"] == "p":
                    st.markdown(b["text"])
                elif b["type"] == "bullets":
                    st.markdown("\n".join(f"- {i}" for i in b["items"]))
                elif b["type"] == "code":
                    st.code(b["text"], language="sql" if "SELECT" in b["text"] else "text")
                elif b["type"] == "table":
                    st.dataframe(pd.DataFrame(b["rows"], columns=b["columns"]), hide_index=True, width="stretch")
    st.download_button("⬇️ Markdown version", to_markdown(doc).encode(), file_name=f"{safe_name(p.config.get('project_name'))}.md")


def tab_questions(p: MappingProject) -> None:
    st.caption("Generated from missing sources, unconfirmed joins, grain assumptions and AI rejections. Assign owners, "
               "set resolution status, or add your own rows (category 'User').")
    df = p.df("open_questions", OPEN_QUESTION_FIELDS)
    edited, changed = editable_table(df, "questions", height=460, column_config={
        "severity": st.column_config.SelectboxColumn(options=["High", "Medium", "Low"]),
        "resolution_status": st.column_config.SelectboxColumn("Status", options=["Open", "In progress", "Resolved", "Accepted risk"]),
        "detail": st.column_config.TextColumn(width="large"),
    }, disabled=["id"])

    def _apply():
        recs = edited.astype(object).where(pd.notna(edited), None).to_dict("records")
        for r in recs:
            if not r.get("category"):
                r["category"] = "User"
            r.setdefault("resolution_status", "Open")
        for i, r in enumerate(recs, 1):
            r["id"] = f"Q{i:03d}"
        p.open_questions = recs
        p.touch()
    apply_bar("questions", changed, _apply)


# --------------------------------------------------------------------------- export
def page_export() -> None:
    p = project()
    banner("Export", "Every file is built from the current, reviewed project state.")
    if not mapped():
        st.info("Generate or load a mapping first.")
        return
    counts = summary_counts(p)
    st.markdown(" ".join(f"{badge(k)} {v}" for k, v in counts.items()), unsafe_allow_html=True)
    errors = [i for i in p.validation_issues if i["severity"] == "Error"]
    if errors:
        st.warning(f"{len(errors)} validation error(s) remain - exports are allowed but flag these in Section 10.")
    open_hi = [q for q in p.open_questions if q.get("severity") == "High" and q.get("resolution_status") == "Open"]
    if open_hi:
        st.caption(f"{len(open_hi)} high-severity open question(s) are included in the documentation.")
    with st.expander("Preview column mapping (CSV content)"):
        st.dataframe(p.df("column_mappings", MAPPING_FIELDS), hide_index=True, width="stretch")
    base = safe_name(p.config.get("project_name"))
    cols = st.columns(3)
    for i, (ext, spec) in enumerate(EXPORTS.items()):
        with cols[i % 3]:
            try:
                data = spec["fn"](p)
                st.download_button(f"⬇️ {spec['label']}", data, file_name=f"{base}.{ext}", mime=spec["mime"],
                                   width="stretch", key=f"exp_{ext}")
            except Exception as e:
                st.error(f"{ext}: {e}")
    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Save version snapshot of this export state"):
            v = p.save_version("Export snapshot")
            st.success(f"Saved version {v['version']}.")
    with c2:
        if st.button("Also write files to generated_outputs/"):
            out = cfg.OUTPUT_DIR / base
            out.mkdir(parents=True, exist_ok=True)
            for ext, spec in EXPORTS.items():
                (out / f"{base}.{ext}").write_bytes(spec["fn"](p))
            st.success(f"Written to {out.relative_to(cfg.BASE_DIR)}")


# --------------------------------------------------------------------------- ERP master
def page_erp_master() -> None:
    banner("ERP Master", "Add or change ERP systems without code changes. Stored in templates/erp_master.json.")
    erps = load_erps()
    rows = []
    for e in erps:
        r = {k: e.get(k) for k in ERP_FIELDS}
        for k in ("aliases", "null_date_values", "change_column_candidates", "leading_zero_columns"):
            r[k] = ", ".join(r.get(k) or [])
        rows.append(r)
    df = pd.DataFrame(rows, columns=ERP_FIELDS)
    st.caption("Conventions only apply to columns that exist in supplied metadata: date format drives date conversion, "
               "client column drives the client filter, change columns drive ROW_NUMBER ordering.")
    edited, changed = editable_table(df, "erp_master", height=420, column_config={
        "name": st.column_config.TextColumn("ERP name", required=True),
        "date_format": st.column_config.SelectboxColumn("Date format", options=list(DATE_FORMATS)),
        "active": st.column_config.CheckboxColumn("Active"),
        "description": st.column_config.TextColumn(width="large"),
    })

    def _apply():
        out = []
        for r in edited.astype(object).where(pd.notna(edited), None).to_dict("records"):
            if not clean_str(r.get("name")):
                continue
            for k in ("aliases", "null_date_values", "change_column_candidates", "leading_zero_columns"):
                r[k] = split_list(r.get(k))
            r["active"] = to_bool(r.get("active")) if r.get("active") is not None else True
            out.append(r)
        save_erps(out)
    apply_bar("erp_master", changed, _apply, "Save ERP master")
    with st.expander("Date formats"):
        st.table(pd.DataFrame(DATE_FORMATS.items(), columns=["Code", "Meaning"]))


def page_settings() -> None:
    banner("AI Settings", "Optional. The mapping engine is fully functional without AI.")
    prov = st.selectbox("Provider", cfg.AI_PROVIDERS, index=cfg.AI_PROVIDERS.index(st.session_state.get("ai_provider", cfg.AI_PROVIDERS[0])))
    st.session_state.ai_provider = prov
    configured = bool(cfg.get_secret("ANTHROPIC_API_KEY"))
    st.markdown(f"Server key (`ANTHROPIC_API_KEY` in secrets/env): {'✅ configured' if configured else '❌ not set'}")
    key = st.text_input("Session API key (kept in memory for this browser session only; never saved)",
                        type="password", value=st.session_state.get("ai_key", ""))
    st.session_state.ai_key = key or None
    model = st.text_input("Model", st.session_state.get("ai_model") or cfg.get_secret("ANTHROPIC_MODEL") or cfg.DEFAULT_ANTHROPIC_MODEL)
    st.session_state.ai_model = model
    provider = get_provider(prov, st.session_state.ai_key, model)
    st.markdown(f"Status: {'🟢 ready' if provider.available() else '⚪ not available'}")
    if provider.available() and st.button("Test connection"):
        try:
            with st.spinner("Calling model..."):
                txt = provider.complete("Reply with the single word OK.", "Ping")
            st.success(f"Response: {txt[:50]}")
        except Exception as e:
            st.error(str(e))
    st.markdown("""
**How AI is used safely**
- The prompt contains only your metadata, target columns and context, and instructs the model to use nothing else.
- Every returned table, column and join is checked against metadata; anything unverifiable is rejected and listed in Open Questions.
- AI mappings are **Suggested**, AI joins are **Requires Validation**. Nothing from AI is ever Confirmed automatically.
""")


# =========================================================================== main
def main() -> None:
    page = sidebar()
    {
        "🏠 Projects": page_projects, "⚙️ Configuration": page_configuration, "📝 Business Context": page_context,
        "🗂️ Source Metadata": page_metadata, "🎯 Target Columns": page_targets, "🤖 Generate Mapping": page_generate,
        "🔍 Review & Preview": page_review, "⬇️ Export": page_export, "🏭 ERP Master": page_erp_master,
        "🔑 AI Settings": page_settings,
    }[page]()


main()
