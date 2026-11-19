import streamlit as st

from config.settings import LLM_PROVIDERS, SQL_DEFAULTS
from services.llm_service import LLMConfig, LLMError, test_connection
from utils.ui import llm, repo, setup_page, sidebar_context

setup_page()
sidebar_context()
st.title("⚙️ Settings")

st.subheader("🤖 LLM reasoning engine")
st.caption("Keys entered here live only in this browser session. For deployment put them in Streamlit secrets "
           "(ANTHROPIC_API_KEY / OPENAI_API_KEY / OPENAI_BASE_URL) - see README.")
cur = llm()
labels = list(LLM_PROVIDERS)
cur_label = next((k for k, v in LLM_PROVIDERS.items() if v["id"] == cur.provider), labels[0])
with st.form("llm_form"):
    prov_label = st.selectbox("Provider", labels, index=labels.index(cur_label))
    pid = LLM_PROVIDERS[prov_label]["id"]
    c = st.columns(2)
    model = c[0].text_input("Model", cur.model if cur.provider == pid and cur.model else LLM_PROVIDERS[prov_label]["default_model"])
    key = c[1].text_input("API key", cur.api_key if cur.provider == pid else "", type="password")
    base_url = st.text_input("Base URL (OpenAI-compatible only, e.g. http://localhost:11434/v1)",
                             cur.base_url if pid == "openai_compatible" else "")
    c = st.columns(2)
    temp = c[0].slider("Temperature", 0.0, 1.0, float(cur.temperature), 0.05)
    mx = c[1].number_input("Max tokens", 500, 16000, int(cur.max_tokens), 500)
    if st.form_submit_button("Save", type="primary"):
        st.session_state.llm = LLMConfig(provider=pid, model=model, api_key=key, base_url=base_url,
                                         temperature=temp, max_tokens=int(mx))
        st.success("Saved.")
if llm().enabled and st.button("🔌 Test connection"):
    try:
        st.success(f"Response: {test_connection(llm())[:100]}")
    except LLMError as exc:
        st.error(str(exc))

st.subheader("❄️ Snowflake SQL defaults")
s = st.session_state.sql_settings
with st.form("sql_form"):
    c = st.columns(4)
    s["database"] = c[0].text_input("Source (raw) database", s["database"])
    s["schema"] = c[1].text_input("Source schema - {erp_id} is replaced", s["schema"])
    s["target_database"] = c[2].text_input("Target database", s["target_database"])
    s["target_schema"] = c[3].text_input("Target schema", s["target_schema"])
    c = st.columns(4)
    s["cdc_timestamp_column"] = c[0].text_input("CDC timestamp column", s["cdc_timestamp_column"],
                                                help="_FIVETRAN_SYNCED, _SDC_EXTRACTED_AT, SYSTEM_TS, ...")
    s["cdc_delete_column"] = c[1].text_input("Soft-delete flag column", s["cdc_delete_column"])
    s["date_mode"] = c[2].selectbox("SAP/numeric dates landed as", ["VARCHAR", "DATE"], index=["VARCHAR", "DATE"].index(s["date_mode"]))
    s["identifier_case"] = c[3].selectbox("Identifier case", ["UPPER", "AS_IS"], index=["UPPER", "AS_IS"].index(s["identifier_case"]))
    c = st.columns(3)
    s["apply_client_filter"] = c[0].checkbox("Filter SAP client (MANDT)", s["apply_client_filter"])
    s["sap_client"] = c[1].text_input("SAP client", s["sap_client"])
    s["language"] = c[2].text_input("Text language (SPRAS)", s["language"])
    if st.form_submit_button("Save SQL defaults"):
        st.session_state.sql_settings = s
        st.success("Saved.")
if st.button("Reset SQL defaults"):
    st.session_state.sql_settings = dict(SQL_DEFAULTS)
    st.rerun()

st.subheader("🗄️ Knowledge base")
if st.button("🔄 Reload all knowledge files"):
    repo().reload()
    st.success(f"Reloaded {len(repo().erps)} ERPs.")
if repo().load_errors:
    st.error("\n".join(repo().load_errors))
