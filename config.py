"""Central configuration for ERP Mapping Studio.

API keys are never hardcoded. They are read (in order) from Streamlit secrets,
then environment variables. See README.md -> "Configuring AI".
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "ERP Mapping Studio"
APP_VERSION = "1.0.0"

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
SAMPLE_DATA_DIR = BASE_DIR / "sample_data"
OUTPUT_DIR = BASE_DIR / "generated_outputs"
PROJECTS_DIR = OUTPUT_DIR / "projects"
ERP_MASTER_FILE = TEMPLATES_DIR / "erp_master.json"

for _d in (OUTPUT_DIR, PROJECTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Mapping status vocabulary (spec section 10)
# ---------------------------------------------------------------------------
STATUS_CONFIRMED = "Confirmed"
STATUS_SUGGESTED = "Suggested"
STATUS_MISSING = "Missing"
STATUS_REQUIRES_VALIDATION = "Requires Validation"
MAPPING_STATUSES = [STATUS_CONFIRMED, STATUS_SUGGESTED, STATUS_REQUIRES_VALIDATION, STATUS_MISSING]

STATUS_ICONS = {
    STATUS_CONFIRMED: "🟢",
    STATUS_SUGGESTED: "🟡",
    STATUS_REQUIRES_VALIDATION: "🟠",
    STATUS_MISSING: "🔴",
}

# Where a mapping / join came from. This is what makes every join traceable.
EVIDENCE_METADATA_FK = "Metadata FK reference"        # declared in supplied metadata
EVIDENCE_USER = "User confirmed"                       # entered/approved by a user
EVIDENCE_USER_HINT = "User target hint"                # target spec named the source
EVIDENCE_NAME_MATCH = "Heuristic: shared key column"   # inferred, never auto-confirmed
EVIDENCE_SIMILARITY = "Heuristic: name/description similarity"
EVIDENCE_AI = "AI recommendation"
EVIDENCE_NONE = "No evidence"

CONFIRMED_EVIDENCE = {EVIDENCE_METADATA_FK, EVIDENCE_USER, EVIDENCE_USER_HINT}

JOIN_TYPES = ["INNER JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL OUTER JOIN"]
CARDINALITIES = ["1:1", "1:N", "N:1", "M:N", "Unknown"]

HISTORY_OPTIONS = ["Current records only", "Full history (SCD Type 2 style)", "Snapshot as-of date"]
MAPPING_TYPES = ["Single ERP", "Cross ERP"]
LOAD_TYPES = ["Full load", "Incremental (MERGE)"]

DEFAULT_MODULES = [
    "Sales", "Procurement", "Inventory", "Logistics", "Shipment", "Delivery",
    "Finance", "Manufacturing", "Master Data", "Quality", "Warehouse",
]

# Mapping-engine thresholds
SIMILARITY_SUGGEST_THRESHOLD = 0.45   # below this -> treated as Missing
MAX_CANDIDATES_PER_TARGET = 3

# ---------------------------------------------------------------------------
# AI provider configuration
# ---------------------------------------------------------------------------
AI_PROVIDERS = ["Anthropic Claude", "OpenAI (planned)", "Azure OpenAI (planned)"]
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
AI_MAX_TOKENS = 8000


def get_secret(name: str, default: str | None = None) -> str | None:
    """Read a secret from Streamlit secrets, then the environment. Never logs values."""
    try:
        import streamlit as st  # local import keeps engines usable without Streamlit

        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.environ.get(name, default)
