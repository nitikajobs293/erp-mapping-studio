"""ERP metadata repository: onboarding, CRUD, validation workflow, search index maintenance."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from erpdm.constants import SourceType, ValidationStatus
from erpdm.db.models import (
    BusinessProcess, BusinessTerm, Document, ERPColumn, ERPModule, ERPSystem, ERPTable,
    GeneratedArtifact, Mapping, Relationship, SearchIndex, Transaction, TransactionTable, UseCase,
)
from erpdm.schemas import ERPSystemIn, MetadataPackage, RelationshipIn, TableIn
from erpdm.services.audit import audit, record_version

FALLBACK_PROVENANCE = {
    "source_type": SourceType.STANDARD_ERP_KNOWLEDGE.value,
    "confidence": 0.8,
    "validation_status": ValidationStatus.REQUIRES_VALIDATION.value,
}


class MetadataError(Exception):
    pass


@dataclass
class ImportResult:
    created: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    def bump(self, kind: str, created: bool) -> None:
        bucket = self.created if created else self.updated
        bucket[kind] = bucket.get(kind, 0) + 1


def _prov(item, defaults: dict) -> dict:
    out = {}
    for k in ("source_type", "confidence", "validation_status"):
        v = getattr(item, k, None)
        out[k] = v if v is not None else defaults.get(k, FALLBACK_PROVENANCE[k])
    return out


# ----------------------------------------------------------------------------- ERP systems
def list_erps(session: Session, active_only: bool = True) -> list[ERPSystem]:
    q = select(ERPSystem).order_by(ERPSystem.name)
    if active_only:
        q = q.where(ERPSystem.is_active.is_(True))
    return list(session.scalars(q))


def get_erp(session: Session, code: str) -> ERPSystem | None:
    return session.scalar(select(ERPSystem).where(func.upper(ERPSystem.code) == (code or "").upper()))


def require_erp(session: Session, code: str) -> ERPSystem:
    erp = get_erp(session, code)
    if erp is None:
        raise MetadataError(f"ERP system {code!r} is not registered.")
    return erp


def upsert_erp(session: Session, data: ERPSystemIn, user: str | None) -> tuple[ERPSystem, bool]:
    erp = get_erp(session, data.code)
    created = erp is None
    if created:
        erp = ERPSystem(code=data.code)
        session.add(erp)
    for k, v in data.model_dump(exclude={"code"}).items():
        if v is not None or created:
            setattr(erp, k, v)
    erp.updated_by = user
    session.flush()
    record_version(session, "erp_system", erp, "create" if created else "update", user)
    audit(session, user, "erp.create" if created else "erp.update", "erp_system", erp.id, {"code": erp.code})
    _index(session, erp.id, "erp", erp.id, f"{erp.code} — {erp.name}",
           " ".join(filter(None, [erp.vendor, erp.version_label, erp.description, erp.landscape_notes])))
    return erp, created


def get_or_create_module(session: Session, erp: ERPSystem, code: str, name: str | None = None) -> ERPModule:
    mod = session.scalar(select(ERPModule).where(ERPModule.erp_id == erp.id,
                                                 func.upper(ERPModule.code) == code.upper()))
    if mod is None:
        mod = ERPModule(erp_id=erp.id, code=code.upper(), name=name or code.upper())
        session.add(mod)
        session.flush()
    elif name and mod.name != name:
        mod.name = name
    return mod


# ----------------------------------------------------------------------------- tables / columns
def get_table(session: Session, erp_id: int, name: str) -> ERPTable | None:
    return session.scalar(
        select(ERPTable).options(selectinload(ERPTable.columns))
        .where(ERPTable.erp_id == erp_id, func.upper(ERPTable.name) == (name or "").upper()))


def require_table(session: Session, erp_id: int, name: str) -> ERPTable:
    t = get_table(session, erp_id, name)
    if t is None:
        raise MetadataError(f"Table {name!r} is not in the repository for this ERP.")
    return t


def get_column(table: ERPTable, name: str) -> ERPColumn | None:
    up = name.upper()
    return next((c for c in table.columns if c.name.upper() == up), None)


def list_tables(session: Session, erp_id: int, module: str | None = None, table_class: str | None = None,
                text: str | None = None, validation_status: str | None = None,
                page: int = 1, page_size: int = 50) -> tuple[list[ERPTable], int]:
    q = select(ERPTable).where(ERPTable.erp_id == erp_id)
    if module:
        q = q.join(ERPModule, ERPTable.module_id == ERPModule.id).where(ERPModule.code == module)
    if table_class:
        q = q.where(ERPTable.table_class == table_class)
    if validation_status:
        q = q.where(ERPTable.validation_status == validation_status)
    if text:
        like = f"%{text.lower()}%"
        q = q.where(or_(func.lower(ERPTable.name).like(like), func.lower(ERPTable.description).like(like)))
    total = session.scalar(select(func.count()).select_from(q.subquery()))
    rows = session.scalars(q.options(selectinload(ERPTable.columns), selectinload(ERPTable.module))
                           .order_by(ERPTable.name).offset((page - 1) * page_size).limit(page_size))
    return list(rows), int(total or 0)


def upsert_table(session: Session, erp: ERPSystem, t: TableIn, user: str | None,
                 defaults: dict | None = None, result: ImportResult | None = None) -> ERPTable:
    defaults = defaults or {}
    table = get_table(session, erp.id, t.name)
    created = table is None
    prov = _prov(t, defaults)
    if created:
        table = ERPTable(erp_id=erp.id, name=t.name.upper() if erp.code.startswith("SAP") else t.name)
        session.add(table)
    module = get_or_create_module(session, erp, t.module) if t.module else None
    for attr in ("description", "table_type", "table_class", "business_entity", "change_tracking",
                 "delete_strategy", "soft_delete_rule", "default_filter", "notes"):
        val = getattr(t, attr)
        if val is not None and val != "unknown" or created:
            setattr(table, attr, val)
    if module:
        table.module_id = module.id
    # Don't downgrade already-verified metadata with a weaker source.
    if created or table.validation_status != ValidationStatus.VERIFIED.value:
        table.source_type, table.confidence, table.validation_status = (
            prov["source_type"], prov["confidence"], prov["validation_status"])
    table.source_reference = t.source_reference or defaults.get("source_reference") or table.source_reference
    table.updated_by = user
    session.flush()
    existing = {c.name.upper(): c for c in table.columns}
    for i, c in enumerate(t.columns, start=1):
        col = existing.get(c.name.upper())
        cprov = _prov(c, prov)
        if col is None:
            col = ERPColumn(table_id=table.id, name=c.name, ordinal=i)
            table.columns.append(col)
        for attr in ("description", "data_type", "length", "decimals", "business_name",
                     "business_definition", "check_table"):
            val = getattr(c, attr)
            if val is not None:
                setattr(col, attr, val)
        col.is_pk = bool(c.is_pk or col.is_pk)
        col.is_nullable = bool(c.is_nullable and not col.is_pk)
        if col.validation_status != ValidationStatus.VERIFIED.value:
            col.source_type, col.confidence, col.validation_status = (
                cprov["source_type"], cprov["confidence"], cprov["validation_status"])
    session.flush()
    record_version(session, "erp_table", table, "create" if created else "update", user)
    reindex_table(session, table)
    if result is not None:
        result.bump("tables", created)
    return table


def reindex_table(session: Session, table: ERPTable) -> None:
    cols = " ".join(f"{c.name} {c.description or ''} {c.business_name or ''}" for c in table.columns)
    mod = table.module.code if table.module else None
    _index(session, table.erp_id, "table", table.id, f"{table.name} — {table.description or ''}",
           f"{table.business_entity or ''} {table.table_class} {cols}", mod, table.validation_status)


# ----------------------------------------------------------------------------- relationships
def check_relationship(session: Session, erp: ERPSystem, r: RelationshipIn) -> tuple[list[str], list[str]]:
    """Structural validation. Returns (errors, warnings). Errors block import."""
    errors, warnings = [], []
    ft, tt = get_table(session, erp.id, r.from_table), get_table(session, erp.id, r.to_table)
    if ft is None:
        errors.append(f"Unknown table {r.from_table}")
    if tt is None:
        errors.append(f"Unknown table {r.to_table}")
    if errors:
        return errors, warnings
    if not r.join_columns:
        errors.append("No join columns given")
    for jp in r.join_columns:
        fc, tc = get_column(ft, jp.from_), get_column(tt, jp.to)
        if fc is None:
            errors.append(f"{ft.name}.{jp.from_} does not exist")
        if tc is None:
            errors.append(f"{tt.name}.{jp.to} does not exist")
        if fc and tc and fc.data_type and tc.data_type and _type_family(fc.data_type) != _type_family(tc.data_type):
            warnings.append(f"Type mismatch {ft.name}.{fc.name} ({fc.data_type}) vs {tt.name}.{tc.name} ({tc.data_type})")
    if errors:
        return errors, warnings
    # Cardinality sanity: for 1:N the 'from' side join columns should cover the from-table PK
    # (excluding client column); for N:1 the 'to' side should cover the to-table PK.
    client = (erp.client_column or "").upper()
    from_cols = {jp.from_.upper() for jp in r.join_columns}
    to_cols = {jp.to.upper() for jp in r.join_columns}
    from_pk = {c for c in (x.upper() for x in ft.primary_key) if c != client}
    to_pk = {c for c in (x.upper() for x in tt.primary_key) if c != client}
    # (The 'from' side of a 1:N may legitimately join on a unique alternate key such as EQUI.OBJNR,
    #  so only the side that would multiply rows is checked.)
    del from_cols, from_pk
    if r.cardinality in ("N:1", "1:1") and to_pk and not to_pk <= to_cols:
        extra = (r.extra_condition or "").upper()
        missing = [k for k in to_pk - to_cols if k not in extra]
        if missing:
            warnings.append(f"Cardinality {r.cardinality} claims {tt.name} is the 'one' side but key columns "
                            f"{missing} are neither joined nor filtered — duplicates possible.")
    return errors, warnings


def _type_family(t: str) -> str:
    t = t.upper()
    if any(x in t for x in ("CHAR", "CHR", "STRING", "TEXT", "CLNT", "LANG", "CUKY", "UNIT", "NUMC", "VARCHAR")):
        return "string"
    if any(x in t for x in ("DATS", "DATE", "TIMESTAMP", "TIMS")):
        return "temporal"
    if any(x in t for x in ("INT", "DEC", "NUM", "CURR", "QUAN", "FLOAT", "FLTP", "PREC")):
        return "number"
    return t


def find_relationship(session: Session, erp_id: int, from_id: int, to_id: int,
                      join_columns: list[dict]) -> Relationship | None:
    key = sorted((d["from"].upper(), d["to"].upper()) for d in join_columns)
    for rel in session.scalars(select(Relationship).where(
            Relationship.erp_id == erp_id, Relationship.from_table_id == from_id,
            Relationship.to_table_id == to_id)):
        if sorted((d["from"].upper(), d["to"].upper()) for d in rel.join_columns) == key:
            return rel
    return None


def upsert_relationship(session: Session, erp: ERPSystem, r: RelationshipIn, user: str | None,
                        defaults: dict | None = None, result: ImportResult | None = None,
                        strict: bool = True) -> Relationship | None:
    errors, warnings = check_relationship(session, erp, r)
    if errors:
        msg = f"{r.from_table}->{r.to_table}: " + "; ".join(errors)
        if strict and result is None:
            raise MetadataError(msg)
        if result is not None:
            result.skipped.append(msg)
        return None
    ft, tt = get_table(session, erp.id, r.from_table), get_table(session, erp.id, r.to_table)
    jc = [{"from": get_column(ft, j.from_).name, "to": get_column(tt, j.to).name} for j in r.join_columns]
    rel = find_relationship(session, erp.id, ft.id, tt.id, jc)
    created = rel is None
    prov = _prov(r, defaults or {})
    if created:
        rel = Relationship(erp_id=erp.id, from_table_id=ft.id, to_table_id=tt.id, join_columns=jc)
        session.add(rel)
    for attr in ("extra_condition", "join_type", "cardinality", "relationship_type", "is_mandatory",
                 "description", "business_purpose"):
        setattr(rel, attr, getattr(r, attr))
    ev = "; ".join(filter(None, [r.evidence] + [f"check: {w}" for w in warnings]))
    rel.evidence = ev or rel.evidence
    if created or rel.validation_status not in (ValidationStatus.VERIFIED.value, ValidationStatus.REJECTED.value):
        rel.source_type, rel.confidence, rel.validation_status = (
            prov["source_type"], prov["confidence"], prov["validation_status"])
        if warnings and rel.validation_status == ValidationStatus.VERIFIED.value:
            rel.validation_status = ValidationStatus.REQUIRES_VALIDATION.value
    rel.updated_by = user
    session.flush()
    record_version(session, "relationship", rel, "create" if created else "update", user)
    reindex_relationship(session, rel)
    if result is not None:
        result.bump("relationships", created)
    return rel


def reindex_relationship(session: Session, rel: Relationship) -> None:
    ft, tt = session.get(ERPTable, rel.from_table_id), session.get(ERPTable, rel.to_table_id)
    cond = " AND ".join(f"{ft.name}.{d['from']} = {tt.name}.{d['to']}" for d in rel.join_columns)
    _index(session, rel.erp_id, "relationship", rel.id, f"{ft.name} → {tt.name} ({rel.relationship_type})",
           f"{cond} {rel.description or ''} {rel.business_purpose or ''}", None, rel.validation_status)


def relationships_among(session: Session, erp_id: int, table_ids: Iterable[int] | None = None,
                        include_rejected: bool = False) -> list[Relationship]:
    q = select(Relationship).options(selectinload(Relationship.from_table), selectinload(Relationship.to_table)) \
        .where(Relationship.erp_id == erp_id)
    if table_ids is not None:
        ids = list(table_ids)
        q = q.where(or_(Relationship.from_table_id.in_(ids), Relationship.to_table_id.in_(ids)))
    if not include_rejected:
        q = q.where(Relationship.validation_status != ValidationStatus.REJECTED.value)
    return list(session.scalars(q))


def set_relationship_status(session: Session, rel_id: int, status: str, user: str,
                            note: str | None = None) -> Relationship:
    rel = session.get(Relationship, rel_id)
    if rel is None:
        raise MetadataError("Relationship not found")
    if status not in {s.value for s in ValidationStatus}:
        raise MetadataError(f"Unknown status {status}")
    rel.validation_status = status
    if status == ValidationStatus.VERIFIED.value:
        rel.validated_by, rel.validated_at = user, datetime.now(timezone.utc)
        rel.confidence = max(rel.confidence, 0.95)
        if rel.source_type in (SourceType.AI_INFERRED.value, SourceType.UNVERIFIED_ASSUMPTION.value,
                               SourceType.EXISTING_SQL.value, SourceType.STANDARD_ERP_KNOWLEDGE.value):
            rel.evidence = (rel.evidence or "") + f" | originally {rel.source_type}; approved by {user}"
            rel.source_type = SourceType.USER_VERIFIED.value
    if note:
        rel.evidence = (rel.evidence or "") + f" | {user}: {note}"
    rel.updated_by = user
    record_version(session, "relationship", rel, "validate" if status == "verified" else status, user)
    audit(session, user, f"relationship.{status}", "relationship", rel.id, {"note": note})
    reindex_relationship(session, rel)
    return rel


def set_entity_status(session: Session, model, entity_id: int, status: str, user: str) -> None:
    obj = session.get(model, entity_id)
    if obj is None:
        raise MetadataError("Not found")
    obj.validation_status = status
    if status == ValidationStatus.VERIFIED.value:
        obj.confidence = max(obj.confidence or 0, 0.95)
    record_version(session, model.__tablename__, obj, status, user)
    audit(session, user, f"{model.__tablename__}.{status}", model.__tablename__, entity_id)


# ----------------------------------------------------------------------------- transactions / terms / use cases
def get_transaction(session: Session, erp_id: int, code: str) -> Transaction | None:
    return session.scalar(
        select(Transaction).options(selectinload(Transaction.table_links).selectinload(TransactionTable.table))
        .where(Transaction.erp_id == erp_id, func.upper(Transaction.code) == (code or "").strip().upper()))


def _upsert_transaction(session, erp, tx, user, defaults, result):
    obj = get_transaction(session, erp.id, tx.code)
    created = obj is None
    prov = _prov(tx, defaults)
    if created:
        obj = Transaction(erp_id=erp.id, code=tx.code.upper())
        session.add(obj)
    for attr in ("name", "transaction_type", "description", "business_purpose", "business_logic",
                 "business_object", "app_components", "root_table", "default_columns"):
        val = getattr(tx, attr)
        if val is not None:
            setattr(obj, attr, val)
    if tx.module:
        obj.module_id = get_or_create_module(session, erp, tx.module).id
    if tx.process:
        proc = session.scalar(select(BusinessProcess).where(BusinessProcess.erp_id == erp.id,
                                                            BusinessProcess.name == tx.process))
        obj.process_id = proc.id if proc else None
    obj.source_type, obj.confidence, obj.validation_status = (
        prov["source_type"], prov["confidence"], prov["validation_status"])
    obj.updated_by = user
    session.flush()
    linked = {l.table_id: l for l in obj.table_links}
    for link in tx.tables:
        t = get_table(session, erp.id, link["table"])
        if t is None:
            result.skipped.append(f"T-Code {tx.code}: table {link['table']} not in repository")
            continue
        if t.id in linked:
            linked[t.id].role = link.get("role", linked[t.id].role)
            linked[t.id].notes = link.get("notes", linked[t.id].notes)
        else:
            obj.table_links.append(TransactionTable(table_id=t.id, role=link.get("role", "secondary"),
                                                    notes=link.get("notes")))
    session.flush()
    record_version(session, "transaction", obj, "create" if created else "update", user)
    tables = " ".join(l["table"] for l in tx.tables)
    _index(session, erp.id, "transaction", obj.id, f"{obj.code} — {obj.name}",
           f"{obj.description or ''} {obj.business_purpose or ''} {obj.business_object or ''} {tables}",
           tx.module, obj.validation_status)
    result.bump("transactions", created)


def _upsert_term(session, erp, term, user, defaults, result):
    obj = session.scalar(select(BusinessTerm).where(BusinessTerm.erp_id == erp.id,
                                                    func.lower(BusinessTerm.term) == term.term.lower()))
    created = obj is None
    prov = _prov(term, defaults)
    if term.table:
        t = get_table(session, erp.id, term.table)
        if t is None or (term.column and get_column(t, term.column) is None):
            result.skipped.append(f"Term '{term.term}': {term.table}.{term.column} not in repository")
            return
    if created:
        obj = BusinessTerm(erp_id=erp.id, term=term.term)
        session.add(obj)
    obj.synonyms = term.synonyms
    for attr in ("definition", "domain", "transformation", "lookup_logic"):
        val = getattr(term, attr)
        if val is not None:
            setattr(obj, attr, val)
    obj.table_name = term.table.upper() if term.table else None
    obj.column_name = term.column.upper() if term.column else None
    if created or obj.validation_status != ValidationStatus.VERIFIED.value:
        obj.source_type, obj.confidence, obj.validation_status = (
            prov["source_type"], prov["confidence"], prov["validation_status"])
    obj.updated_by = user
    session.flush()
    _index(session, erp.id, "term", obj.id, obj.term,
           f"{' '.join(obj.synonyms or [])} {obj.definition or ''} {obj.table_name or ''}.{obj.column_name or ''}",
           None, obj.validation_status)
    result.bump("terms", created)


def _upsert_use_case(session, erp, uc, user, defaults, result):
    obj = session.scalar(select(UseCase).where(UseCase.erp_id == erp.id, UseCase.name == uc.name))
    created = obj is None
    missing = [t for t in uc.tables + uc.optional_tables if get_table(session, erp.id, t) is None]
    if missing:
        result.skipped.append(f"Use case '{uc.name}': tables not in repository {missing}")
    if created:
        obj = UseCase(erp_id=erp.id, name=uc.name)
        session.add(obj)
    for attr in ("domain", "description", "keywords", "root_table", "grain"):
        setattr(obj, attr, getattr(uc, attr))
    obj.tables = [t for t in uc.tables if t not in missing]
    obj.optional_tables = [t for t in uc.optional_tables if t not in missing]
    prov = _prov(uc, defaults)
    obj.source_type, obj.confidence, obj.validation_status = (
        prov["source_type"], prov["confidence"], prov["validation_status"])
    obj.updated_by = user
    session.flush()
    _index(session, erp.id, "use_case", obj.id, obj.name,
           f"{obj.domain or ''} {obj.description or ''} {' '.join(obj.keywords or [])} {' '.join(obj.tables or [])}")
    result.bump("use_cases", created)


def _upsert_process(session, erp, p, user, result):
    obj = session.scalar(select(BusinessProcess).where(BusinessProcess.erp_id == erp.id,
                                                       BusinessProcess.name == p.name))
    created = obj is None
    if created:
        obj = BusinessProcess(erp_id=erp.id, name=p.name)
        session.add(obj)
    obj.domain, obj.sub_process, obj.description, obj.flow_steps = p.domain, p.sub_process, p.description, p.flow_steps
    if p.module:
        obj.module_id = get_or_create_module(session, erp, p.module).id
    session.flush()
    _index(session, erp.id, "process", obj.id, obj.name,
           f"{obj.domain or ''} {obj.sub_process or ''} {obj.description or ''} {' '.join(p.flow_steps)}", p.module)
    result.bump("processes", created)


# ----------------------------------------------------------------------------- package import
def import_package(session: Session, pkg: MetadataPackage, user: str | None,
                   erp_code: str | None = None) -> ImportResult:
    """Idempotent import of a metadata package into one ERP. Order matters: tables before joins."""
    result = ImportResult()
    if pkg.erp is not None:
        erp, created = upsert_erp(session, pkg.erp, user)
        result.bump("erp", created)
    else:
        if not erp_code:
            raise MetadataError("Package has no 'erp' section; choose a target ERP.")
        erp = require_erp(session, erp_code)
    defaults = pkg.defaults or {}
    for m in pkg.modules:
        get_or_create_module(session, erp, m.code, m.name)
    for p in pkg.processes:
        _upsert_process(session, erp, p, user, result)
    for t in pkg.tables:
        upsert_table(session, erp, t, user, defaults, result)
    for r in pkg.relationships:
        upsert_relationship(session, erp, r, user, defaults, result)
    for tx in pkg.transactions:
        _upsert_transaction(session, erp, tx, user, defaults, result)
    for term in pkg.terms:
        _upsert_term(session, erp, term, user, defaults, result)
    for uc in pkg.use_cases:
        _upsert_use_case(session, erp, uc, user, defaults, result)
    result.skipped.extend(pkg.warnings)
    audit(session, user, "metadata.import", "erp_system", erp.id,
          {"created": result.created, "updated": result.updated, "skipped": len(result.skipped)})
    return result


# ----------------------------------------------------------------------------- search index
def _index(session: Session, erp_id: int | None, entity_type: str, entity_id: int, title: str, body: str,
           module: str | None = None, status: str | None = None) -> None:
    row = session.scalar(select(SearchIndex).where(SearchIndex.entity_type == entity_type,
                                                   SearchIndex.entity_id == entity_id))
    if row is None:
        row = SearchIndex(entity_type=entity_type, entity_id=entity_id)
        session.add(row)
    row.erp_id, row.title, row.body, row.module, row.validation_status = (
        erp_id, title[:300], body, module, status)


# ----------------------------------------------------------------------------- export
def export_package(session: Session, erp: ERPSystem) -> MetadataPackage:
    """Round-trippable metadata package for one ERP (backup, promotion between environments, review)."""
    from erpdm.schemas import (ColumnIn, JoinPair, ModuleIn, ProcessIn, TermIn, TransactionIn, UseCaseIn)

    def prov(o) -> dict:
        return {"source_type": o.source_type, "confidence": o.confidence, "validation_status": o.validation_status}

    tables = list(session.scalars(select(ERPTable).options(selectinload(ERPTable.columns), selectinload(ERPTable.module))
                                  .where(ERPTable.erp_id == erp.id).order_by(ERPTable.name)))
    pkg = MetadataPackage(
        erp=ERPSystemIn(code=erp.code, name=erp.name, vendor=erp.vendor, version_label=erp.version_label,
                        category=erp.category, description=erp.description, landscape_notes=erp.landscape_notes,
                        extraction_method=erp.extraction_method, default_language_key=erp.default_language_key,
                        has_client_column=erp.has_client_column, client_column=erp.client_column),
        modules=[ModuleIn(code=m.code, name=m.name, description=m.description)
                 for m in session.scalars(select(ERPModule).where(ERPModule.erp_id == erp.id))],
        processes=[ProcessIn(name=p.name, domain=p.domain, module=p.module.code if p.module else None,
                             sub_process=p.sub_process, description=p.description, flow_steps=p.flow_steps or [])
                   for p in session.scalars(select(BusinessProcess).where(BusinessProcess.erp_id == erp.id))],
        tables=[TableIn(name=t.name, description=t.description, module=t.module.code if t.module else None,
                        table_type=t.table_type, table_class=t.table_class, business_entity=t.business_entity,
                        change_tracking=t.change_tracking, delete_strategy=t.delete_strategy,
                        soft_delete_rule=t.soft_delete_rule, default_filter=t.default_filter, notes=t.notes,
                        source_reference=t.source_reference, **prov(t),
                        columns=[ColumnIn(name=c.name, description=c.description, data_type=c.data_type, length=c.length,
                                          decimals=c.decimals, is_pk=c.is_pk, is_nullable=c.is_nullable,
                                          business_name=c.business_name, business_definition=c.business_definition,
                                          check_table=c.check_table, **prov(c)) for c in t.columns])
                for t in tables],
        relationships=[RelationshipIn(from_table=r.from_table.name, to_table=r.to_table.name,
                                      join_columns=[JoinPair(**{"from": j["from"], "to": j["to"]}) for j in r.join_columns],
                                      extra_condition=r.extra_condition, join_type=r.join_type, cardinality=r.cardinality,
                                      relationship_type=r.relationship_type, is_mandatory=r.is_mandatory,
                                      description=r.description, business_purpose=r.business_purpose,
                                      evidence=r.evidence, **prov(r))
                       for r in relationships_among(session, erp.id, include_rejected=True)],
        transactions=[TransactionIn(code=x.code, name=x.name, module=x.module.code if x.module else None,
                                    process=x.process.name if x.process else None, transaction_type=x.transaction_type,
                                    description=x.description, business_purpose=x.business_purpose,
                                    business_logic=x.business_logic, business_object=x.business_object,
                                    app_components=x.app_components, root_table=x.root_table,
                                    default_columns=x.default_columns, **prov(x),
                                    tables=[{"table": l.table.name, "role": l.role, "notes": l.notes} for l in x.table_links])
                      for x in session.scalars(select(Transaction).options(
                          selectinload(Transaction.table_links).selectinload(TransactionTable.table))
                          .where(Transaction.erp_id == erp.id))],
        terms=[TermIn(term=t.term, synonyms=t.synonyms or [], definition=t.definition, domain=t.domain, table=t.table_name,
                      column=t.column_name, transformation=t.transformation, lookup_logic=t.lookup_logic, **prov(t))
               for t in session.scalars(select(BusinessTerm).where(BusinessTerm.erp_id == erp.id))],
        use_cases=[UseCaseIn(name=u.name, domain=u.domain, description=u.description, keywords=u.keywords or [],
                             tables=u.tables or [], optional_tables=u.optional_tables or [], root_table=u.root_table,
                             grain=u.grain)
                   for u in session.scalars(select(UseCase).where(UseCase.erp_id == erp.id))],
    )
    return pkg


# ----------------------------------------------------------------------------- statistics
def dashboard_stats(session: Session) -> dict[str, int]:
    def c(model, *where):
        q = select(func.count()).select_from(model)
        for w in where:
            q = q.where(w)
        return int(session.scalar(q) or 0)

    return {
        "erp_systems": c(ERPSystem, ERPSystem.is_active.is_(True)),
        "modules": c(ERPModule),
        "tables": c(ERPTable),
        "columns": c(ERPColumn),
        "transactions": c(Transaction),
        "processes": c(BusinessProcess),
        "relationships": c(Relationship),
        "relationships_verified": c(Relationship, Relationship.validation_status == "verified"),
        "relationships_pending": c(Relationship, Relationship.validation_status.in_(["pending", "requires_validation"])),
        "mappings": c(Mapping),
        "dbt_models": c(GeneratedArtifact, GeneratedArtifact.kind == "dbt_model"),
        "sql_scripts": c(GeneratedArtifact, GeneratedArtifact.kind == "sql"),
        "documents": c(Document),
        "use_cases": c(UseCase),
        "terms": c(BusinessTerm),
    }


def per_erp_stats(session: Session) -> list[dict]:
    rows = []
    for erp in list_erps(session):
        tables = session.scalar(select(func.count()).select_from(ERPTable).where(ERPTable.erp_id == erp.id)) or 0
        rels = session.scalar(select(func.count()).select_from(Relationship).where(Relationship.erp_id == erp.id)) or 0
        ver = session.scalar(select(func.count()).select_from(Relationship).where(
            Relationship.erp_id == erp.id, Relationship.validation_status == "verified")) or 0
        tx = session.scalar(select(func.count()).select_from(Transaction).where(Transaction.erp_id == erp.id)) or 0
        rows.append({"ERP": erp.name, "Code": erp.code, "Category": erp.category, "Tables": tables,
                     "Relationships": rels, "Verified joins": ver, "T-Codes": tx,
                     "Status": "Metadata onboarding required" if tables == 0 else "Active"})
    return rows
