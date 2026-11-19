import pandas as pd
import streamlit as st

from config.settings import APP_NAME, APP_TAGLINE
from utils.ui import confidence_legend, go, hero, repo, setup_page, sidebar_context, store

setup_page()
erp = sidebar_context()
r = repo()

hero(f"🚚 {APP_NAME}", APP_TAGLINE + " — Sales Orders · Deliveries · Shipments · Inventory · Transportation · Fulfilment")

total_tables = sum(len(e.tables) for e in r.erps.values())
total_rels = sum(len(e.relationships) for e in r.erps.values())
total_tx = sum(len(e.transactions) for e in r.erps.values())
c = st.columns(5)
c[0].metric("ERP / WMS systems", len(r.erps))
c[1].metric("Tables / objects", total_tables)
c[2].metric("Relationships", total_rels)
c[3].metric("T-codes / screens", total_tx)
c[4].metric("RAG document chunks", len(store().chunks))

st.markdown("#### Start here")
q1, q2, q3 = st.columns(3)
with q1:
    st.markdown("**🔎 Analyse a transaction**\n\n*ERP: SAP S/4HANA, T-Code: IQ09 — tables, joins, grain and Snowflake query.*")
    go("app_pages/tcode_explorer.py", "Open T-Code Explorer")
with q2:
    st.markdown("**🧩 Map business columns**\n\n*Shipment number, delivery number, sales order, material, shipped qty…*")
    go("app_pages/column_mapping.py", "Open Column Mapping")
with q3:
    st.markdown("**⚖️ Compare ERPs**\n\n*Sales order & shipment models across SAP, JDE and SYSPRO + canonical model.*")
    go("app_pages/cross_erp.py", "Open Cross-ERP Comparison")
go("app_pages/erp_assistant.py", "…or just ask the ERP Assistant in plain English", "💬")

st.markdown("#### Knowledge base coverage")
rows = []
for e in sorted(r.erps.values(), key=lambda x: x.name):
    s = e.summary()
    conf = pd.Series([t.get("confidence", "validate") for t in e.tables.values()]).value_counts().to_dict()
    s["🟢 standard tables"] = conf.get("standard", 0)
    s["🟡/🔴 inferred / validate"] = conf.get("inferred", 0) + conf.get("validate", 0)
    s["Objects modelled"] = ", ".join(e.business_objects)
    rows.append(s)
st.dataframe(pd.DataFrame(rows).drop(columns=["Id"]), width="stretch", hide_index=True)
confidence_legend()
st.caption("Knowledge is curated starter content with explicit confidence levels. Upload your own data dictionaries "
           "(e.g. SAP DD03L export, JDE F9860/F9210, SYSPRO data dictionary) in the **ERP Metadata Repository** to confirm "
           "fields; add ERPs without code changes.")
if r.load_errors:
    st.warning("Knowledge files with errors: " + "; ".join(r.load_errors))
