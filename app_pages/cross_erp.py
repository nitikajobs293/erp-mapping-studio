import streamlit as st

from services.comparison_service import (canonical_ddl, canonical_union_sql, compare, concepts_for_object,
                                         highlights)
from services.document_service import comparison_docx, comparison_excel
from services.validation_service import validate_sql
from utils.ui import repo, setup_page, sidebar_context, sql_settings

setup_page()
sidebar_context()
r = repo()
st.title("⚖️ Cross-ERP Comparison & Canonical Model")
st.caption("Compare the same business process across ERPs, see common / missing / differently-defined fields, and generate "
           "a canonical Snowflake model with a UNION of all sources.")

catalog = r.business_object_catalog()
names = r.erp_names()
id2name = {v: k for k, v in names.items()}
c1, c2 = st.columns([1, 3])
with c1:
    bo = st.selectbox("Business object", list(catalog), index=list(catalog).index("sales_order"),
                      format_func=lambda x: catalog[x]["label"])
with c2:
    default = [e for e in ["sap_s4hana", "iscala", "jde_e1", "syspro", "blue_yonder_wms"] if e in r.erps]
    erp_ids = st.multiselect("ERPs", list(id2name), default=default, format_func=lambda x: id2name[x])
all_concepts = concepts_for_object(r, bo)
concepts = st.multiselect("Business fields", all_concepts, default=all_concepts,
                          format_func=lambda x: r.concept_catalog()[x]["label"])
if len(erp_ids) < 2 or not concepts:
    st.info("Select at least two ERPs and one field.")
    st.stop()

matrix, details = compare(r, bo, erp_ids, concepts)
cov = matrix["Status"].value_counts().to_dict()
k = st.columns(3)
k[0].metric("Common fields", cov.get("Common", 0))
k[1].metric("Missing in some ERPs", cov.get("Missing in some ERPs", 0))
k[2].metric("Missing everywhere", cov.get("Missing everywhere", 0))


def _style(row):
    color = {"Common": "#e8f5e9", "Missing in some ERPs": "#fff8e1", "Missing everywhere": "#ffebee"}[row["Status"]]
    return [f"background-color: {color}"] * len(row)


st.dataframe(matrix.style.apply(_style, axis=1), width="stretch", hide_index=True,
             height=min(700, 40 + 35 * len(matrix)))
not_modelled = [id2name[e] for e in erp_ids if bo not in r.get(e).business_objects]
if not_modelled:
    st.warning(f"'{catalog[bo]['label']}' is not modelled as a business object for: {', '.join(not_modelled)} "
               "(fields shown come from generic concept mappings; these ERPs are skipped in the UNION SQL).")

hl = highlights(details, matrix)
t1, t2, t3, t4 = st.tabs(["🔍 Highlights", "📋 Field details", "🧱 Canonical model DDL", "🔗 Multi-ERP UNION SQL"])
with t1:
    for k_, v in hl.items():
        with st.expander(f"{k_} ({len(v)})", expanded=k_ in ("Missing fields", "Different business meanings (see notes)")):
            for i in v or ["-"]:
                st.markdown(f"- {i}")
with t2:
    st.dataframe(details, width="stretch", hide_index=True)
ddl = canonical_ddl(r, bo, concepts, sql_settings())
union = canonical_union_sql(r, bo, erp_ids, concepts, sql_settings())
with t3:
    st.code(ddl, language="sql")
    st.caption("Grain of the canonical table = the business object grain in each ERP; SOURCE_SYSTEM + SOURCE_GRAIN_KEY is the key.")
with t4:
    st.code(union, language="sql")
    for f in validate_sql(union):
        if f["level"] in ("error", "warning"):
            st.warning(f["message"])

st.divider()
title = f"Cross-ERP comparison - {catalog[bo]['label']}"
c = st.columns(3)
c[0].download_button("⬇️ Excel report", comparison_excel(matrix, details, hl, ddl, union),
                     file_name=f"cross_erp_{bo}.xlsx", width="stretch")
c[1].download_button("⬇️ Word report", comparison_docx(title, matrix, hl, ddl, union),
                     file_name=f"cross_erp_{bo}.docx", width="stretch")
c[2].download_button("⬇️ Canonical SQL", ddl + "\n\n" + union, file_name=f"canonical_{bo}.sql", width="stretch")
