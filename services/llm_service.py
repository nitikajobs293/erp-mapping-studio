"""Provider-agnostic LLM service (Anthropic Claude, OpenAI, any OpenAI-compatible endpoint).

The LLM is the reasoning layer on top of the knowledge base + RAG context. It is instructed to never
present unverified tables/fields as facts and to label every claim with an evidence level.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterator

SYSTEM_PROMPT = """You are a Senior ERP Solution Architect and Logistics Data Architect (SAP ECC/S/4HANA, Oracle EBS/Fusion,
JD Edwards, SYSPRO, iScala, Infor M3/LN/Nexus, Microsoft D365, Blue Yonder, Manhattan, Körber) and an expert Snowflake developer.
Your focus: logistics - sales orders, deliveries, shipments, transportation, inventory, procurement, production, returns.

STRICT ACCURACY RULES
1. Never invent table names, column names, joins or primary keys. Prefer the KNOWLEDGE BASE CONTEXT and RETRIEVED DOCUMENTS given to you.
2. Tag every table / field / join you mention with one evidence label:
   [KB-Standard] in the provided knowledge base as a standard object;
   [Uploaded] confirmed by uploaded metadata/documents (cite the source name);
   [Inferred] based on known ERP conventions but NOT in the provided context;
   [Validate] uncertain or customer-specific - must be checked in the real system.
3. Never claim that document numbers of different processes join directly unless a reference field exists
   (e.g. SAP LIPS-VGBEL/VGPOS -> VBAP, VBFA document flow, VTTP shipment-delivery bridge).
4. Always state dataset grain, keys, cardinality and duplicate risks. Never recommend DISTINCT to fix duplicates.
5. Snowflake SQL must use explicit aliases, explicit join conditions, CTEs, and ROW_NUMBER()/QUALIFY only for justified deduplication.
6. If you do not know, say so and say how to find out (e.g. SE11/SE93 in SAP, data dictionary, ODP/CDS views).
Answer in well-structured Markdown with tables where useful. Be concise but complete."""


@dataclass
class LLMConfig:
    provider: str = "none"            # none | anthropic | openai | openai_compatible
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.1
    max_tokens: int = 4000

    @property
    def enabled(self) -> bool:
        return self.provider != "none" and bool(self.api_key or self.provider == "openai_compatible")


class LLMError(RuntimeError):
    pass


def _messages_with_context(messages: list[dict], context: str) -> list[dict]:
    msgs = [m for m in messages if m["role"] in ("user", "assistant")]
    if context and msgs and msgs[-1]["role"] == "user":
        msgs = msgs[:-1] + [{"role": "user", "content": f"{context}\n\n---\nQUESTION:\n{msgs[-1]['content']}"}]
    return msgs


def stream_chat(cfg: LLMConfig, messages: list[dict], context: str = "") -> Iterator[str]:
    if not cfg.enabled:
        raise LLMError("No LLM provider configured")
    msgs = _messages_with_context(messages, context)
    try:
        if cfg.provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=cfg.api_key)
            with client.messages.stream(model=cfg.model, max_tokens=cfg.max_tokens, temperature=cfg.temperature,
                                        system=SYSTEM_PROMPT, messages=msgs) as stream:
                for text in stream.text_stream:
                    yield text
        else:
            from openai import OpenAI
            kwargs = {"api_key": cfg.api_key or "not-needed"}
            if cfg.base_url:
                kwargs["base_url"] = cfg.base_url
            client = OpenAI(**kwargs)
            stream = client.chat.completions.create(
                model=cfg.model, temperature=cfg.temperature, max_tokens=cfg.max_tokens, stream=True,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}] + msgs)
            for ev in stream:
                if ev.choices and ev.choices[0].delta and ev.choices[0].delta.content:
                    yield ev.choices[0].delta.content
    except ImportError as exc:
        raise LLMError(f"Provider SDK not installed: {exc}") from exc
    except Exception as exc:  # surface provider errors to the UI
        raise LLMError(f"{type(exc).__name__}: {exc}") from exc


def complete(cfg: LLMConfig, prompt: str, context: str = "") -> str:
    return "".join(stream_chat(cfg, [{"role": "user", "content": prompt}], context))


def suggest_mappings(cfg: LLMConfig, erp_name: str, business_object: str, columns: list[str], context: str) -> list[dict]:
    """Ask the LLM for candidate sources for unmapped columns. Results are always tagged 'ai'."""
    prompt = (
        f"ERP: {erp_name}\nBusiness object: {business_object}\nUnmapped business columns: {json.dumps(columns)}\n\n"
        "For each column propose the most likely source. Prefer tables in the knowledge base context. "
        "Return ONLY a JSON array, no prose, with objects: "
        '{"column": str, "table": str, "field": str, "description": str, "rationale": str, '
        '"evidence": "KB-Standard|Uploaded|Inferred|Validate"}. Use field "" if you are not confident.'
    )
    raw = complete(cfg, prompt, context)
    m = re.search(r"\[.*\]", raw, flags=re.S)
    if not m:
        raise LLMError("LLM did not return JSON")
    items = json.loads(m.group(0))
    out = []
    for it in items:
        if not it.get("table"):
            continue
        out.append({"column": it.get("column", ""), "table": str(it["table"]).upper() if erp_name.startswith("SAP") else it["table"],
                    "field": it.get("field") or None, "confidence": "ai",
                    "note": f"AI ({it.get('evidence', 'Inferred')}): {it.get('rationale', '')}",
                    "description": it.get("description", "")})
    return out


def test_connection(cfg: LLMConfig) -> str:
    return complete(cfg, "Reply with exactly: OK")
