"""Lightweight RAG store: per-ERP document collections, chunking, TF-IDF retrieval.

Runs everywhere (Streamlit Community Cloud included) with only scikit-learn. The VectorStore interface
(add / search / delete) can be swapped for Chroma, pgvector, Snowflake Cortex Search, etc. without touching
callers.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from config.settings import RAG_DIR

GLOBAL = "_global"


@dataclass
class Chunk:
    id: str
    erp_id: str
    source: str
    kind: str          # pdf | text | metadata | api_spec | knowledge_base
    version: str
    text: str
    location: str = ""


def _chunk_text(text: str, size: int = 1200, overlap: int = 200) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text)
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 1 <= size:
            cur = f"{cur}\n{p}" if cur else p
        else:
            if cur:
                chunks.append(cur)
            while len(p) > size:
                chunks.append(p[:size])
                p = p[size - overlap:]
            cur = p
    if cur:
        chunks.append(cur)
    return chunks


def extract_text(name: str, data: bytes) -> list[tuple[str, str, str]]:
    """Return [(text, kind, location)] for an uploaded file."""
    low = name.lower()
    if low.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return [(p.extract_text() or "", "pdf", f"page {i + 1}") for i, p in enumerate(reader.pages)]
    if low.endswith((".csv", ".xlsx", ".xls")):
        df = pd.read_csv(io.BytesIO(data)) if low.endswith(".csv") else pd.read_excel(io.BytesIO(data))
        df = df.fillna("").astype(str)
        rows = [" | ".join(f"{c}: {v}" for c, v in r.items() if v) for r in df.to_dict(orient="records")]
        out = []
        for i in range(0, len(rows), 25):
            out.append(("\n".join(rows[i:i + 25]), "metadata", f"rows {i + 1}-{min(i + 25, len(rows))}"))
        return out
    if low.endswith((".json", ".yaml", ".yml")):
        return [(data.decode("utf-8", errors="ignore"), "api_spec", "")]
    if low.endswith(".docx"):
        from docx import Document
        doc = Document(io.BytesIO(data))
        return [("\n".join(p.text for p in doc.paragraphs), "text", "")]
    return [(data.decode("utf-8", errors="ignore"), "text", "")]


class VectorStore:
    def __init__(self, path: Path = RAG_DIR):
        self.path = path
        self.file = path / "chunks.json"
        self.chunks: list[Chunk] = []
        self._vec = None
        self._matrix = None
        self.load()

    # ---- persistence
    def load(self):
        if self.file.exists():
            self.chunks = [Chunk(**c) for c in json.loads(self.file.read_text(encoding="utf-8"))]
        self._vec = None

    def save(self):
        self.file.write_text(json.dumps([asdict(c) for c in self.chunks]), encoding="utf-8")
        self._vec = None

    # ---- mutation
    def add_document(self, erp_id: str, name: str, data: bytes, version: str = "") -> int:
        self.delete_source(erp_id, name)
        n = 0
        for text, kind, loc in extract_text(name, data):
            for i, piece in enumerate(_chunk_text(text)):
                cid = hashlib.md5(f"{erp_id}|{name}|{loc}|{i}".encode()).hexdigest()[:16]
                self.chunks.append(Chunk(cid, erp_id or GLOBAL, name, kind, version, piece,
                                         f"{loc} #{i + 1}".strip()))
                n += 1
        self.save()
        return n

    def delete_source(self, erp_id: str, name: str):
        self.chunks = [c for c in self.chunks if not (c.erp_id == (erp_id or GLOBAL) and c.source == name)]
        self.save()

    def sources(self) -> pd.DataFrame:
        if not self.chunks:
            return pd.DataFrame(columns=["ERP", "Source", "Type", "Version", "Chunks"])
        df = pd.DataFrame([asdict(c) for c in self.chunks])
        g = df.groupby(["erp_id", "source", "kind", "version"]).size().reset_index(name="Chunks")
        return g.rename(columns={"erp_id": "ERP", "source": "Source", "kind": "Type", "version": "Version"})

    # ---- retrieval
    def _build(self, extra: list[Chunk]):
        from sklearn.feature_extraction.text import TfidfVectorizer
        corpus = self.chunks + extra
        if not corpus:
            return None, None, []
        vec = TfidfVectorizer(token_pattern=r"(?u)\b[\w/]{2,}\b", ngram_range=(1, 2), sublinear_tf=True,
                              lowercase=True, max_features=60000)
        matrix = vec.fit_transform([c.text for c in corpus])
        return vec, matrix, corpus

    def search(self, query: str, erp_id: str | None = None, k: int = 6, extra: list[Chunk] | None = None) -> list[dict]:
        vec, matrix, corpus = self._build(extra or [])
        if vec is None:
            return []
        from sklearn.metrics.pairwise import cosine_similarity
        sims = cosine_similarity(vec.transform([query]), matrix)[0]
        ranked = sims.argsort()[::-1]
        out = []
        for idx in ranked:
            c = corpus[idx]
            if erp_id and c.erp_id not in (erp_id, GLOBAL):
                continue
            if sims[idx] <= 0.02:
                break
            out.append({**asdict(c), "score": float(sims[idx])})
            if len(out) >= k:
                break
        return out


def knowledge_base_chunks(erp) -> list[Chunk]:
    """Turn the structured ERP knowledge into retrievable text chunks (tables, relationships, T-codes)."""
    chunks = []
    for t, td in erp.all_tables().items():
        fields = "; ".join(f"{f} = {v.get('description', '')}" for f, v in (td.get("fields") or {}).items())
        text = (f"{erp.name} table {t}: {td.get('description', '')}. Module {td.get('module', '')}. "
                f"Primary key {', '.join(td.get('primary_key', []) or [])}. {td.get('role', '')} {td.get('notes', '')} "
                f"Fields: {fields}")
        chunks.append(Chunk(f"kb-{erp.id}-{t}", erp.id, "knowledge base", "knowledge_base", "", text, f"table {t}"))
    for code, tx in erp.transactions.items():
        text = (f"{erp.name} transaction/screen {code}: {tx.get('description', '')}. Business object "
                f"{tx.get('business_object', '')}. Tables {', '.join(tx.get('tables', []))}. {tx.get('notes', '')}")
        chunks.append(Chunk(f"kb-{erp.id}-tx-{code}", erp.id, "knowledge base", "knowledge_base", "", text, f"tcode {code}"))
    rel_text = "\n".join(f"{r.get('from')} -> {r.get('to')} ({r.get('cardinality')}) on " +
                         ", ".join(f"{a}={b}" for a, b in r.get("on", [])) + f" {r.get('label', '')}"
                         for r in erp.relationships)
    if rel_text:
        chunks.append(Chunk(f"kb-{erp.id}-rels", erp.id, "knowledge base", "knowledge_base", "",
                            f"{erp.name} table relationships and joins:\n{rel_text}", "relationships"))
    return chunks


_STORE: VectorStore | None = None


def get_store() -> VectorStore:
    global _STORE
    if _STORE is None:
        _STORE = VectorStore()
    return _STORE
