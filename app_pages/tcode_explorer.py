import streamlit as st

from config.settings import CONFIDENCE, QUERY_TYPES
from services.assistant_service import build_context, parse_request, tcode_analysis
from services.llm_service import LLMError, stream_chat
from services.sql_service import generate_sql
from services.model_service import ingestion_recommendations
from services.validation_service import dq_queries
from utils.components import architecture_preview, mapping_table
from utils.ui import llm, repo, set_result, setup_page, sidebar_context, sql_settings, store

setup_page()
erp = sidebar_context()
r = repo()
is_sap = erp.family == "SAP"
st.title("🔎 T-Code Explorer" if is_sap else "🔎 Screen / Report / API Explorer")
st.caption("Enter an SAP transaction code (VA03, VL03N, VT03N, VF03, ME23N, MB51, IQ09, …) or, for non-SAP systems, a "
           "screen, report, program, business object or API endpoint.")

known = sorted(erp.transactions)
if st.session_state.get("tcode_erp") != erp.id:
    st.session_state.tcode_erp = erp.id
    st.session_state.tcode_text = "IQ09" if "IQ09" in known else (known[0] if known else "")


def _pick():
    if st.session_state.get("tcode_pills"):
        st.session_state.tcode_text = st.session_state.tcode_pills


c1, c2 = st.columns([2, 3])
with c1:
    label = "Transaction code" if is_sap else "Screen / report / API identifier"
    code = st.text_input(label, key="tcode_text").strip()
with c2:
    if known:
        st.pills("Known in knowledge base", known, key="tcode_pills", on_change=_pick)
if not known:
    st.info(f"No screens/T-codes recorded for {erp.name} yet. Add them in the ERP Metadata Repository, or use the AI analysis below.")
if not code:
    st.stop()

res = tcode_analysis(r, erp.id, code, st.session_state.erp_version, sql_settings())
if not res["found"]:
    st.warning(f"**{res['code']}** — {res['type']}\n\n{res['message']}")
    if llm().enabled and st.button("🤖 Ask AI to analyse this transaction (results flagged as AI-suggested)"):
        intent = parse_request(f"{erp.name} transaction {code}: backend tables, joins, keys, grain", r, erp.id)
        ctx, _ = build_context(intent, r, store(), st.session_state.erp_version)
        try:
            st.write_stream(stream_chat(llm(), [{"role": "user", "content":
                f"ERP {erp.name} {st.session_state.erp_version}. Transaction/screen {code}. Provide: description, standard vs "
                "custom, program, backend tables with descriptions, join keys, PK/FK, grain, a Snowflake SELECT, history, "
                "dedup, incremental and validation checks. Label every table/field with an evidence tag."}], ctx))
        except LLMError as exc:
            st.error(str(exc))
    st.stop()

m = res.get("mapping")
st.subheader(f"{res['code']} — {res['description']}")
k = st.columns(4)
k[0].metric("1 · Description", res["description"][:28] + ("…" if len(res["description"]) > 28 else ""))
k[1].metric("2 · Standard / custom", res["type"])
k[2].metric("Business object", res.get("business_object") or "-")
k[3].metric("Tables", len(res["tables_df"]))
st.markdown(f"**3 · Program / report:** `{res['program']}` — "
            f"{CONFIDENCE.get(res['program_confidence'], {}).get('icon', '')} "
            f"{CONFIDENCE.get(res['program_confidence'], {}).get('label', '')}")
if res.get("notes"):
    st.info(res["notes"])
st.caption("A transaction rarely maps to one table: it reads a set of related tables. Each table and join carries its own confidence.")

tabs = st.tabs(["4-5 · Tables", "6-8 · Joins, keys & relationships", "9-10 · Grain & field mapping", "11 · Snowflake SQL",
                "12-14 · History, dedup & incremental", "15 · Validation & reconciliation"])
with tabs[0]:
    st.dataframe(res["tables_df"], width="stretch", hide_index=True)
    if res.get("tables_unknown"):
        st.caption("Tables listed for this transaction but not yet described in the KB: " + ", ".join(res["tables_unknown"]))
with tabs[1]:
    if res.get("relationships_df") is not None:
        st.dataframe(res["relationships_df"], width="stretch", hide_index=True)
    if m:
        architecture_preview(m, key="tc_arch", approve=False)
with tabs[2]:
    if m:
        st.info(f"**Grain:** {m.grain}  \n**Business key:** {', '.join(m.business_key)}  \n**Model type:** {m.model_type}")
        mapping_table(m, key="tc_map")
        if st.button("📌 Use this mapping in Data Model / SQL / Downloads", type="primary"):
            set_result(m)
            st.success("Loaded into workspace.")
with tabs[3]:
    if m:
        qt = st.selectbox("Query type", QUERY_TYPES, index=2, key="tc_qt")
        sql = generate_sql(m, erp, qt, sql_settings())
        st.code(sql, language="sql")
        st.download_button("⬇️ Download .sql", sql, file_name=f"{erp.id}_{res['code']}_{qt.split()[0].lower()}.sql")
with tabs[4]:
    st.markdown(f"**12 · Historical data availability:** {res.get('historical') or 'See ERP notes - transaction tables keep history until archived; master data needs snapshots.'}")
    if m:
        st.markdown("**13 · Deduplication logic**")
        for d in m.duplicate_risks or ["No specific risks recorded"]:
            st.markdown(f"- {d}")
        st.markdown("- Replication duplicates: `QUALIFY ROW_NUMBER() OVER (PARTITION BY <source PK> ORDER BY <CDC ts> DESC) = 1` per table — never DISTINCT.")
        st.markdown("**14 · Incremental ingestion recommendations**")
        for rec in ingestion_recommendations(m, erp):
            st.markdown(f"- {rec}")
with tabs[5]:
    if m:
        for name, q in dq_queries(m, erp, sql_settings()).items():
            with st.expander(name):
                st.code(q, language="sql")
