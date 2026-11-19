"""Central configuration: paths, LLM provider defaults, confidence vocabulary, SQL defaults."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
KB_DIR = BASE_DIR / "knowledge_base"
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
CUSTOM_ERP_DIR = DATA_DIR / "custom_erps"
UPLOAD_DIR = DATA_DIR / "uploads"
RAG_DIR = DATA_DIR / "rag_index"
for _d in (CUSTOM_ERP_DIR, UPLOAD_DIR, RAG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

APP_NAME = "ERP Logistics Data Architect"
APP_TAGLINE = "AI-powered multi-ERP mapping, data modelling & Snowflake SQL assistant"

# --------------------------------------------------------------------------- LLM
LLM_PROVIDERS = {
    "None (offline knowledge base only)": {"id": "none", "default_model": ""},
    "Anthropic Claude": {"id": "anthropic", "default_model": "claude-sonnet-5-5"},
    "OpenAI": {"id": "openai", "default_model": "gpt-4.1"},
    "OpenAI-compatible (Azure, Groq, Ollama, vLLM, ...)": {"id": "openai_compatible", "default_model": "llama3.1"},
}

# --------------------------------------------------------------------------- Confidence
# The four evidence categories requested in the spec + display helpers.
CONFIDENCE = {
    "standard": {
        "rank": 1, "label": "Standard / documented",
        "long": "Standard ERP data-dictionary object, consistent with vendor documentation (verify in your system)",
        "icon": "🟢",
    },
    "uploaded": {
        "rank": 2, "label": "Confirmed - uploaded metadata",
        "long": "Confirmed by metadata you uploaded (data dictionary / table inventory)",
        "icon": "🔵",
    },
    "inferred": {
        "rank": 3, "label": "Inferred",
        "long": "Inferred from known ERP conventions - validate before production use",
        "icon": "🟡",
    },
    "validate": {
        "rank": 4, "label": "Needs validation",
        "long": "Needs validation in the actual ERP environment (customer-specific or uncertain)",
        "icon": "🔴",
    },
    "ai": {
        "rank": 5, "label": "AI-suggested",
        "long": "Suggested by the LLM, not present in the knowledge base - treat as unverified",
        "icon": "🟣",
    },
    "unavailable": {
        "rank": 6, "label": "Not available",
        "long": "No source found in the knowledge base",
        "icon": "⚫",
    },
}


def confidence_badge(level: str) -> str:
    c = CONFIDENCE.get(level or "validate", CONFIDENCE["validate"])
    return f"{c['icon']} {c['label']}"


def weakest(levels) -> str:
    levels = [l for l in levels if l]
    if not levels:
        return "unavailable"
    return max(levels, key=lambda l: CONFIDENCE.get(l, CONFIDENCE["validate"])["rank"])


# --------------------------------------------------------------------------- SQL defaults
SQL_DEFAULTS = {
    "database": "RAW_DB",
    "schema": "{erp_id}",            # resolved per ERP
    "target_database": "ANALYTICS_DB",
    "target_schema": "SILVER",
    "sap_client": "100",
    "language": "E",
    "date_mode": "VARCHAR",          # how SAP DATS / numeric dates landed: VARCHAR or DATE
    "cdc_timestamp_column": "_FIVETRAN_SYNCED",
    "cdc_delete_column": "_FIVETRAN_DELETED",
    "apply_client_filter": True,
    "identifier_case": "UPPER",      # UPPER or AS_IS (D365 Synapse Link / BY lower-case)
}

QUERY_TYPES = [
    "Basic SELECT query",
    "Complete joined SELECT query",
    "Deduplicated query",
    "Incremental load query",
    "Historical data query",
    "CREATE OR REPLACE VIEW",
    "dbt model SQL",
]


def get_secret(name: str, default: str = "") -> str:
    """Read a secret from Streamlit secrets or environment variables."""
    try:
        import streamlit as st  # noqa: WPS433

        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # secrets file absent
        pass
    return os.environ.get(name, default)
