"""T-Code / transaction intelligence: everything about a transaction, assembled from the repository."""
from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from erpdm.db.models import ERPSystem, ERPTable, Transaction
from erpdm.schemas import GeneratedSQL, JoinPlan, SelectedColumn
from erpdm.services.metadata import get_table, get_transaction
from erpdm.services.modelling import ModelRecommendation, recommend_model
from erpdm.services.relationships import plan_joins, relationship_rows
from erpdm.services.sqlgen import default_columns_for, generate_sql


@dataclass
class TableInfo:
    name: str
    description: str | None
    role: str
    table_class: str
    primary_key: list[str]
    columns_used: list[str]
    notes: str | None
    source_type: str
    confidence: float
    validation_status: str


@dataclass
class TCodeAnalysis:
    erp: ERPSystem
    transaction: Transaction
    tables: list[TableInfo]
    relationships: list[dict]
    plan: JoinPlan
    columns: list[SelectedColumn]
    sql: GeneratedSQL
    model: ModelRecommendation
    verification: list[tuple[str, str]] = field(default_factory=list)  # (statement, label)


def find_similar_codes(session: Session, erp_id: int, code: str, limit: int = 5) -> list[str]:
    codes = list(session.scalars(select(Transaction.code).where(Transaction.erp_id == erp_id)))
    return [c for c, score, _ in process.extract(code.upper(), codes, scorer=fuzz.ratio, limit=limit) if score >= 50]


def analyze_transaction(session: Session, erp: ERPSystem, code: str, dialect: str = "Snowflake",
                        include_optional: bool = False, include_pending: bool = False,
                        raw_database: str | None = None, raw_schema: str | None = None) -> TCodeAnalysis | None:
    tx = get_transaction(session, erp.id, code)
    if tx is None:
        return None
    links = [l for l in tx.table_links if l.role != "status"]
    if tx.default_columns:
        columns = [SelectedColumn(table=c.split(".")[0], column=c.split(".")[1]) for c in tx.default_columns]
    else:
        columns = default_columns_for(session, erp, [l.table.name for l in links if l.role in ("primary", "secondary")])
    used_tables = list(dict.fromkeys(c.table for c in columns))
    if include_optional:
        used_tables += [l.table.name for l in links if l.table.name not in used_tables and l.role != "status"]
    root = tx.root_table or next((l.table.name for l in links if l.role == "primary"), used_tables[0])
    plan = plan_joins(session, erp, used_tables, root=root, include_pending=include_pending)
    sql = generate_sql(session, erp, plan, columns, dialect, raw_database=raw_database,
                       raw_schema=raw_schema or erp.code)
    model = recommend_model(session, erp, plan, columns)
    all_names = [l.table.name for l in tx.table_links]
    infos = []
    by_table: dict[str, list[str]] = {}
    for c in columns:
        by_table.setdefault(c.table.upper(), []).append(c.column)
    for l in tx.table_links:
        t: ERPTable = l.table
        infos.append(TableInfo(t.name, t.description, l.role, t.table_class, t.primary_key,
                               by_table.get(t.name.upper(), []), l.notes or t.notes, t.source_type,
                               t.confidence, t.validation_status))
    for name in plan.tables:  # bridge tables added by the planner
        if name not in all_names:
            t = get_table(session, erp.id, name)
            infos.append(TableInfo(t.name, t.description, "bridge", t.table_class, t.primary_key,
                                   by_table.get(t.name.upper(), []), t.notes, t.source_type, t.confidence,
                                   t.validation_status))
    rels = relationship_rows(session, erp.id, list(dict.fromkeys(all_names + plan.tables)))
    verification = [
        (f"T-Code {tx.code} definition", f"{tx.source_type} · {tx.validation_status} · confidence {tx.confidence:.2f}"),
        ("Backend tables", "From repository (" + ", ".join(sorted({i.validation_status for i in infos})) + ")"),
        ("Joins", f"{len(plan.edges)} joins; lowest confidence {plan.min_confidence:.2f}"),
        ("SQL, model and incremental strategy", "Generated from the metadata above — AI was not used"),
    ]
    if erp.landscape_notes and "ASSUMPTION" in erp.landscape_notes.upper():
        verification.insert(0, ("System structure", "ASSUMED from a reference system — validation required"))
    return TCodeAnalysis(erp, tx, infos, rels, plan, columns, sql, model, verification)
