"""Business column → source column resolution and source-to-target mapping documents."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from erpdm.config import get_settings
from erpdm.constants import SourceType, ValidationStatus
from erpdm.db.models import BusinessTerm, ERPColumn, ERPSystem, ERPTable, Mapping, MappingRow
from erpdm.schemas import JoinPlan, SelectedColumn, TermIn
from erpdm.services.audit import audit, record_version
from erpdm.services.metadata import _upsert_term, ImportResult, get_column, get_table
from erpdm.services.relationships import PlanningError, plan_joins
from erpdm.services.sqlgen import snake
from erpdm.services.typemap import snowflake_type, staging_expression


class MappingError(Exception):
    pass


@dataclass
class ColumnMatch:
    business_column: str
    table: str | None
    column: str | None
    method: str
    confidence: float
    source_type: str | None
    validation_status: str
    alternatives: list[str] = field(default_factory=list)
    note: str | None = None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def resolve_business_columns(session: Session, erp: ERPSystem, names: list[str],
                             context_tables: list[str] | None = None) -> list[ColumnMatch]:
    context = [t.upper() for t in (context_tables or [])]
    terms = list(session.scalars(select(BusinessTerm).where(BusinessTerm.erp_id == erp.id)))
    cols = list(session.execute(
        select(ERPColumn, ERPTable).join(ERPTable, ERPColumn.table_id == ERPTable.id)
        .where(ERPTable.erp_id == erp.id)).all())

    def ctx_rank(table: str) -> int:
        return context.index(table.upper()) if table.upper() in context else len(context) + 1

    out = []
    for raw in names:
        name = (raw or "").strip()
        if not name:
            continue
        n = _norm(name)
        # 1. explicit TABLE.COLUMN
        m = re.match(r"^([A-Za-z0-9_/]+)\.([A-Za-z0-9_/]+)$", name)
        if m:
            t = get_table(session, erp.id, m.group(1))
            c = get_column(t, m.group(2)) if t else None
            if c:
                out.append(ColumnMatch(name, t.name, c.name, "explicit", c.confidence, c.source_type, c.validation_status))
                continue
        # 2. column business names (prefer context tables)
        bn = [(c, t) for c, t in cols if c.business_name and _norm(c.business_name) == n]
        bn.sort(key=lambda ct: ctx_rank(ct[1].name))
        if bn and (not context or bn[0][1].name.upper() in context):
            c, t = bn[0]
            alts = [f"{t2.name}.{c2.name}" for c2, t2 in bn[1:] if ctx_rank(t2.name) == ctx_rank(t.name)]
            out.append(ColumnMatch(name, t.name, c.name, "business_name", round(min(c.confidence, 0.95), 2),
                                   c.source_type, ValidationStatus.REQUIRES_VALIDATION.value if alts else c.validation_status,
                                   alts + [f"{t2.name}.{c2.name}" for c2, t2 in bn[1:4] if f"{t2.name}.{c2.name}" not in alts],
                                   "Ambiguous: same business name in context" if alts else None))
            continue
        # 3. glossary terms + synonyms
        hit = next((t for t in terms if _norm(t.term) == n or n in {_norm(s) for s in (t.synonyms or [])}), None)
        if hit and hit.table_name:
            out.append(ColumnMatch(name, hit.table_name, hit.column_name, "glossary", hit.confidence,
                                   hit.source_type, hit.validation_status,
                                   [f"{t.name}.{c.name}" for c, t in bn[:3]]))
            continue
        if bn:  # business name match outside context
            c, t = bn[0]
            out.append(ColumnMatch(name, t.name, c.name, "business_name", round(min(c.confidence, 0.8), 2), c.source_type,
                                   ValidationStatus.REQUIRES_VALIDATION.value,
                                   [f"{t2.name}.{c2.name}" for c2, t2 in bn[1:4]], "Table not in the selected context"))
            continue
        # 4. technical column name
        tech = [(c, t) for c, t in cols if c.name.upper() == name.upper()]
        if tech:
            tech.sort(key=lambda ct: (ctx_rank(ct[1].name), not ct[0].is_pk))
            c, t = tech[0]
            out.append(ColumnMatch(name, t.name, c.name, "technical_name", 0.6, c.source_type,
                                   ValidationStatus.REQUIRES_VALIDATION.value, [f"{t2.name}.{c2.name}" for c2, t2 in tech[1:4]]))
            continue
        # 5. fuzzy against business names, descriptions and terms
        scored = []
        for c, t in cols:
            cand = [c.business_name or "", c.description or ""]
            s = max(fuzz.token_set_ratio(n, _norm(x)) for x in cand if x) if any(cand) else 0
            if s >= 80:
                scored.append((s - (5 if t.name.upper() not in context and context else 0), c, t))
        for term in terms:
            for x in [term.term] + list(term.synonyms or []):
                s = fuzz.token_set_ratio(n, _norm(x))
                if s >= 85 and term.table_name:
                    t = get_table(session, erp.id, term.table_name)
                    c = get_column(t, term.column_name) if t else None
                    if c:
                        scored.append((s, c, t))
        if scored:
            scored.sort(key=lambda x: (-x[0], ctx_rank(x[2].name)))
            s, c, t = scored[0]
            alts = list(dict.fromkeys(f"{t2.name}.{c2.name}" for _, c2, t2 in scored[1:6] if (t2.name, c2.name) != (t.name, c.name)))[:3]
            out.append(ColumnMatch(name, t.name, c.name, "fuzzy", round(0.3 + (s - 80) / 100, 2),
                                   SourceType.UNVERIFIED_ASSUMPTION.value, ValidationStatus.REQUIRES_VALIDATION.value, alts,
                                   f"Fuzzy text match ({s:.0f}%) — confirm"))
            continue
        out.append(ColumnMatch(name, None, None, "unmatched", 0.0, None, ValidationStatus.REQUIRES_VALIDATION.value,
                               note="No match in repository — upload metadata or map manually."))
    return out


def parse_business_column_file(filename: str, data: bytes) -> list[str]:
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "csv":
        df = pd.read_csv(io.BytesIO(data), dtype=str, header=None, sep=None, engine="python")
    elif ext in ("xlsx", "xls"):
        df = pd.read_excel(io.BytesIO(data), dtype=str, header=None)
    elif ext == "txt":
        return [l.strip() for l in data.decode("utf-8", "ignore").splitlines() if l.strip()]
    else:
        raise MappingError("Upload a CSV, XLSX or TXT file.")
    df = df.dropna(how="all")
    if df.empty:
        return []
    first = df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
    if first and re.sub(r"[^a-z]", "", first[0].lower()) in ("businesscolumn", "businesscolumns", "column", "columnname",
                                                              "businessname", "field", "attribute"):
        first = first[1:]
    return [x for x in first if x]


def create_mapping(session: Session, erp: ERPSystem, business_columns: list[str], user: str,
                   name: str | None = None, context_tables: list[str] | None = None, root_table: str | None = None,
                   target_database: str | None = None, target_schema: str | None = None,
                   target_table: str | None = None, transaction_code: str | None = None,
                   use_case: str | None = None, include_pending: bool = False,
                   matches: list[ColumnMatch] | None = None) -> Mapping:
    settings = get_settings()
    if not business_columns and not matches:
        raise MappingError("No business columns supplied.")
    matches = matches or resolve_business_columns(session, erp, business_columns, context_tables)
    matched_tables = list(dict.fromkeys(m.table for m in matches if m.table))
    root = root_table or (context_tables[0] if context_tables else (matched_tables[0] if matched_tables else None))
    plan: JoinPlan | None = None
    if matched_tables:
        try:
            plan = plan_joins(session, erp, matched_tables, root=root, include_pending=include_pending)
        except PlanningError as e:
            raise MappingError(str(e)) from e
    root_tbl = get_table(session, erp.id, plan.root_table) if plan else None
    entity = snake(root_tbl.business_entity or root_tbl.name) if root_tbl else "entity"
    m = Mapping(name=name or f"{erp.code} {transaction_code or use_case or entity} mapping",
                erp_id=erp.id, transaction_code=transaction_code, use_case=use_case,
                target_database=target_database or settings.target_database,
                target_schema=target_schema or "MARTS", target_table=target_table or f"DIM_{entity.upper()}",
                root_table=plan.root_table if plan else None, created_by=user, updated_by=user,
                join_plan=plan.model_dump() if plan else None)
    session.add(m)
    session.flush()
    edges_by_table = {e.right_table: e for e in plan.edges} if plan else {}
    parents = {e.right_table: e.left_table for e in plan.edges} if plan else {}
    used_targets: set[str] = set()
    for i, match in enumerate(matches, start=1):
        row = MappingRow(mapping_id=m.id, ordinal=i, business_column=match.business_column, source_erp=erp.code,
                         target_database=m.target_database, target_schema=m.target_schema, target_table=m.target_table,
                         match_method=match.method, confidence=match.confidence, source_type=match.source_type,
                         validation_status=match.validation_status)
        tgt = snake(match.business_column)
        while tgt in used_targets:
            tgt += "_2"
        used_targets.add(tgt)
        row.target_column = tgt
        if match.table and match.column:
            t = get_table(session, erp.id, match.table)
            c = get_column(t, match.column)
            row.source_module = t.module.code if t.module else None
            row.source_table, row.source_column = t.name, c.name
            row.source_data_type = f"{c.data_type or ''}({c.length})" if c.length else (c.data_type or "")
            row.target_data_type = snowflake_type(c.data_type, c.length, c.decimals)
            expr, conv = staging_expression(c.name, c.data_type, c.length, c.decimals)
            row.transformation = expr
            row.data_type_conversion = conv
            row.business_definition = c.business_definition or c.description
            e = edges_by_table.get(t.name)
            if e:
                row.join_condition = f"{e.join_type} JOIN {t.name} ON {e.condition_sql}"
                chain, cur = [t.name], t.name
                while cur in parents:
                    cur = parents[cur]
                    chain.append(cur)
                row.join_dependency = " → ".join(reversed(chain))
                if "'" in e.condition_sql:
                    row.filter_conditions = " AND ".join(p for p in e.condition_sql.split(" AND ") if "'" in p)
            elif plan and t.name == plan.root_table:
                row.join_dependency = f"{t.name} (root / grain)"
            if t.default_filter:
                f = t.default_filter.replace("{alias}", t.name)
                row.filter_conditions = f"{row.filter_conditions} AND {f}" if row.filter_conditions else f
            if t.table_class in ("text", "config") or (e and e.relationship_type in ("lookup", "text")):
                row.lookup_logic = (f"Lookup {t.name} via {', '.join(j['right'] for j in e.join_columns)}"
                                    if e else f"Lookup on {t.name}")
            if c.is_pk and t.name == (plan.root_table if plan else None):
                row.null_handling = "Must not be NULL (grain key)"
                row.data_quality_rules = "not_null; unique (with other key columns)"
            elif e and e.join_type == "LEFT":
                row.null_handling = "NULL when the related record does not exist (LEFT JOIN)" + (
                    "; text defaults to ''" if t.table_class == "text" else "")
                row.default_value = "''" if t.table_class == "text" else None
            else:
                row.null_handling = "Blank → NULL"
            rules = [row.data_quality_rules] if row.data_quality_rules else []
            if (c.data_type or "").upper() in ("DATS", "DATE"):
                rules.append("valid date (not 00000000 / not in future for creation dates)")
            if c.check_table:
                rules.append(f"relationships → {c.check_table}")
            row.data_quality_rules = "; ".join(r for r in rules if r) or None
            row.business_logic = (f"Source {t.name}.{c.name}: {c.description or ''}"
                                  + (f" (alternatives: {', '.join(match.alternatives)})" if match.alternatives else ""))
        else:
            row.business_logic = match.note
        if match.note and match.table:
            row.business_logic = f"{row.business_logic}. {match.note}"
        session.add(row)
    session.flush()
    record_version(session, "mapping", m, "create", user)
    audit(session, user, "mapping.create", "mapping", m.id, {"rows": len(matches), "erp": erp.code})
    return m


EDITABLE = ["business_column", "source_table", "source_column", "target_column", "target_data_type", "transformation",
            "default_value", "null_handling", "lookup_logic", "filter_conditions", "business_definition",
            "business_logic", "data_quality_rules", "validation_status"]


def get_mapping(session: Session, mapping_id: int) -> Mapping | None:
    return session.scalar(select(Mapping).options(selectinload(Mapping.rows)).where(Mapping.id == mapping_id))


def list_mappings(session: Session, erp_id: int | None = None) -> list[Mapping]:
    q = select(Mapping).order_by(Mapping.created_at.desc())
    if erp_id:
        q = q.where(Mapping.erp_id == erp_id)
    return list(session.scalars(q))


def mapping_dataframe(m: Mapping) -> pd.DataFrame:
    rows = []
    for r in m.rows:
        rows.append({
            "id": r.id, "Business Column": r.business_column, "ERP": r.source_erp, "Source Module": r.source_module,
            "Source Table": r.source_table, "Source Column": r.source_column, "Data Type": r.source_data_type,
            "Target Database": r.target_database, "Target Schema": r.target_schema, "Target Table": r.target_table,
            "Target Column": r.target_column, "Target Data Type": r.target_data_type,
            "Transformation": r.transformation, "Data Type Conversion": r.data_type_conversion,
            "Default Value": r.default_value, "Null Handling": r.null_handling, "Lookup Logic": r.lookup_logic,
            "Join Condition": r.join_condition, "Join Dependency": r.join_dependency,
            "Filter Conditions": r.filter_conditions, "Business Definition": r.business_definition,
            "Business Logic": r.business_logic, "Data Quality Rules": r.data_quality_rules,
            "Match Method": r.match_method, "Confidence": r.confidence, "Source Type": r.source_type,
            "Validation Status": r.validation_status,
        })
    return pd.DataFrame(rows)


COLUMN_TO_ATTR = {
    "Business Column": "business_column", "Source Table": "source_table", "Source Column": "source_column",
    "Target Column": "target_column", "Target Data Type": "target_data_type", "Transformation": "transformation",
    "Default Value": "default_value", "Null Handling": "null_handling", "Lookup Logic": "lookup_logic",
    "Filter Conditions": "filter_conditions", "Business Definition": "business_definition",
    "Business Logic": "business_logic", "Data Quality Rules": "data_quality_rules",
    "Validation Status": "validation_status",
}


def update_mapping_rows(session: Session, mapping_id: int, df: pd.DataFrame, user: str) -> int:
    m = get_mapping(session, mapping_id)
    if m is None:
        raise MappingError("Mapping not found")
    if m.status == "approved":
        raise MappingError("Approved mappings are read-only. Create a new version to change it.")
    erp_id = m.erp_id
    by_id = {r.id: r for r in m.rows}
    changed = 0
    for rec in df.to_dict("records"):
        rid = rec.get("id")
        if rid is None or pd.isna(rid):
            continue
        row = by_id.get(int(rid))
        if row is None:
            continue
        for col, attr in COLUMN_TO_ATTR.items():
            if col not in rec:
                continue
            val = rec[col]
            val = None if val is None or (isinstance(val, float) and pd.isna(val)) or val == "" else str(val)
            if getattr(row, attr) != val:
                if attr in ("source_table", "source_column") and val:
                    t = get_table(session, erp_id, rec.get("Source Table") or row.source_table)
                    if t is None or (rec.get("Source Column") and get_column(t, rec["Source Column"]) is None):
                        raise MappingError(f"Row '{row.business_column}': {rec.get('Source Table')}."
                                           f"{rec.get('Source Column')} does not exist in the repository.")
                if attr == "validation_status" and val not in {s.value for s in ValidationStatus}:
                    raise MappingError(f"Invalid validation status {val}")
                setattr(row, attr, val)
                if attr in ("source_table", "source_column"):
                    row.match_method, row.source_type = "manual", SourceType.USER_VERIFIED.value
                changed += 1
    m.updated_by = user
    m.status = "in_review" if changed else m.status
    record_version(session, "mapping", m, "update", user)
    audit(session, user, "mapping.update", "mapping", m.id, {"changed_cells": changed})
    return changed


def approve_mapping(session: Session, mapping_id: int, user: str, promote_terms: bool = True) -> list[str]:
    m = get_mapping(session, mapping_id)
    if m is None:
        raise MappingError("Mapping not found")
    issues = [f"'{r.business_column}' is unmapped" for r in m.rows if not r.source_table]
    if issues:
        raise MappingError("Cannot approve: " + "; ".join(issues))
    m.status, m.approved_by, m.approved_at = "approved", user, datetime.now(timezone.utc)
    for r in m.rows:
        r.validation_status = ValidationStatus.VERIFIED.value
        r.source_type = SourceType.USER_VERIFIED.value
    notes = []
    if promote_terms:
        from erpdm.db.models import ERPSystem as _E
        erp = session.get(_E, m.erp_id)
        res = ImportResult()
        for r in m.rows:
            _upsert_term(session, erp, TermIn(term=r.business_column, table=r.source_table, column=r.source_column,
                                               definition=r.business_definition, transformation=r.transformation,
                                               source_type=SourceType.USER_VERIFIED.value, confidence=1.0,
                                               validation_status=ValidationStatus.VERIFIED.value), user, {}, res)
        notes.append(f"Glossary updated: {res.created.get('terms', 0)} new, {res.updated.get('terms', 0)} updated terms.")
    record_version(session, "mapping", m, "approve", user)
    audit(session, user, "mapping.approve", "mapping", m.id)
    return notes


def selected_columns_from_mapping(m: Mapping) -> list[SelectedColumn]:
    return [SelectedColumn(table=r.source_table, column=r.source_column, alias=r.target_column,
                           business_name=r.business_column) for r in m.rows if r.source_table and r.source_column]
