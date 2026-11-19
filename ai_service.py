"""AI integration layer.

Provider-agnostic interface with an Anthropic Claude implementation. OpenAI /
Azure OpenAI can be added by implementing `AIProvider.complete`.

The AI is used only as a recommender: every table, column and join it returns is
verified against supplied metadata in mapping_engine.apply_ai_result(); anything
unverifiable is rejected and logged, and nothing it returns is ever Confirmed.
"""
from __future__ import annotations

import json
import re
from typing import Any

import pandas as pd

from config import AI_MAX_TOKENS, DEFAULT_ANTHROPIC_MODEL, get_secret
from join_analyzer import table_info
from mapping_engine import table_relevance
from utils.text_utils import clean_str

MAX_PROMPT_COLUMNS = 600

SYSTEM_PROMPT = """You are a senior ERP data integration architect and Snowflake developer.
You map ERP source metadata to target columns.

STRICT RULES:
1. Use ONLY tables and columns that appear in the SOURCE METADATA provided. Never invent tables, columns or relationships.
2. If no suitable source exists for a target column, omit it from column_mappings and add an open question.
3. Only propose joins whose columns exist in the metadata. Say in the reasoning whether the relationship is
   declared in the metadata (References column) or inferred by you. Inferred joins will be treated as
   recommendations requiring validation.
4. Confidence is 0.0-1.0 and must reflect uncertainty honestly.
5. Respond with a single JSON object only - no prose, no markdown fences.

JSON schema:
{
  "column_mappings": [{"target_column": str, "source_table": str, "source_columns": [str],
                       "transformation_logic": str, "confidence": float, "reasoning": str}],
  "joins": [{"left_table": str, "left_columns": [str], "right_table": str, "right_columns": [str],
             "cardinality": "1:1|1:N|N:1|M:N", "confidence": float, "reasoning": str}],
  "grain": {"table": str, "keys": [str], "reasoning": str},
  "assumptions": [str],
  "open_questions": [str]
}"""


class AIProvider:
    name = "base"

    def available(self) -> bool:
        return False

    def complete(self, system: str, user: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class AnthropicProvider(AIProvider):
    name = "Anthropic Claude"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or get_secret("ANTHROPIC_API_KEY")
        self.model = model or get_secret("ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL

    def available(self) -> bool:
        if not self.api_key:
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def complete(self, system: str, user: str) -> str:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        resp = client.messages.create(model=self.model, max_tokens=AI_MAX_TOKENS, system=system,
                                      messages=[{"role": "user", "content": user}])
        return "".join(getattr(b, "text", "") for b in resp.content)


class PlannedProvider(AIProvider):
    """Placeholder for OpenAI / Azure OpenAI - keeps the UI and interface stable."""

    def __init__(self, name: str):
        self.name = name

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError(f"{self.name} support is planned. Implement AIProvider.complete in ai_service.py.")


def get_provider(name: str = "Anthropic Claude", api_key: str | None = None, model: str | None = None) -> AIProvider:
    if name.startswith("Anthropic"):
        return AnthropicProvider(api_key, model)
    return PlannedProvider(name)


# --------------------------------------------------------------------------- prompt
def build_prompt(project, erp: str) -> str:
    meta = project.metadata_for(erp)
    info = table_info(meta)
    rel = table_relevance(info, project.business_context, project.target_columns)
    hinted = {h.split(".")[0].upper() for t in project.target_columns
              for h in clean_str(t.get("source_hint")).split(";") if "." in h}
    ranked = sorted(info, key=lambda t: (t not in hinted, -rel.get(t, 0)))
    keep, n = [], 0
    for t in ranked:
        if n + len(info[t]["columns"]) > MAX_PROMPT_COLUMNS and keep:
            break
        keep.append(t)
        n += len(info[t]["columns"])
    m = meta[meta["table_name"].isin(keep)][["table_name", "table_description", "column_name", "data_type",
                                             "column_description", "is_primary_key", "references",
                                             "business_description"]]
    tgt = pd.DataFrame(project.target_columns)
    tcols = [c for c in ["target_column", "target_data_type", "business_definition", "mandatory",
                         "expected_transformation", "business_rules"] if c in tgt.columns]
    confirmed = [f"{j['left_table']}.({j['left_columns']}) -> {j['right_table']}.({j['right_columns']})"
                 for j in project.joins if j.get("source_erp") == erp and j.get("status") == "Confirmed"]
    omitted = len(info) - len(keep)
    return (f"SOURCE ERP: {erp}\n"
            f"MODULE / PROCESS: {project.config.get('source_module')} / {project.config.get('business_process')}\n"
            f"EXPECTED GRAIN: {project.config.get('expected_grain') or 'not specified'}\n\n"
            f"BUSINESS CONTEXT:\n{project.business_context or '(none)'}\n\n"
            f"SOURCE METADATA (CSV; {len(keep)} tables{f', {omitted} less relevant tables omitted' if omitted else ''}):\n"
            f"{m.to_csv(index=False)}\n"
            f"JOINS ALREADY DECLARED IN METADATA (confirmed):\n" + ("\n".join(confirmed) or "(none)") + "\n\n"
            f"TARGET COLUMNS (CSV):\n{tgt[tcols].to_csv(index=False) if tcols else '(none)'}\n"
            "Return the JSON object described in the system prompt. Snowflake SQL syntax for transformation_logic, "
            "referencing columns as TABLE.COLUMN.")


def parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise ValueError("AI response was not valid JSON.")


def recommend(project, erp: str, provider: AIProvider) -> dict[str, Any]:
    if not provider.available():
        raise RuntimeError("AI provider is not configured. Set ANTHROPIC_API_KEY in .streamlit/secrets.toml or the environment.")
    raw = provider.complete(SYSTEM_PROMPT, build_prompt(project, erp))
    result = parse_json(raw)
    result["_raw_chars"] = len(raw)
    return result
