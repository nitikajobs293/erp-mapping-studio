import streamlit as st

from services import model_service as ms
from services.mapping_engine import build_mapping, default_columns_for
from utils.components import architecture_preview
from utils.ui import repo, set_result, setup_page, sidebar_context, sql_settings

setup_page()
erp = sidebar_context()
r = repo()
st.title("🗺️ Data Model Designer")
st.caption("Preview the architecture before generating documents: source tables, relationships, PK/FK, cardinality, fact/dimension "
           "roles, grain, and the Snowflake staging → silver → gold design.")

res = st.session_state.current_result
mode = st.radio("Model", ["Active mapping", "Business object (knowledge base)", "Whole ERP knowledge graph"], horizontal=True)

if mode == "Active mapping":
    if not res:
        st.info("No active mapping yet. Create one in **Column Mapping**, **Business Object Mapping**, **T-Code Explorer**, "
                "or quickly from a business object below.")
        bo = st.selectbox("Quick start - business object", list(erp.business_objects))
        if bo and st.button("Build default mapping"):
            set_result(build_mapping(r, erp.id, default_columns_for(r, erp.id, bo), bo, st.session_state.erp_version, sql_settings()))
            st.rerun()
        st.stop()
    architecture_preview(res, key="dm")
    erp_r = r.get(res.erp_id)
    with st.expander("Export diagram sources"):
        dot = ms.mapping_dot(res, erp_r)
        c = st.columns(4)
        c[0].download_button("Graphviz .dot", dot, file_name=f"{res.erp_id}_{res.business_object}_er.dot")
        c[1].download_button("Mermaid .mmd", ms.mermaid_er(res, erp_r), file_name=f"{res.erp_id}_{res.business_object}_er.mmd")
        png = ms.render_dot(dot, "png")
        if png:
            c[2].download_button("PNG", png, file_name=f"{res.erp_id}_{res.business_object}_er.png", mime="image/png")
            c[3].download_button("SVG", ms.render_dot(dot, "svg"), file_name=f"{res.erp_id}_{res.business_object}_er.svg",
                                 mime="image/svg+xml")
        st.code(ms.mermaid_er(res, erp_r), language="text")
elif mode == "Business object (knowledge base)":
    bo = st.selectbox("Business object", list(erp.business_objects),
                      format_func=lambda x: r.business_object_catalog().get(x, {}).get("label", x))
    st.graphviz_chart(ms.business_object_dot(erp, bo), width="stretch")
    b = erp.business_objects[bo]
    st.info(f"**Grain:** {b.get('grain')}  \n**Model:** {b.get('model_type')}")
else:
    st.graphviz_chart(ms.full_erp_dot(erp), width="stretch")
    st.caption(f"All tables and relationships known for {erp.name}.")
