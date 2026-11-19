"""Configurable LLM provider (Anthropic, OpenAI, Azure OpenAI, Ollama) over plain HTTPS.

Keys come only from environment variables (LLM_API_KEY). Every failure raises LLMError so callers can
fall back to deterministic, metadata-only behaviour.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import httpx

from erpdm.config import Settings, get_settings
from erpdm.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-4o", "azure_openai": None, "ollama": "llama3.1"}


class LLMError(Exception):
    pass


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    latency_ms: int


class LLMClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None):
        self.s = settings or get_settings()
        self.provider = (self.s.llm_provider or "none").lower()
        self.model = self.s.llm_model or DEFAULT_MODELS.get(self.provider)
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return self.s.llm_enabled and self.provider in DEFAULT_MODELS

    def describe(self) -> str:
        return f"{self.provider}:{self.model}" if self.enabled else "disabled (metadata-only mode)"

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.s.llm_timeout_seconds, transport=self._transport)

    def complete(self, system: str, messages: list[dict], max_tokens: int | None = None,
                 temperature: float | None = None, retries: int = 2) -> LLMResponse:
        if not self.enabled:
            raise LLMError("No LLM provider configured (set LLM_PROVIDER and LLM_API_KEY).")
        max_tokens = max_tokens or self.s.llm_max_tokens
        temperature = self.s.llm_temperature if temperature is None else temperature
        last: Exception | None = None
        for attempt in range(retries + 1):
            t0 = time.monotonic()
            try:
                text = getattr(self, f"_{self.provider}")(system, messages, max_tokens, temperature)
                return LLMResponse(text, self.provider, self.model or "", int((time.monotonic() - t0) * 1000))
            except LLMError as e:
                last = e
                if "status 4" in str(e) and "429" not in str(e):
                    break  # client errors other than rate limiting won't succeed on retry
            except (httpx.HTTPError, KeyError, ValueError) as e:
                last = e
            time.sleep(min(2 ** attempt, 8))
        log.error("LLM call failed provider=%s: %s", self.provider, last)
        raise LLMError(f"LLM provider error: {last}")

    def _key(self) -> str:
        if not self.s.llm_api_key:
            raise LLMError("LLM_API_KEY is not set.")
        return self.s.llm_api_key.get_secret_value()

    @staticmethod
    def _check(r: httpx.Response) -> dict:
        if r.status_code >= 400:
            raise LLMError(f"status {r.status_code}: {r.text[:300]}")
        return r.json()

    def _anthropic(self, system, messages, max_tokens, temperature) -> str:
        url = (self.s.llm_base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        with self._client() as c:
            data = self._check(c.post(url, headers={"x-api-key": self._key(), "anthropic-version": "2023-06-01",
                                                    "content-type": "application/json"},
                                      json={"model": self.model, "system": system, "messages": messages,
                                            "max_tokens": max_tokens, "temperature": temperature}))
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

    def _openai(self, system, messages, max_tokens, temperature) -> str:
        url = (self.s.llm_base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        with self._client() as c:
            data = self._check(c.post(url, headers={"Authorization": f"Bearer {self._key()}"},
                                      json={"model": self.model, "messages": [{"role": "system", "content": system}] + messages,
                                            "max_tokens": max_tokens, "temperature": temperature}))
        return data["choices"][0]["message"]["content"]

    def _azure_openai(self, system, messages, max_tokens, temperature) -> str:
        if not self.s.llm_base_url or not self.model:
            raise LLMError("Azure OpenAI needs LLM_BASE_URL (resource endpoint) and LLM_MODEL (deployment name).")
        ver = self.s.llm_api_version or "2024-06-01"
        url = f"{self.s.llm_base_url.rstrip('/')}/openai/deployments/{self.model}/chat/completions?api-version={ver}"
        with self._client() as c:
            data = self._check(c.post(url, headers={"api-key": self._key()},
                                      json={"messages": [{"role": "system", "content": system}] + messages,
                                            "max_tokens": max_tokens, "temperature": temperature}))
        return data["choices"][0]["message"]["content"]

    def _ollama(self, system, messages, max_tokens, temperature) -> str:
        url = (self.s.llm_base_url or "http://localhost:11434").rstrip("/") + "/api/chat"
        with self._client() as c:
            data = self._check(c.post(url, json={"model": self.model, "stream": False,
                                                 "messages": [{"role": "system", "content": system}] + messages,
                                                 "options": {"temperature": temperature, "num_predict": max_tokens}}))
        return data["message"]["content"]


def extract_json(text: str):
    """Pull the first JSON array/object out of an LLM answer."""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    raw = m.group(1) if m else text
    start = min([i for i in (raw.find("["), raw.find("{")) if i >= 0], default=-1)
    if start < 0:
        raise LLMError("No JSON in model output")
    for end in range(len(raw), start, -1):
        try:
            return json.loads(raw[start:end])
        except json.JSONDecodeError:
            continue
    raise LLMError("Unparseable JSON in model output")
