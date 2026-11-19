import streamlit as st

from services.assistant_service import build_context, parse_request
from services.llm_service import LLMError, suggest_mappings
from services.mapping_engine import build_mapping, default_columns_for, parse_columns
from services.sql_service import generate_sql
from utils.components import architecture_preview, confidence_explainer, mapping_table, metrics
from utils.ui import llm, repo, set_result, setup_page, sidebar_context, sql_settings, store

setup_page()
erp = sidebar_context()
r = repo()
st.title("🧩 Column Mapping — source-to-target")
st.caption("Give a list of business columns; get the source table/field, joins, transformations, target Snowflake columns, "
           "availability, alternatives and confidence for each. Unknown columns are highlighted, never dropped. "
           "You can also type direct references like `LIKP.WADAT_IST`.")

catalog = r.business_object_catalog()
bo_options = ["(auto-detect)"] + list(catalog)
DEFAULT = ("Shipment Number\nShipment Date\nDelivery Number\nSales Order Number\nMaterial Number\nShipment Quantity\n"
           "Carrier\nPlant\nCustomer\nShip-to Party\nActual Goods Issue Date\nShipment Status")

c1, c2 = st.columns([1, 2])
with c1:
    bo_sel = st.selectbox("Business object", bo_options, index=bo_options.index("shipment"),
                          format_func=lambda x: catalog.get(x, {}).get("label", x))
    bo = None if bo_sel == "(auto-detect)" else bo_sel
    modelled = bo in erp.business_objects if bo else True
    if bo and not modelled:
        st.warning(f"{catalog[bo]['label']} is not modelled for {erp.name}; the closest modelled object will be used. "
                   f"Modelled: {', '.join(erp.business_objects) or 'none'}")
    if bo and modelled and st.button("➕ Insert all known fields of this object"):
        st.session_state.cols_text = "\n".join(default_columns_for(r, erp.id, bo))
        st.rerun()
    if "cols_text" not in st.session_state:
        st.session_state.cols_text = DEFAULT
with c2:
    cols_text = st.text_area("Business columns (one per line, or comma separated)", key="cols_text", height=260)

go_btn = st.button("🚀 Generate mapping", type="primary")
if go_btn:
    columns = parse_columns(cols_text)
    if not columns:
        st.error("Enter at least one column.")
        st.stop()
    st.session_state.ai_rows = []
    res = build_mapping(r, erp.id, columns, bo, version=st.session_state.erp_version, settings=sql_settings())
    set_result(res)

res = st.session_state.current_result
if not res:
    confidence_explainer()
    st.stop()
if res.erp_id != erp.id:
    st.info(f"The active mapping is for **{res.erp_name}**. Click *Generate mapping* to map for {erp.name}.")

st.divider()
st.subheader(f"{res.erp_name}{f' ({res.erp_version})' if res.erp_version else ''} — {res.business_object_label}")
metrics(res)
t1, t2, t3 = st.tabs(["📑 Source-to-target mapping", "🏗️ Architecture preview (review before documents)", "❄️ SQL preview"])
with t1:
    mapping_table(res, key="cm")
    confidence_explainer()
    missing = [x["Business Column"] for x in res.rows if x["_confidence"] == "unavailable"]
    if missing:
        st.markdown("**Unmapped columns:** " + ", ".join(f"`{m}`" for m in missing))
        if llm().enabled:
            if st.button("🤖 AI suggest sources for unmapped columns (flagged 🟣 AI-suggested)"):
                intent = parse_request(f"{erp.name} {res.business_object} " + " ".join(missing), r, erp.id)
                intent.business_object = res.business_object
                ctx, _ = build_context(intent, r, store(), res.erp_version)
                try:
                    sugg = suggest_mappings(llm(), erp.name, res.business_object_label, missing, ctx)
                    for s in sugg:
                        if erp.find_table(s["table"]) and s.get("field"):
                            s["table"] = erp.find_table(s["table"])
                    keep = [x for x in res.requested_columns if x not in [s["column"] for s in sugg]]
                    new = build_mapping(r, erp.id, keep, res.business_object, res.erp_version, sql_settings(), extra_rows=sugg)
                    set_result(new)
                    st.rerun()
                except (LLMError, ValueError) as exc:
                    st.error(f"AI suggestion failed: {exc}")
        else:
            st.caption("Configure an LLM in Settings to get AI suggestions, or upload a data dictionary in the Metadata Repository.")
with t2:
    architecture_preview(res, key="cm_arch")
with t3:
    st.code(generate_sql(res, r.get(res.erp_id), "Deduplicated query", sql_settings()), language="sql")
    st.page_link("app_pages/sql_generator.py", label="Open SQL Generator for all query types", icon="❄️")
