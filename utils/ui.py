"""Shared Streamlit state, styling and reusable components."""
from __future__ import annotations

import streamlit as st

from config.settings import CONFIDENCE, LLM_PROVIDERS, SQL_DEFAULTS, get_secret
from services.erp_knowledge_service import KnowledgeRepository, get_repository
from services.llm_service import LLMConfig
from services.rag_service import VectorStore, get_store

CSS = """
<style>
.block-container {padding-top: 1.6rem; max-width: 1500px;}
div[data-testid="stMetricValue"] {font-size: 1.5rem;}
.erp-hero {background: linear-gradient(120deg,#0d47a1 0%,#1565c0 55%,#00838f 100%); color:#fff; padding:1.3rem 1.6rem;
           border-radius:14px; margin-bottom:1rem;}
.erp-hero h1 {color:#fff; margin:0; font-size:1.8rem;} .erp-hero p {margin:.3rem 0 0 0; opacity:.92;}
.pill {display:inline-block; padding:2px 10px; border-radius:999px; font-size:.78rem; margin:2px 4px 2px 0;
       background:#eef2f7; border:1px solid #d5dce6;}
.small-note {font-size:.82rem; color:#5f6b7a;}
</style>
"""


def setup_page():
    st.markdown(CSS, unsafe_allow_html=True)
    init_state()


@st.cache_resource(show_spinner=False)
def _repo() -> KnowledgeRepository:
    return get_repository()


@st.cache_resource(show_spinner=False)
def _store() -> VectorStore:
    return get_store()


def repo() -> KnowledgeRepository:
    return _repo()


def store() -> VectorStore:
    return _store()


def init_state():
    ss = st.session_state
    r = repo()
    if "erp_id" not in ss or ss.erp_id not in r.erps:
        ss.erp_id = "sap_s4hana" if "sap_s4hana" in r.erps else next(iter(r.erps))
    ss.setdefault("erp_version", "")
    ss.setdefault("chat", [])
    ss.setdefault("current_result", None)
    ss.setdefault("ai_rows", [])
    ss.setdefault("architecture_approved", False)
    ss.setdefault("sql_settings", dict(SQL_DEFAULTS))
    if "llm" not in ss:
        provider, key, model = "none", "", ""
        if get_secret("ANTHROPIC_API_KEY"):
            provider, key, model = "anthropic", get_secret("ANTHROPIC_API_KEY"), get_secret("ANTHROPIC_MODEL", "claude-sonnet-5-5")
        elif get_secret("OPENAI_API_KEY"):
            provider, key, model = "openai", get_secret("OPENAI_API_KEY"), get_secret("OPENAI_MODEL", "gpt-4.1")
        ss.llm = LLMConfig(provider=provider, api_key=key, model=model, base_url=get_secret("OPENAI_BASE_URL"))


def llm() -> LLMConfig:
    return st.session_state.llm


def sql_settings() -> dict:
    return st.session_state.sql_settings


def sidebar_context():
    """Global ERP / version selector shown in the sidebar of every page."""
    r = repo()
    ss = st.session_state
    names = r.erp_names()
    ids = list(names.values())
    with st.sidebar:
        st.markdown("### 🏭 ERP context")
        idx = ids.index(ss.erp_id) if ss.erp_id in ids else 0
        name = st.selectbox("ERP system", list(names), index=idx, key="sb_erp")
        new_id = names[name]
        if new_id != ss.erp_id:
            ss.erp_id = new_id
            ss.erp_version = ""
        erp = r.get(ss.erp_id)
        versions = erp.versions
        v_idx = versions.index(ss.erp_version) if ss.erp_version in versions else 0
        ss.erp_version = st.selectbox("Version", versions, index=v_idx, key=f"sb_ver_{ss.erp_id}")
        st.caption(f"Storage: **{erp.storage}** · {len(erp.tables)} tables · {len(erp.relationships)} joins")
        status = "🟢 " + llm().provider if llm().enabled else "⚪ offline (knowledge base only)"
        st.caption(f"LLM: {status}")
        cr = ss.current_result
        if cr:
            st.caption(f"Active mapping: **{cr.erp_name} · {cr.business_object_label}** ({len(cr.rows)} cols)")
    return erp


def confidence_legend():
    st.markdown(" ".join(f"<span class='pill'>{c['icon']} {c['label']}</span>" for k, c in CONFIDENCE.items()),
                unsafe_allow_html=True)


def hero(title: str, subtitle: str):
    st.markdown(f"<div class='erp-hero'><h1>{title}</h1><p>{subtitle}</p></div>", unsafe_allow_html=True)


def show_warnings(warnings: list[str], risks: list[str] | None = None):
    if warnings:
        with st.expander(f"⚠️ {len(warnings)} warning(s) / validation notes", expanded=True):
            for w in warnings:
                st.markdown(f"- {w}")
    if risks:
        with st.expander(f"🔁 Duplicate risks ({len(risks)})"):
            for w in risks:
                st.markdown(f"- {w}")


def public(df):
    return df[[c for c in df.columns if not str(c).startswith("_")]]


def set_result(result):
    st.session_state.current_result = result
    st.session_state.architecture_approved = False


def go(page: str, label: str, icon: str = "➡️"):
    try:
        st.page_link(page, label=label, icon=icon)
    except Exception:
        pass
