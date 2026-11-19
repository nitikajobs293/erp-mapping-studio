import pandas as pd
import streamlit as st

from config.settings import CONFIDENCE
from services import model_service as ms
from services.mapping_engine import build_mapping, default_columns_for
from utils.components import mapping_table, metrics
from utils.ui import repo, set_result, setup_page, sidebar_context, sql_settings

setup_page()
erp = sidebar_context()
r = repo()
st.title("📦 Business Object Mapping")
st.caption("Logistics-first view: pick a business object and see every canonical field, where it lives in the selected ERP, "
           "the tables involved, grain and duplicate risks.")

catalog = r.business_object_catalog()
priority = ["sales_order", "delivery", "shipment", "inventory", "inventory_movement", "serial_number", "billing",
            "purchase_order", "production_order", "returns", "stock_transfer"]
bo = st.radio("Business object", priority, horizontal=True, format_func=lambda x: catalog.get(x, {}).get("label", x))
st.caption(catalog.get(bo, {}).get("description", ""))

if bo not in erp.business_objects:
    st.warning(f"**{catalog[bo]['label']}** is not yet modelled for {erp.name}. Modelled objects: "
               f"{', '.join(catalog.get(b, {}).get('label', b) for b in erp.business_objects) or 'none'}. "
               "Returns and stock transfers reuse the sales-order / delivery / purchase-order structures with "
               "different document types (e.g. SAP returns order type RE, VBTYP H; STO = EKKO-BSART UB + delivery).")
    st.stop()

b = erp.business_objects[bo]
c = st.columns(4)
c[0].metric("Anchor (grain) table", b.get("anchor"))
c[1].metric("Header", b.get("header") or "-")
c[2].metric("Tables", len(b.get("tables", [])))
c[3].metric("Business key", ", ".join(b.get("business_key", [])) or "?")
st.info(f"**Grain:** {b.get('grain')}  \n**Model type:** {b.get('model_type')}")
if b.get("base_filter"):
    st.caption(f"Base filter: `{b['base_filter'].replace('{', '').replace('}', '')}`")

left, right = st.columns([3, 2])
with left:
    st.markdown("**Canonical field coverage**")
    rows = []
    for cid, cdef in r.concept_catalog().items():
        if bo not in cdef.get("objects", []):
            continue
        spec = erp.concept_spec(cid, bo)
        if spec:
            src = spec["expr"].replace("{", "").replace("}", "") if spec.get("expr") else f"{spec.get('table')}.{spec.get('field') or '?'}"
            lvl = spec.get("confidence", "inferred")
        else:
            src, lvl = "— not available", "unavailable"
        rows.append({"Business field": cdef["label"], "Source": src, "Target column": cdef["target"],
                     "Confidence": f"{CONFIDENCE[lvl]['icon']} {CONFIDENCE[lvl]['label']}", "Note": (spec or {}).get("note", "")})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, height=520)
with right:
    st.markdown("**Object data model (knowledge base)**")
    st.graphviz_chart(ms.business_object_dot(erp, bo), width="stretch")
    if b.get("duplicate_risks"):
        st.markdown("**Duplicate risks**")
        for d in b["duplicate_risks"]:
            st.markdown(f"- {d}")

st.divider()
cols = default_columns_for(r, erp.id, bo)
chosen = st.multiselect("Fields to include in the mapping", cols, default=cols)
if st.button("🧩 Build mapping for selected fields", type="primary", disabled=not chosen):
    res = build_mapping(r, erp.id, chosen, bo, st.session_state.erp_version, sql_settings())
    set_result(res)
    st.success("Mapping created and loaded into the workspace.")
res = st.session_state.current_result
if res and res.business_object == bo and res.erp_id == erp.id:
    metrics(res)
    mapping_table(res, key="bom")
    st.page_link("app_pages/data_model.py", label="Review architecture in Data Model Designer", icon="🗺️")
