"""Metadata-grounded ERP assistant (RAG) with hallucination guard and deterministic fallback."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from erpdm.constants import SourceType, ValidationStatus
from erpdm.db.models import BusinessProcess, BusinessTerm, ERPColumn, ERPSystem, ERPTable, Transaction
from erpdm.logging_setup import get_logger
from erpdm.schemas import JoinPair, RelationshipIn
from erpdm.services.llm import LLMClient, LLMError, extract_json
from erpdm.services.metadata import check_relationship, get_table, get_transaction, relationships_among, upsert_relationship
from erpdm.services.relationships import PlanningError, plan_joins
from erpdm.services.search import search, search_documents

log = get_logger(__name__)

SYSTEM_PROMPT = """You are the ERP Data Modelling Assistant for a data engineering team.
Rules (strict):
1. Answer ONLY from the CONTEXT block. It is the team's ERP metadata repository and indexed documents.
2. Never invent table names, column names, join conditions, T-codes or business rules. If something is not in
   CONTEXT, say "Not in the metadata repository" and ask the user to upload the table structure or documentation.
3. Always state the provenance shown in CONTEXT (source type, validation status, confidence) for tables and joins
   you mention. Never present a join with status other than 'verified' as a confirmed fact.
4. Cite items as [TABLE:NAME], [JOIN:id], [TCODE:CODE] or [DOC:n].
5. Use Snowflake SQL unless asked otherwise. Use only joins listed in CONTEXT.
6. Be concise and practical; use markdown tables/code blocks where helpful."""

ALLOWED_UPPER = {
    "SQL", "AND", "OR", "NOT", "NULL", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "SELECT", "FROM", "WHERE", "ON",
    "AS", "GROUP", "BY", "ORDER", "HAVING", "QUALIFY", "UNION", "ALL", "CASE", "WHEN", "THEN", "ELSE", "END", "WITH",
    "COUNT", "SUM", "MAX", "MIN", "DISTINCT", "COALESCE", "TRIM", "NULLIF", "CAST", "LIMIT", "IS", "IN", "EXISTS",
    "SAP", "ERP", "ECC", "CDC", "SCD", "SCD1", "SCD2", "PK", "FK", "ID", "API", "ETL", "ELT", "BI", "DBT", "RAW",
    "DATE", "TIME", "TRUE", "FALSE", "YAML", "JSON", "CSV", "DDL", "DDIC", "SLT", "ODP", "UTC", "KPI", "ROW_NUMBER",
    "OVER", "PARTITION", "DESC", "ASC", "VARCHAR", "NUMBER", "CHAR", "NUMC", "DATS", "TIMS", "CLNT", "LANG", "QUAN",
    "CURR", "CUKY", "UNIT", "DEC", "INT", "INTEGER", "TIMESTAMP", "TABLE", "JOIN", "TCODE", "DOC", "NOTE", "N/A",
    "S4HANA", "HANA", "MERGE", "INSERT", "UPDATE", "DELETE", "CREATE", "VIEW", "CDS", "OK", "TODO", "FYI", "ASAP",
    "IMPORTANT", "WARNING", "YES", "NO", "MDOC", "EBS", "R12", "JDE", "WMS", "TMS", "SCM", "OEM", "UOM", "ISO",
}


QUESTION_STOP = {"which", "what", "where", "how", "the", "are", "is", "tables", "table", "hold", "holds", "contain",
                 "contains", "information", "info", "data", "explain", "between", "relationship", "sap", "for", "and",
                 "can", "create", "build", "model", "used", "use", "does", "with", "from", "into", "about", "show",
                 "give", "list", "all", "get", "need", "want", "store", "stored", "stores", "find"}


def _stem(w: str) -> str:
    return w[:-1] if w.endswith("s") and len(w) > 4 else w


def _coverage(terms: list[str], text: str) -> float:
    if not terms:
        return 0.0
    low = text.lower()
    return sum(1 for t in terms if _stem(t) in low) / len(terms)


@dataclass
class AssistantAnswer:
    text: str
    mode: str                      # llm | metadata | not_found
    citations: list[str] = field(default_factory=list)
    unverified_identifiers: list[str] = field(default_factory=list)
    context_tables: list[str] = field(default_factory=list)
    sql: str | None = None


# ----------------------------------------------------------------------------- grounding
def detect_entities(session: Session, erp: ERPSystem, question: str) -> dict:
    tokens = set(re.findall(r"\b[A-Za-z][A-Za-z0-9_/]{1,39}\b", question))
    up = {t.upper() for t in tokens}
    tables = list(session.scalars(select(ERPTable).where(ERPTable.erp_id == erp.id,
                                                          func.upper(ERPTable.name).in_(up))))
    order = {t.upper(): question.upper().find(t.upper()) for t in tokens}
    tables.sort(key=lambda t: order.get(t.name.upper(), 0))
    txs = list(session.scalars(select(Transaction).where(Transaction.erp_id == erp.id,
                                                         func.upper(Transaction.code).in_(up))))
    return {"tables": tables, "transactions": txs}


def _table_block(t: ERPTable, max_cols: int = 40) -> str:
    cols = "; ".join(f"{c.name}{' (PK)' if c.is_pk else ''} {c.data_type or ''}: {c.description or ''}"
                     for c in t.columns[:max_cols])
    return (f"[TABLE:{t.name}] {t.description} | class={t.table_class} | PK={t.primary_key} | "
            f"provenance={t.source_type}/{t.validation_status}/conf {t.confidence:.2f}"
            + (f" | notes: {t.notes}" if t.notes else "") + f"\n  columns: {cols}")


def _rel_block(r) -> str:
    cond = " AND ".join(f"{r.from_table.name}.{j['from']} = {r.to_table.name}.{j['to']}" for j in r.join_columns)
    extra = f" AND {r.extra_condition.replace('{from}', r.from_table.name).replace('{to}', r.to_table.name)}" if r.extra_condition else ""
    return (f"[JOIN:{r.id}] {r.from_table.name} → {r.to_table.name} ON {cond}{extra} | {r.join_type} | {r.cardinality} | "
            f"{r.relationship_type} | {r.description or ''} | provenance={r.source_type}/{r.validation_status}/conf {r.confidence:.2f}")


def build_context(session: Session, erp: ERPSystem, question: str, max_tables: int = 8) -> tuple[str, list[str], dict]:
    ent = detect_entities(session, erp, question)
    tables: dict[str, ERPTable] = {t.name: t for t in ent["tables"]}
    blocks = [f"ERP: {erp.name} ({erp.code}). Version: {erp.version_label}. Notes: {erp.landscape_notes or '-'}"]
    for tx in ent["transactions"]:
        blocks.append(f"[TCODE:{tx.code}] {tx.name} — {tx.description or ''} Purpose: {tx.business_purpose or ''} "
                      f"Logic: {tx.business_logic or ''} Components: {tx.app_components or {}} | "
                      f"provenance={tx.source_type}/{tx.validation_status}/conf {tx.confidence:.2f}\n  tables: "
                      + ", ".join(f"{l.table.name} ({l.role})" for l in tx.table_links))
        for l in tx.table_links:
            tables.setdefault(l.table.name, l.table)
    hits = search(session, question, erp_id=erp.id, limit=12)
    for h in hits:
        if h.entity_type == "table" and len(tables) < max_tables:
            t = session.get(ERPTable, h.entity_id)
            tables.setdefault(t.name, t)
        elif h.entity_type == "term":
            term = session.get(BusinessTerm, h.entity_id)
            blocks.append(f"[TERM] {term.term} → {term.table_name}.{term.column_name} ({term.validation_status}) {term.definition or ''}")
        elif h.entity_type == "process":
            p = session.get(BusinessProcess, h.entity_id)
            blocks.append(f"[PROCESS] {p.name}: {p.description or ''} Steps: {' → '.join(p.flow_steps or [])}")
    for t in list(tables.values())[:max_tables]:
        blocks.append(_table_block(t))
    rels = relationships_among(session, erp.id, [t.id for t in tables.values()])
    rels = [r for r in rels if r.from_table.name in tables and r.to_table.name in tables]
    blocks += [_rel_block(r) for r in rels]
    for i, (chunk, _) in enumerate(search_documents(session, question, erp.id, limit=4), start=1):
        blocks.append(f"[DOC:{i}] ({chunk.document.filename}) {chunk.content[:1200]}")
    return "\n".join(blocks), list(tables), ent


def unknown_identifiers(session: Session, erp: ERPSystem, text: str) -> list[str]:
    stripped = re.sub(r"\[(?:TABLE|JOIN|TCODE|DOC):[^\]]+\]", "", text)
    candidates = set(re.findall(r"\b[A-Z][A-Z0-9_]{2,29}\b", stripped)) - ALLOWED_UPPER
    if not candidates:
        return []
    known_tables = {n.upper() for n in session.scalars(select(ERPTable.name).where(ERPTable.erp_id == erp.id))}
    known_cols = {n.upper() for n in session.scalars(
        select(ERPColumn.name).join(ERPTable).where(ERPTable.erp_id == erp.id))}
    known_codes = {c.upper() for c in session.scalars(select(Transaction.code).where(Transaction.erp_id == erp.id))}
    known = known_tables | known_cols | known_codes | {erp.code.upper()}
    # lower-case aliases & snake_case output columns are fine; only flag ERP-looking identifiers
    return sorted(c for c in candidates if c not in known and not c.startswith("I0") and not c.isdigit())


# ----------------------------------------------------------------------------- answering
def answer(session: Session, erp: ERPSystem, question: str, history: list[dict] | None = None,
           llm: LLMClient | None = None) -> AssistantAnswer:
    question = (question or "").strip()[:4000]
    if not question:
        return AssistantAnswer("Ask a question about tables, T-codes, joins or models.", "not_found")
    context, ctx_tables, ent = build_context(session, erp, question)
    llm = llm or LLMClient()
    if llm.enabled and (ctx_tables or "[DOC:" in context or "[TERM]" in context):
        msgs = [m for m in (history or [])[-6:] if m.get("role") in ("user", "assistant")]
        msgs.append({"role": "user", "content": f"CONTEXT:\n{context}\n\nQUESTION: {question}"})
        try:
            resp = llm.complete(SYSTEM_PROMPT, msgs)
            unknown = unknown_identifiers(session, erp, resp.text)
            cites = sorted(set(re.findall(r"\[(?:TABLE|JOIN|TCODE|DOC):[^\]]+\]", resp.text)))
            return AssistantAnswer(resp.text, "llm", cites, unknown, ctx_tables)
        except LLMError as e:
            log.warning("assistant falling back to metadata mode: %s", e)
    return metadata_answer(session, erp, question, ent, ctx_tables)


def metadata_answer(session: Session, erp: ERPSystem, question: str, ent: dict, ctx_tables: list[str]) -> AssistantAnswer:
    """Deterministic answers built only from the repository (used when no LLM is configured/available)."""
    q = question.lower()
    parts, cites, sql = [], [], None
    for tx in ent["transactions"]:
        cites.append(f"[TCODE:{tx.code}]")
        rows = "\n".join(f"| {l.table.name} | {l.role} | {l.table.description or ''} | {l.table.validation_status} |"
                         for l in tx.table_links)
        parts.append(f"### {tx.code} — {tx.name}\n{tx.description or ''}\n\n**Business logic:** {tx.business_logic or 'n/a'}\n\n"
                     f"| Table | Role | Description | Validation |\n|---|---|---|---|\n{rows}\n\n"
                     f"_Provenance: {tx.source_type}, {tx.validation_status}, confidence {tx.confidence:.2f}. "
                     f"Open **T-Code Intelligence** for SQL, joins and the dbt model._")
    tables = ent["tables"]
    if len(tables) >= 2:
        try:
            plan = plan_joins(session, erp, [t.name for t in tables], root=tables[0].name, include_pending=False)
            lines = [f"| {e.left_table} | {e.right_table} | `{e.condition_sql}` | {e.join_type} | {e.cardinality} | "
                     f"{e.relationship_type} | {e.validation_status} ({e.confidence:.2f}) |" for e in plan.edges]
            parts.append("### Relationships\n| From | To | Condition | Join | Card. | Type | Validation |\n"
                         "|---|---|---|---|---|---|---|\n" + "\n".join(lines))
            for e in plan.edges:
                if e.description:
                    parts.append(f"- **{e.left_table} ↔ {e.right_table}:** {e.description}. {e.business_purpose or ''}")
                cites.append(f"[JOIN:{e.relationship_id}]")
            if plan.warnings:
                parts.append("**Notes:**\n" + "\n".join(f"- {w}" for w in plan.warnings))
            if any(k in q for k in ("join", "query", "sql", "select")):
                from erpdm.services.sqlgen import default_columns_for, generate_sql
                cols = default_columns_for(session, erp, plan.tables, max_per_table=4)
                sql = generate_sql(session, erp, plan, cols, "Snowflake").sql
                parts.append(f"```sql\n{sql}\n```")
        except PlanningError as e:
            parts.append(f"No trusted join path: {e}")
    if ("difference" in q or "explain" in q or "what is" in q or len(tables) == 1 or "contain" in q) and tables:
        for t in tables[:4]:
            cites.append(f"[TABLE:{t.name}]")
            keys = ", ".join(t.primary_key)
            biz = ", ".join(f"{c.name} ({c.business_name})" for c in t.columns if c.business_name)[:600]
            parts.append(f"**{t.name}** — {t.description}. Class: {t.table_class}; key: {keys}. "
                         f"Business columns: {biz or '—'}. {t.notes or ''} "
                         f"_({t.source_type}, {t.validation_status}, {t.confidence:.2f})_")
    if not tables and not ent["transactions"]:
        content = [t for t in re.findall(r"[a-z0-9]+", q) if t not in QUESTION_STOP and len(t) > 2]
        hits = [h for h in search(session, question, erp_id=erp.id, limit=12)
                if _coverage(content, f"{h.title} {h.snippet}") >= 0.5][:8]
        top_tables = [h for h in hits if h.entity_type == "table"][:2]
        if top_tables:
            parts.append("**Most likely table(s):** " + "; ".join(
                f"**{h.title.split(' — ')[0]}** — {h.title.split(' — ', 1)[-1]} _({h.validation_status})_" for h in top_tables))
            cites += [f"[TABLE:{h.title.split(' — ')[0]}]" for h in top_tables]
        if hits:
            parts.append("I couldn't pin the question to a specific table or T-code. Closest repository matches:\n"
                         + "\n".join(f"- **{h.entity_type}** · {h.title} — {h.snippet[:140]}" for h in hits))
            if any(w in q for w in ("flow", "process")):
                procs = [session.get(BusinessProcess, h.entity_id) for h in hits if h.entity_type == "process"]
                for p in procs[:2]:
                    parts.append(f"**{p.name}:** " + " → ".join(p.flow_steps or []))
        docs = search_documents(session, question, erp.id, limit=3)
        for i, (chunk, _) in enumerate(docs, start=1):
            parts.append(f"[DOC:{i}] *{chunk.document.filename}*: {chunk.content[:400]}…")
            cites.append(f"[DOC:{i}]")
    if not parts:
        return AssistantAnswer(
            f"I don't have metadata in the **{erp.name}** repository that answers this. To avoid guessing, please "
            "upload the table structure (DDL / DD03L extract), relationship definitions or documentation in "
            "**Metadata Management**, or rephrase with a table name or T-code.", "not_found")
    footer = ("\n\n---\n_Answer assembled from the metadata repository only (no AI). Items not marked 'verified' "
              "must be validated against your system._")
    return AssistantAnswer("\n\n".join(parts) + footer, "metadata", sorted(set(cites)), [], ctx_tables, sql)


# ----------------------------------------------------------------------------- AI relationship suggestions
REL_PROMPT = """You propose join relationships between ERP tables for a metadata repository.
Use ONLY the tables and columns provided. Output a JSON array; each item:
{"from_table": str, "to_table": str, "join_columns": [{"from": str, "to": str}], "cardinality": "1:1|1:N|N:1",
 "relationship_type": "header_item|master_detail|lookup|text|reference|document_flow",
 "extra_condition": str|null, "description": str, "confidence": number between 0 and 0.6}
Only include joins you are confident exist in the standard data model. No prose, JSON only."""


def suggest_relationships(session: Session, erp: ERPSystem, table_names: list[str], user: str,
                          llm: LLMClient | None = None) -> tuple[int, list[str]]:
    """Ask the LLM for joins; every suggestion is structurally validated, then stored as ai_inferred/pending."""
    llm = llm or LLMClient()
    if not llm.enabled:
        raise LLMError("AI suggestions need an LLM provider (LLM_PROVIDER / LLM_API_KEY).")
    tables = [get_table(session, erp.id, n) for n in table_names]
    tables = [t for t in tables if t]
    context = "\n".join(_table_block(t, 80) for t in tables)
    resp = llm.complete(REL_PROMPT, [{"role": "user", "content": f"ERP: {erp.name}\n{context}"}], temperature=0)
    items = extract_json(resp.text)
    stored, rejected = 0, []
    for it in items if isinstance(items, list) else []:
        try:
            rel = RelationshipIn(
                from_table=it["from_table"], to_table=it["to_table"],
                join_columns=[JoinPair(**{"from": j["from"], "to": j["to"]}) for j in it["join_columns"]],
                cardinality=it.get("cardinality", "uncertain"), relationship_type=it.get("relationship_type", "reference"),
                extra_condition=it.get("extra_condition"), description=it.get("description"),
                evidence=f"AI suggestion from {resp.provider}:{resp.model}",
                source_type=SourceType.AI_INFERRED.value, confidence=min(float(it.get("confidence", 0.5)), 0.6),
                validation_status=ValidationStatus.PENDING.value)
        except (KeyError, TypeError, ValueError) as e:
            rejected.append(f"Malformed suggestion {it}: {e}")
            continue
        errors, _ = check_relationship(session, erp, rel)
        if errors:
            rejected.append(f"{rel.from_table}→{rel.to_table}: " + "; ".join(errors))
            continue
        upsert_relationship(session, erp, rel, user)
        stored += 1
    return stored, rejected
