"""Business use-case explorer: natural-language requirement → ERP, process, tables, columns, joins, SQL."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from erpdm.constants import BUSINESS_DOMAINS
from erpdm.db.models import BusinessProcess, ERPSystem, UseCase
from erpdm.schemas import JoinPlan, SelectedColumn
from erpdm.services.mapping import ColumnMatch, resolve_business_columns
from erpdm.services.metadata import get_table, list_erps
from erpdm.services.relationships import PlanningError, plan_joins
from erpdm.services.search import bm25_rank, tokenize

STOP = {"i", "want", "to", "build", "a", "an", "the", "that", "provides", "provide", "with", "and", "of", "for",
        "data", "model", "details", "information", "info", "including", "include", "need", "create", "report",
        "on", "in", "by", "which", "shows", "show", "list", "all", "their", "its", "my", "our", "we"}
GENERIC_TAILS = ("details", "information", "info", "data", "attributes")


@dataclass
class UseCaseAnalysis:
    erp: ERPSystem | None
    erp_reason: str
    domain: str | None
    use_case: UseCase | None
    use_case_score: float
    alternatives: list[str]
    process: BusinessProcess | None
    phrases: list[str]
    matches: list[ColumnMatch]
    mandatory_tables: list[str]
    optional_tables: list[str]
    plan: JoinPlan | None
    columns: list[SelectedColumn]
    explanation: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def extract_phrases(text: str) -> list[str]:
    """'equipment details, serial numbers, material information and plant' → candidate attribute phrases."""
    t = re.sub(r"[\"'“”]", "", text.lower())
    m = re.search(r"\b(?:provides?|with|including|containing|showing|shows|covering|having|of)\b(.*)$", t)
    body = m.group(1) if m else t
    parts = re.split(r",|;|\band\b|\bplus\b|\bas well as\b|/", body)
    out = []
    for p in parts:
        words = [w for w in re.findall(r"[a-z0-9]+", p) if w not in STOP]
        if not words:
            continue
        phrase = " ".join(words)
        phrase = re.sub(r"(numbers|categories|locations|plants|manufacturers|descriptions)$",
                        lambda m: {"numbers": "number", "categories": "category"}.get(m.group(1), m.group(1)[:-1]), phrase)
        out.append(phrase)
    return list(dict.fromkeys(out))


def detect_erp(session: Session, text: str, selected: ERPSystem | None) -> tuple[ERPSystem | None, str]:
    low = text.lower()
    for erp in sorted(list_erps(session), key=lambda e: -len(e.name)):
        names = {erp.code.lower().replace("_", " "), erp.code.lower(), erp.name.lower()}
        if any(re.search(rf"\b{re.escape(n)}\b", low) for n in names if len(n) > 2):
            return erp, f"Mentioned in the requirement ({erp.name})"
    if selected:
        return selected, "Selected ERP (not mentioned in the requirement)"
    return None, "No ERP identified — select one"


def analyze_requirement(session: Session, text: str, selected_erp: ERPSystem | None = None,
                        include_optional: bool = True, include_pending: bool = False) -> UseCaseAnalysis:
    erp, reason = detect_erp(session, text, selected_erp)
    phrases = extract_phrases(text)
    terms = tokenize(text)
    domain = None
    for d in BUSINESS_DOMAINS:
        if all(w in terms for w in tokenize(d)):
            domain = d
            break
    if erp is None:
        return UseCaseAnalysis(None, reason, domain, None, 0, [], None, phrases, [], [], [], None, [],
                               ["Select or mention an ERP system."])
    ucs = list(session.scalars(select(UseCase).where(UseCase.erp_id == erp.id)))
    ranked = bm25_rank([(u, f"{u.name} {u.name} {u.domain or ''} {u.description or ''} "
                           f"{' '.join(u.keywords or [])} {' '.join(u.keywords or [])}") for u in ucs], terms)
    uc, score = (ranked[0] if ranked else (None, 0.0))
    alternatives = [u.name for u, _ in ranked[1:4]]
    if uc and uc.domain:
        domain = uc.domain  # the matched use case is a stronger signal than loose keywords
    process = None
    if uc and uc.process_id:
        process = session.get(BusinessProcess, uc.process_id)
    if process is None and domain:
        process = session.scalar(select(BusinessProcess).where(BusinessProcess.erp_id == erp.id,
                                                               BusinessProcess.domain == domain))
    context = list((uc.tables or []) + (uc.optional_tables or [])) if uc else []
    attribute_phrases = []
    for p in phrases:
        base = p
        for tail in GENERIC_TAILS:
            base = re.sub(rf"\s+{tail}$", "", base)
        attribute_phrases.append(base.strip())
    matches = resolve_business_columns(session, erp, attribute_phrases, context) if attribute_phrases else []
    explanation = [f"ERP: {erp.name} — {reason}."]
    if uc:
        explanation.append(f"Best matching use case: '{uc.name}' (relevance {score:.2f}). Grain: {uc.grain or 'n/a'}.")
    else:
        explanation.append("No stored use case matched; tables come from column matches only.")
    if process:
        explanation.append(f"Business process: {process.name} ({process.domain}).")
    unresolved = [m.business_column for m in matches if m.method == "unmatched"]
    # "equipment details" style phrases that map to an entity rather than a column are expected to be unresolved.
    mandatory = list(uc.tables or []) if uc else []
    optional = list(uc.optional_tables or []) if uc else []
    for m in matches:
        if m.table and m.table not in mandatory and m.table not in optional:
            optional.append(m.table)
    tables = mandatory + (optional if include_optional else [t for t in optional if any(m.table == t for m in matches)])
    tables = [t for t in dict.fromkeys(tables) if get_table(session, erp.id, t)]
    columns: list[SelectedColumn] = []
    for m in matches:
        if m.table and m.column:
            columns.append(SelectedColumn(table=m.table, column=m.column, business_name=m.business_column))
    plan = None
    if tables:
        try:
            plan = plan_joins(session, erp, tables, root=uc.root_table if uc else None, include_pending=include_pending)
        except PlanningError as e:
            explanation.append(f"Join planning failed: {e}")
    if plan:
        # Grain key + key business columns of the root if the requirement didn't name them.
        root = get_table(session, erp.id, plan.root_table)
        client = (erp.client_column or "").upper()
        have = {(c.table.upper(), c.column.upper()) for c in columns}
        for k in root.primary_key:
            if k.upper() != client and (root.name.upper(), k.upper()) not in have:
                columns.insert(0, SelectedColumn(table=root.name, column=k, business_name=None))
        columns = [c for c in columns if c.table in plan.tables]
        explanation.append(f"Mandatory joins (INNER): {[f'{e.left_table}→{e.right_table}' for e in plan.edges if e.join_type == 'INNER'] or 'none'}; "
                           f"optional joins (LEFT): {[f'{e.left_table}→{e.right_table}' for e in plan.edges if e.join_type == 'LEFT']}.")
    if unresolved:
        explanation.append(f"Phrases without a column match (map manually or upload metadata): {unresolved}.")
    return UseCaseAnalysis(erp, reason, domain, uc, score, alternatives, process, phrases, matches, mandatory,
                           optional, plan, columns, explanation, unresolved)
