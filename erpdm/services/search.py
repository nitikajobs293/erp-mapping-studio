"""Metadata search: PostgreSQL full-text search (GIN index) with a portable BM25 fallback."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import func, literal_column, or_, select, text
from sqlalchemy.orm import Session

from erpdm.db.base import is_postgres
from erpdm.db.models import DocumentChunk, SearchIndex

_TOKEN = re.compile(r"[A-Za-z0-9_/]+")


@dataclass
class SearchHit:
    entity_type: str
    entity_id: int
    erp_id: int | None
    title: str
    snippet: str
    score: float
    validation_status: str | None = None


def tokenize(s: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(s or "") if len(t) > 1]


def _snippet(body: str, terms: list[str], width: int = 180) -> str:
    low = body.lower()
    pos = min((low.find(t) for t in terms if t in low), default=0)
    start = max(0, pos - 40)
    return ("…" if start else "") + body[start:start + width].strip() + ("…" if len(body) > start + width else "")


def search(session: Session, query: str, erp_id: int | None = None, entity_types: list[str] | None = None,
           module: str | None = None, limit: int = 50, offset: int = 0) -> list[SearchHit]:
    query = (query or "").strip()
    if not query:
        return []
    terms = tokenize(query)
    if not terms:
        return []
    # Exact identifier matches (table / T-code names) always rank first.
    exact = _exact_hits(session, query, erp_id, entity_types)
    if is_postgres(session):
        hits = _pg_search(session, query, erp_id, entity_types, module, limit + offset + len(exact))
    else:
        hits = _bm25_search(session, terms, erp_id, entity_types, module)
    seen = {(h.entity_type, h.entity_id) for h in exact}
    merged = exact + [h for h in hits if (h.entity_type, h.entity_id) not in seen]
    return merged[offset:offset + limit]


def _exact_hits(session, query, erp_id, entity_types) -> list[SearchHit]:
    q = select(SearchIndex).where(or_(*[
        func.upper(SearchIndex.title).like(f"{tok.upper()} —%") for tok in query.split()[:5]]))
    if erp_id:
        q = q.where(SearchIndex.erp_id == erp_id)
    if entity_types:
        q = q.where(SearchIndex.entity_type.in_(entity_types))
    return [SearchHit(r.entity_type, r.entity_id, r.erp_id, r.title, r.body[:180], 100.0, r.validation_status)
            for r in session.scalars(q.limit(20))]


def _pg_search(session, query, erp_id, entity_types, module, limit) -> list[SearchHit]:
    doc = func.to_tsvector(literal_column("'english'"), SearchIndex.title + literal_column("' '") + SearchIndex.body)
    tsq = func.websearch_to_tsquery(literal_column("'english'"), query)
    # Prefix-matching OR query as a second chance for partial identifiers (e.g. "EQU").
    prefix = " | ".join(f"{re.sub(r'[^A-Za-z0-9_]', '', t)}:*" for t in tokenize(query) if re.sub(r'[^A-Za-z0-9_]', '', t))
    tsq_prefix = func.to_tsquery(literal_column("'simple'"), prefix or "x")
    doc_simple = func.to_tsvector(literal_column("'simple'"), SearchIndex.title + literal_column("' '") + SearchIndex.body)
    rank = func.ts_rank(doc, tsq) * 2 + func.ts_rank(doc_simple, tsq_prefix)
    q = select(SearchIndex, rank.label("rank")).where(or_(doc.op("@@")(tsq), doc_simple.op("@@")(tsq_prefix)))
    if erp_id:
        q = q.where(SearchIndex.erp_id == erp_id)
    if entity_types:
        q = q.where(SearchIndex.entity_type.in_(entity_types))
    if module:
        q = q.where(SearchIndex.module == module)
    rows = session.execute(q.order_by(text("rank DESC")).limit(limit)).all()
    terms = tokenize(query)
    return [SearchHit(r.SearchIndex.entity_type, r.SearchIndex.entity_id, r.SearchIndex.erp_id,
                      r.SearchIndex.title, _snippet(r.SearchIndex.body, terms), float(r.rank),
                      r.SearchIndex.validation_status) for r in rows]


def _bm25_search(session, terms, erp_id, entity_types, module, k1=1.4, b=0.75) -> list[SearchHit]:
    q = select(SearchIndex)
    if erp_id:
        q = q.where(SearchIndex.erp_id == erp_id)
    if entity_types:
        q = q.where(SearchIndex.entity_type.in_(entity_types))
    if module:
        q = q.where(SearchIndex.module == module)
    rows = list(session.scalars(q))
    return [SearchHit(r.entity_type, r.entity_id, r.erp_id, r.title, _snippet(r.body, terms), s, r.validation_status)
            for r, s in bm25_rank([(r, f"{r.title} {r.title} {r.body}") for r in rows], terms, k1, b)]


def bm25_rank(docs: list[tuple[object, str]], terms: list[str], k1: float = 1.4, b: float = 0.75):
    """Rank (obj, text) pairs; supports prefix matches at half weight. Returns [(obj, score)] desc."""
    if not docs:
        return []
    toks = [tokenize(t) for _, t in docs]
    avgdl = sum(len(t) for t in toks) / len(toks) or 1
    n = len(docs)
    df = Counter()
    for tk in toks:
        s = set(tk)
        for term in terms:
            if term in s or any(x.startswith(term) for x in s):
                df[term] += 1
    out = []
    for (obj, _), tk in zip(docs, toks):
        tf = Counter(tk)
        score = 0.0
        for term in terms:
            f = tf.get(term, 0) + 0.5 * sum(v for k, v in tf.items() if k != term and k.startswith(term) and len(term) >= 3)
            if not f:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(tk) / avgdl))
        if score > 0:
            out.append((obj, score))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def search_documents(session: Session, query: str, erp_id: int | None = None, limit: int = 5) -> list[tuple[DocumentChunk, float]]:
    terms = tokenize(query)
    if not terms:
        return []
    scope = [or_(DocumentChunk.erp_id == erp_id, DocumentChunk.erp_id.is_(None))] if erp_id else []
    if is_postgres(session):
        clean = [re.sub(r"\W", "", t) for t in terms]
        clean = [t for t in clean if t]
        if not clean:
            return []
        doc = func.to_tsvector(literal_column("'english'"), DocumentChunk.content)
        # OR query for recall on natural-language questions; ts_rank orders by coverage
        or_q = func.to_tsquery(literal_column("'english'"), " | ".join(clean))
        rank = func.ts_rank(doc, or_q)
        rows = session.execute(select(DocumentChunk, rank.label("r")).where(doc.op("@@")(or_q), *scope)
                               .order_by(text("r DESC")).limit(limit)).all()
        return [(r.DocumentChunk, float(r.r)) for r in rows]
    chunks = list(session.scalars(select(DocumentChunk).where(*scope)))
    return bm25_rank([(c, c.content) for c in chunks], terms)[:limit]


def ensure_pg_indexes(session: Session) -> None:
    """GIN expression indexes for FTS (idempotent). Also created by the Alembic migration."""
    if not is_postgres(session):
        return
    session.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_search_index_fts ON search_index USING GIN "
        "(to_tsvector('english', title || ' ' || body))"))
    session.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_search_index_fts_simple ON search_index USING GIN "
        "(to_tsvector('simple', title || ' ' || body))"))
    session.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_document_chunks_fts ON document_chunks USING GIN "
        "(to_tsvector('english', content))"))
