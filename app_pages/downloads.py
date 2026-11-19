import pandas as pd
import streamlit as st

from services import document_service as ds
from services import model_service as ms
from services.sql_service import generate_all
from services.validation_service import dq_queries
from utils.components import architecture_preview
from utils.ui import repo, setup_page, sidebar_context, sql_settings

setup_page()
sidebar_context()
r = repo()
st.title("📄 Documentation & Downloads")
res = st.session_state.current_result
if not res:
    st.info("No active mapping. Create one first (Column Mapping, Business Object Mapping, T-Code Explorer or Assistant).")
    st.stop()
erp = r.get(res.erp_id)
st.caption(f"Deliverables for **{res.erp_name} {res.erp_version} · {res.business_object_label}** ({len(res.rows)} columns)")

if not st.session_state.architecture_approved:
    st.warning("Step 1 — review and approve the architecture. Documents are generated only after approval.")
    architecture_preview(res, key="dl_arch")
    st.stop()

st.success("✅ Architecture approved. Step 2 — preview each deliverable, then download.")
if st.button("↩️ Re-open architecture review"):
    st.session_state.architecture_approved = False
    st.rerun()


@st.cache_data(show_spinner="Building deliverables…")
def _build(_res, key: str, settings_key: str):
    sqls = generate_all(_res, erp, sql_settings())
    dq = dq_queries(_res, erp, sql_settings())
    return ds.build_package(_res, erp, r, sqls, dq)


pkg = _build(res, f"{res.erp_id}|{res.business_object}|{len(res.rows)}|{id(res)}", str(sorted(sql_settings().items())))
base = f"{res.erp_id}_{res.business_object}"

tabs = st.tabs(["1 · Technical document", "2 · Excel S2T mapping", "3 · SQL scripts", "4 · ER / logical model",
                "5 · Grain & relationships", "6 · DQ queries", "7 · Ingestion", "📦 Everything"])
with tabs[0]:
    st.markdown(f"#### {res.erp_name} - {res.business_object_label} technical design")
    st.markdown("Sections: scope · logical model (diagram) · tables & roles · joins/keys · fact/dim · grain & dedup · "
                "S2T mapping · Snowflake layers · warnings · ingestion · SQL · DQ queries.")
    st.dataframe(pkg["tables"], width="stretch", hide_index=True)
    c = st.columns(2)
    c[0].download_button("⬇️ Word (.docx)", ds.to_docx(res, pkg), file_name=f"{base}_technical_design.docx",
                         type="primary", width="stretch")
    c[1].download_button("⬇️ PDF", ds.to_pdf(res, pkg), file_name=f"{base}_technical_design.pdf",
                         mime="application/pdf", width="stretch")
with tabs[1]:
    st.dataframe(pkg["mapping"], width="stretch", hide_index=True)
    st.download_button("⬇️ Excel mapping (.xlsx, 11 sheets)", ds.to_excel(res, pkg), file_name=f"{base}_mapping.xlsx",
                       type="primary")
with tabs[2]:
    pick = st.selectbox("Preview", list(pkg["sqls"]))
    st.code(pkg["sqls"][pick], language="yaml" if "yml" in pick else "sql")
    st.download_button("⬇️ All Snowflake SQL (.sql)", ds.sql_script(pkg, res), file_name=f"{base}_all_queries.sql", type="primary")
with tabs[3]:
    st.graphviz_chart(pkg["dot"], width="stretch")
    c = st.columns(4)
    c[0].download_button(".dot", pkg["dot"], file_name=f"{base}_er.dot")
    c[1].download_button("Mermaid", pkg["mermaid"], file_name=f"{base}_er.mmd")
    png = ms.render_dot(pkg["dot"], "png")
    if png:
        c[2].download_button("PNG", png, file_name=f"{base}_er.png", mime="image/png")
        c[3].download_button("SVG", ms.render_dot(pkg["dot"], "svg"), file_name=f"{base}_er.svg", mime="image/svg+xml")
    else:
        st.caption("Install Graphviz (packages.txt) to export PNG/SVG.")
with tabs[4]:
    md = ds.grain_markdown(res, pkg)
    st.markdown(md)
    st.download_button("⬇️ Grain & relationships (.md)", md, file_name=f"{base}_grain_relationships.md")
with tabs[5]:
    for k, v in pkg["dq"].items():
        with st.expander(k):
            st.code(v, language="sql")
    st.download_button("⬇️ DQ queries (.sql)", ds.dq_script(pkg), file_name=f"{base}_dq_checks.sql")
with tabs[6]:
    for x in pkg["ingestion"]:
        st.markdown(f"- {x}")
    st.download_button("⬇️ Ingestion recommendations (.md)", "\n".join(f"- {x}" for x in pkg["ingestion"]),
                       file_name=f"{base}_ingestion.md")
with tabs[7]:
    st.markdown("One ZIP with: Word + PDF design, Excel mapping, all SQL variants, DQ checks, dbt model + sources.yml, "
                "ER diagram (DOT / Mermaid / PNG / SVG), layer diagram, grain doc and ingestion notes.")
    st.download_button("⬇️ Download complete bundle (.zip)", ds.to_zip(res, pkg), file_name=f"{base}_bundle.zip",
                       type="primary", width="stretch")
    st.caption("Cross-ERP comparison reports are downloaded from the Cross-ERP Comparison page.")
