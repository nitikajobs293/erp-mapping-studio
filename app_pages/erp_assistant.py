import streamlit as st

from services import model_service as ms
from services.assistant_service import build_context, offline_answer, parse_request
from services.llm_service import LLMError, stream_chat
from utils.ui import llm, public, repo, set_result, setup_page, sidebar_context, sql_settings, store

setup_page()
erp = sidebar_context()
r = repo()
st.title("💬 Ask ERP Assistant")
st.caption("Ask anything about ERP tables, T-codes, joins, grain or Snowflake SQL. The knowledge base answers first "
           "(with confidence levels); a configured LLM adds reasoning on top, grounded in the KB and your uploaded documents.")

EXAMPLES = [
    "ERP: SAP S/4HANA, T-Code: IQ09. Give me all backend tables, joins, grain and Snowflake query.",
    "Which tables contain delivery information in SAP S/4HANA?",
    "How can I extract sales order header and item information from JDE?",
    "How can I identify shipped and open sales orders in SYSPRO?",
    "What are the backend tables for shipment tracking in Blue Yonder?",
    "How can I identify serial numbers associated with deliveries in SAP?",
    "What are the differences between SAP ECC and S/4HANA shipment data models?",
    "ERP: iScala. I need shipment number, delivery number, sales order, material, shipped quantity and actual shipment date.",
    "Compare sales order and shipment data models between SAP, JDE and SYSPRO.",
]
with st.expander("💡 Example questions", expanded=not st.session_state.chat):
    for i, ex in enumerate(EXAMPLES):
        if st.button(ex, key=f"ex_{i}", width="stretch"):
            st.session_state.pending_q = ex
            st.rerun()

c1, c2 = st.columns([1, 5])
if c1.button("🗑️ Clear chat"):
    st.session_state.chat = []
    st.rerun()
c2.caption(f"Default ERP when none is mentioned: **{erp.name}** (sidebar). "
           + ("LLM: **" + llm().provider + " / " + llm().model + "**" if llm().enabled else
              "LLM not configured → knowledge-base answers only (configure in ⚙️ Settings)."))


def render_payload(payload: dict, idx: int):
    if not payload:
        return
    if "tcode" in payload and payload["tcode"].get("found"):
        t = payload["tcode"]
        st.markdown("**Backend tables**")
        st.dataframe(t["tables_df"], width="stretch", hide_index=True)
        if t.get("relationships_df") is not None and not t["relationships_df"].empty:
            st.markdown("**Joins**")
            st.dataframe(t["relationships_df"], width="stretch", hide_index=True)
        if t.get("mapping"):
            payload.setdefault("mapping", t["mapping"])
    if "mapping" in payload:
        m = payload["mapping"]
        with st.expander(f"🧩 Mapping & architecture — {m.erp_name} {m.business_object_label}", expanded=True):
            st.dataframe(public(m.mapping_df())[["Business Column", "Source Table", "Source Field", "Source Field Description",
                                                 "Join Condition", "Transformation", "Confidence", "Validation Notes"]],
                         width="stretch", hide_index=True)
            st.graphviz_chart(ms.mapping_dot(m, r.get(m.erp_id)), width="stretch")
            if st.button("📌 Load into workspace (Data Model / SQL / Downloads)", key=f"load_{idx}"):
                set_result(m)
                st.success("Loaded. Open **Data Model Designer**, **SQL Generator** or **Documentation & Downloads**.")
    for bo, eids, matrix, _details in payload.get("comparisons", []):
        st.markdown(f"**{r.business_object_catalog().get(bo, {}).get('label', bo)}**")
        st.dataframe(matrix, width="stretch", hide_index=True)
    if payload.get("sources"):
        with st.expander("📚 Retrieved document sources"):
            for s in payload["sources"]:
                st.markdown(f"- **{s['source']}** ({s['location']}, {s['erp_id']}, score {s['score']:.2f})")


for i, msg in enumerate(st.session_state.chat):
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("kb") and msg.get("kb") != msg["content"]:
            with st.expander("📖 Knowledge-base facts used"):
                st.markdown(msg["kb"])
        render_payload(msg.get("payload", {}), i)

question = st.chat_input("e.g. Which tables contain shipment information in SAP S/4HANA?")
question = question or st.session_state.pop("pending_q", None)
if question:
    st.session_state.chat.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        intent = parse_request(question, r, st.session_state.erp_id)
        version = st.session_state.erp_version if intent.erp_id == st.session_state.erp_id else ""
        kb_md, payload = offline_answer(intent, r, version, sql_settings())
        answer = kb_md
        if llm().enabled:
            context, sources = build_context(intent, r, store(), version)
            payload["sources"] = sources
            history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.chat[-8:]]
            try:
                answer = st.write_stream(stream_chat(llm(), history, context))
            except LLMError as exc:
                st.error(f"LLM call failed ({exc}). Showing knowledge-base answer.")
                answer = kb_md
                st.markdown(kb_md)
            else:
                with st.expander("📖 Knowledge-base facts used"):
                    st.markdown(kb_md)
        else:
            st.markdown(kb_md)
        idx = len(st.session_state.chat)
        render_payload(payload, idx)
    st.session_state.chat.append({"role": "assistant", "content": answer, "kb": kb_md, "payload": payload})
