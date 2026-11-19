import streamlit as st

from config.settings import QUERY_TYPES
from services.sql_service import SQLBuilder, generate_all
from services.validation_service import dq_queries, validate_sql
from utils.ui import repo, setup_page, sidebar_context, sql_settings

setup_page()
sidebar_context()
r = repo()
st.title("❄️ Snowflake SQL Generator")
res = st.session_state.current_result
if not res:
    st.info("Create a mapping first (Column Mapping, Business Object Mapping, T-Code Explorer or the Assistant).")
    st.stop()
erp = r.get(res.erp_id)
st.caption(f"Active mapping: **{res.erp_name} · {res.business_object_label}** · grain table `{res.anchor}`")

s = dict(sql_settings())
with st.expander("⚙️ Query options (this page)", expanded=False):
    c = st.columns(4)
    s["database"] = c[0].text_input("Source database", s["database"])
    s["schema"] = c[1].text_input("Source schema ({erp_id} = ERP id)", s["schema"])
    s["target_database"] = c[2].text_input("Target database", s["target_database"])
    s["target_schema"] = c[3].text_input("Target schema", s["target_schema"])
    c = st.columns(4)
    s["cdc_timestamp_column"] = c[0].text_input("CDC timestamp column", s["cdc_timestamp_column"])
    s["cdc_delete_column"] = c[1].text_input("Soft-delete flag column", s["cdc_delete_column"])
    s["date_mode"] = c[2].selectbox("ERP dates landed as", ["VARCHAR", "DATE"], index=["VARCHAR", "DATE"].index(s["date_mode"]))
    s["identifier_case"] = c[3].selectbox("Identifier case", ["UPPER", "AS_IS"], index=["UPPER", "AS_IS"].index(s["identifier_case"]))
    if erp.meta.get("client_column"):
        c = st.columns(3)
        s["apply_client_filter"] = c[0].checkbox(f"Filter client ({erp.meta['client_column']})", s["apply_client_filter"])
        s["sap_client"] = c[1].text_input("Client", s["sap_client"])
        s["language"] = c[2].text_input("Text language (SPRAS)", s["language"])
    if erp.meta.get("physical_name_template"):
        st.caption(f"Physical table names use `{erp.meta['physical_name_template']}`")
        params = dict(erp.meta.get("template_params") or {})
        cols = st.columns(max(1, len(params)))
        for i, (k, v) in enumerate(params.items()):
            params[k] = cols[i].text_input(k, v)
        s["template_params"] = params
    if st.button("Save as default"):
        st.session_state.sql_settings = s
        st.success("Saved for this session.")

qt = st.radio("Query type", QUERY_TYPES, index=1, horizontal=True)
builder = SQLBuilder(res, erp, s)
sql = builder.build(qt)
left, right = st.columns([3, 1])
with left:
    with st.expander("✏️ Edit SQL (validation and downloads use the edited text)"):
        edited = st.text_area("SQL editor", sql, height=420, key=f"sql_{qt}_{res.erp_id}_{res.business_object}",
                              label_visibility="collapsed")
    st.code(edited, language="sql")  # copy button in the top-right corner
with right:
    st.markdown("**Validation**")
    for f in validate_sql(edited):
        {"ok": st.success, "info": st.info, "warning": st.warning, "error": st.error}[f["level"]](f["message"])
    fname = f"{res.erp_id}_{res.business_object}_{qt.split()[0].lower()}.sql"
    st.download_button("⬇️ Download this .sql", edited, file_name=fname, type="primary", width="stretch")
    all_sql = generate_all(res, erp, s)
    bundle = "\n\n".join(f"-- {'=' * 70}\n-- {k}\n-- {'=' * 70}\n{v}" for k, v in all_sql.items() if "yml" not in k)
    st.download_button("⬇️ All query types (.sql)", bundle, file_name=f"{res.erp_id}_{res.business_object}_all.sql",
                       width="stretch")
    if "dbt" in qt:
        st.download_button("⬇️ dbt sources.yml", builder.dbt_sources_yml(), file_name="sources.yml", width="stretch")

with st.expander("🧪 Data-quality & reconciliation queries"):
    dq = dq_queries(res, erp, s)
    for name, q in dq.items():
        st.markdown(f"**{name}**")
        st.code(q, language="sql")
    st.download_button("⬇️ DQ checks (.sql)", "\n\n".join(f"-- {k}\n{v}" for k, v in dq.items()),
                       file_name=f"{res.erp_id}_{res.business_object}_dq.sql")
