"""Metadata ingestion: parse uploaded files into a normalised MetadataPackage and/or document chunks.

Supported: YAML/JSON metadata packages, DDL (.sql CREATE/ALTER), existing SELECT queries (.sql),
table/column inventories and SAP DDIC extracts (.csv/.xlsx), FK extracts (DD05S/DD08L-style),
T-code lists, mapping workbooks, and documentation (.pdf/.docx/.txt/.md) for retrieval.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd
import sqlglot
import yaml
from sqlglot import exp
from sqlalchemy import select
from sqlalchemy.orm import Session

from erpdm.config import get_settings
from erpdm.constants import SourceType, ValidationStatus
from erpdm.db.models import Document, DocumentChunk
from erpdm.logging_setup import get_logger
from erpdm.schemas import (
    ColumnIn, JoinPair, MetadataPackage, RelationshipIn, TableIn, TermIn, TransactionIn,
)
from erpdm.services.audit import audit
from erpdm.services.metadata import ImportResult, import_package

log = get_logger(__name__)

SUPPORTED_EXTENSIONS = {"xlsx", "xls", "csv", "pdf", "docx", "sql", "json", "yaml", "yml", "txt", "md"}

KIND_AUTO = "auto"
KINDS = {
    "auto": "Auto-detect",
    "package": "Metadata package (YAML/JSON)",
    "ddl": "Database DDL (CREATE / ALTER TABLE)",
    "sql_queries": "Existing SQL queries (extract joins)",
    "inventory": "Table / column inventory or SAP DDIC extract (DD03L)",
    "foreign_keys": "Foreign key definitions (DD05S/DD08L or FK list)",
    "tcodes": "T-Code / transaction list",
    "mapping": "Existing mapping workbook (builds glossary)",
    "document": "Documentation for retrieval (PDF/DOCX/TXT/MD/dbt)",
}


class IngestionError(Exception):
    pass


@dataclass
class ParseOutcome:
    kind: str
    package: MetadataPackage | None = None
    document_text: str | None = None
    warnings: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------- helpers
ALIASES = {
    "table": ["table_name", "tabname", "table", "tablename", "source_table", "tbl", "object_name"],
    "column": ["column_name", "fieldname", "column", "field", "field_name", "source_column", "col"],
    "description": ["column_description", "description", "ddtext", "field_description", "comments",
                    "scrtext_l", "fieldtext", "text"],
    "table_description": ["table_description", "table_text", "tabletext", "table_ddtext", "table_desc"],
    "data_type": ["data_type", "datatype", "type", "inttype", "column_type"],
    "length": ["length", "leng", "char_length", "data_length", "intlen"],
    "decimals": ["decimals", "scale", "numeric_scale", "data_scale"],
    "is_pk": ["is_pk", "keyflag", "primary_key", "pk", "key", "is_key"],
    "nullable": ["is_nullable", "nullable"],
    "notnull": ["notnull", "not_null", "mandatory"],
    "position": ["position", "ordinal", "ordinal_position", "column_id"],
    "module": ["module", "application_area", "app_area", "component"],
    "business_name": ["business_name", "business_column", "business_term"],
    "check_table": ["checktable", "check_table"],
    # FK extracts
    "fortable": ["fortable", "fk_table", "child_table", "from_table"],
    "forkey": ["forkey", "fk_column", "child_column", "from_column"],
    "checkfield": ["checkfield", "pk_column", "parent_column", "to_column", "referenced_column"],
    "reftable": ["checktable", "pk_table", "parent_table", "to_table", "referenced_table"],
    # T-codes
    "tcode": ["tcode", "t_code", "transaction", "transaction_code", "code", "screen"],
    "tcode_text": ["ttext", "transaction_text", "tcode_description", "description", "name", "text"],
    "program": ["pgmna", "program", "report"],
}

TRUTHY = {"x", "y", "yes", "true", "1", "pk", "key", "t"}


def _norm_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_") for c in df.columns]
    return df


def _pick(df: pd.DataFrame, key: str, exclude: set[str] | None = None) -> str | None:
    for a in ALIASES[key]:
        if a in df.columns and (not exclude or a not in exclude):
            return a
    return None


def _cell(row, col) -> str | None:
    if col is None:
        return None
    v = row.get(col)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s or None


def _int(v) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def read_tabular(data: bytes, ext: str) -> pd.DataFrame:
    if ext == "csv":
        for enc in ("utf-8-sig", "latin-1"):
            try:
                return pd.read_csv(io.BytesIO(data), dtype=str, encoding=enc, sep=None, engine="python")
            except UnicodeDecodeError:
                continue
        raise IngestionError("Could not decode CSV")
    if ext in ("xlsx", "xls"):
        return pd.read_excel(io.BytesIO(data), dtype=str)
    raise IngestionError(f"Not a tabular file: .{ext}")


def detect_kind(filename: str, data: bytes) -> str:
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext in ("yaml", "yml", "json"):
        try:
            obj = yaml.safe_load(data.decode("utf-8"))
            if isinstance(obj, dict) and ({"tables", "erp", "relationships", "transactions"} & set(obj)):
                return "package"
        except Exception:  # noqa: BLE001
            pass
        return "document"
    if ext == "sql":
        text = data.decode("utf-8", errors="ignore").upper()
        if "CREATE TABLE" in text or ("ALTER TABLE" in text and "FOREIGN KEY" in text):
            return "ddl"
        if "SELECT" in text and "JOIN" in text and "{{" not in text:
            return "sql_queries"
        return "document"
    if ext in ("csv", "xlsx", "xls"):
        df = _norm_cols(read_tabular(data, ext).head(5))
        cols = set(df.columns)
        if {"fortable", "forkey"} & cols or ({"pk_table", "fk_table"} <= cols) or ({"parent_table", "child_table"} <= cols):
            return "foreign_keys"
        if _pick(df, "table") and _pick(df, "column"):
            if "business_column" in cols or "target_column" in cols:
                return "mapping"
            return "inventory"
        if _pick(df, "tcode"):
            return "tcodes"
        raise IngestionError("Could not recognise the spreadsheet layout. Expected TABLE_NAME/COLUMN_NAME "
                             "(or SAP TABNAME/FIELDNAME), an FK extract, or a TCODE list.")
    return "document"


# ----------------------------------------------------------------------------- parsers
def parse_package(data: bytes) -> MetadataPackage:
    try:
        obj = yaml.safe_load(data.decode("utf-8"))
    except yaml.YAMLError as e:
        raise IngestionError(f"Invalid YAML/JSON: {e}") from e
    if not isinstance(obj, dict):
        raise IngestionError("Package must be a mapping at the top level.")
    try:
        return MetadataPackage.model_validate(obj)
    except Exception as e:  # pydantic.ValidationError
        raise IngestionError(f"Package validation failed: {e}") from e


def parse_inventory(df: pd.DataFrame, provenance: dict) -> MetadataPackage:
    df = _norm_cols(df)
    tcol, ccol = _pick(df, "table"), _pick(df, "column")
    if not tcol or not ccol:
        raise IngestionError("Inventory needs table and column name columns.")
    dcol = _pick(df, "description")
    tdcol = _pick(df, "table_description")
    typecol, lencol, deccol = _pick(df, "data_type"), _pick(df, "length"), _pick(df, "decimals")
    pkcol, nullcol, nncol = _pick(df, "is_pk"), _pick(df, "nullable"), _pick(df, "notnull")
    poscol, modcol, bncol, ctcol = _pick(df, "position"), _pick(df, "module"), _pick(df, "business_name"), _pick(df, "check_table")
    tables: dict[str, TableIn] = {}
    warnings: list[str] = []
    rows = df.to_dict("records")
    if poscol:
        rows.sort(key=lambda r: (str(r.get(tcol)), _int(r.get(poscol)) or 0))
    for r in rows:
        tname, cname = _cell(r, tcol), _cell(r, ccol)
        if not tname or not cname:
            continue
        if cname.startswith(".INCLU") or cname.startswith(".APPEND"):  # SAP include structures
            continue
        try:
            t = tables.get(tname.upper())
            if t is None:
                t = TableIn(name=tname, description=_cell(r, tdcol), module=_cell(r, modcol), **provenance)
                tables[tname.upper()] = t
            nullable = True
            if nullcol:
                nullable = (_cell(r, nullcol) or "y").lower() in TRUTHY
            elif nncol:
                nullable = (_cell(r, nncol) or "").lower() not in TRUTHY
            t.columns.append(ColumnIn(
                name=cname, description=_cell(r, dcol), data_type=_cell(r, typecol), length=_int(_cell(r, lencol)),
                decimals=_int(_cell(r, deccol)), is_pk=(_cell(r, pkcol) or "").lower() in TRUTHY,
                is_nullable=nullable, business_name=_cell(r, bncol), check_table=_cell(r, ctcol), **provenance))
        except ValueError as e:
            warnings.append(f"Row skipped ({tname}.{cname}): {e}")
    return MetadataPackage(tables=list(tables.values()), warnings=warnings)


def parse_foreign_keys(df: pd.DataFrame, provenance: dict) -> MetadataPackage:
    """DD05S-style (TABNAME, FIELDNAME, FORTABLE, FORKEY, CHECKTABLE, CHECKFIELD) or generic FK list."""
    df = _norm_cols(df)
    sap_style = "fortable" in df.columns and "forkey" in df.columns
    groups: dict[tuple, list[tuple[str, str]]] = defaultdict(list)
    warnings = []
    for r in df.to_dict("records"):
        if sap_style:
            child, ckey = _cell(r, "fortable"), _cell(r, "forkey")
            parent, pkey = _cell(r, "checktable"), _cell(r, "checkfield")
            fk_id = (_cell(r, "tabname"), _cell(r, "fieldname"), child, parent)
            if not child or child == "*" or not parent or not ckey or not pkey:
                continue  # constant-valued key parts are not joins
        else:
            child = _cell(r, _pick(df, "fortable"))
            ckey = _cell(r, _pick(df, "forkey"))
            parent = _cell(r, _pick(df, "reftable", exclude=set()))
            pkey = _cell(r, _pick(df, "checkfield"))
            fk_id = (_cell(r, "constraint_name") or f"{child}->{parent}", child, parent)
            if not (child and ckey and parent and pkey):
                warnings.append(f"Incomplete FK row skipped: {r}")
                continue
        groups[(fk_id, child, parent)].append((ckey, pkey))
    rels = []
    for (fk_id, child, parent), pairs in groups.items():
        if child.upper() == parent.upper():
            continue
        rels.append(RelationshipIn(
            from_table=child, to_table=parent, join_columns=[JoinPair(**{"from": a, "to": b}) for a, b in pairs],
            join_type="LEFT", cardinality="N:1", relationship_type="lookup",
            description=f"Foreign key {child} → check/parent table {parent}",
            evidence=f"FK definition {fk_id}", **provenance))
    return MetadataPackage(relationships=rels, warnings=warnings)


SQLGLOT_TYPE_MAP = {"VARCHAR": "VARCHAR", "NVARCHAR": "NVARCHAR", "CHAR": "CHAR"}


def parse_ddl(text: str, dialect: str | None, provenance: dict, fk_provenance: dict) -> MetadataPackage:
    tables: dict[str, TableIn] = {}
    rels: list[RelationshipIn] = []
    warnings: list[str] = []
    try:
        statements = sqlglot.parse(text, read=dialect, error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception as e:  # noqa: BLE001
        raise IngestionError(f"DDL could not be parsed: {e}") from e

    def add_fk(child, child_cols, parent, parent_cols, name):
        if len(child_cols) != len(parent_cols) or not child_cols:
            warnings.append(f"FK {name}: column count mismatch, skipped")
            return
        rels.append(RelationshipIn(
            from_table=child, to_table=parent,
            join_columns=[JoinPair(**{"from": a, "to": b}) for a, b in zip(child_cols, parent_cols)],
            join_type="LEFT", cardinality="N:1", relationship_type="lookup",
            description=f"Declared foreign key {name or ''} {child} → {parent}".strip(),
            evidence="Declared in uploaded DDL", **fk_provenance))

    for st in statements:
        if st is None:
            continue
        if isinstance(st, exp.Create) and (st.args.get("kind") or "").upper() == "TABLE":
            schema = st.this
            tname = schema.this.name if isinstance(schema, exp.Schema) else st.this.name
            table = TableIn(name=tname, **provenance)
            pk_cols: set[str] = set()
            if isinstance(schema, exp.Schema):
                for e in schema.expressions:
                    if isinstance(e, exp.ColumnDef):
                        kinds = [c.args.get("kind") for c in e.args.get("constraints", [])]
                        is_pk = any(isinstance(k, exp.PrimaryKeyColumnConstraint) for k in kinds)
                        not_null = any(isinstance(k, exp.NotNullColumnConstraint) and not k.args.get("allow_null")
                                       for k in kinds)
                        dt = e.args.get("kind")
                        length = None
                        if dt is not None and dt.expressions:
                            length = _int(dt.expressions[0].sql())
                        table.columns.append(ColumnIn(
                            name=e.name, data_type=dt.this.name if dt is not None else None, length=length,
                            is_pk=is_pk, is_nullable=not (not_null or is_pk), **provenance))
                        for k in kinds:
                            if isinstance(k, exp.Reference):
                                ref = k.this
                                parent = ref.this.name if isinstance(ref, exp.Schema) else ref.name
                                pcols = [c.name for c in ref.expressions] if isinstance(ref, exp.Schema) else [e.name]
                                add_fk(tname, [e.name], parent, pcols, f"{tname}.{e.name}")
                    elif isinstance(e, exp.PrimaryKey):
                        pk_cols |= {c.name.upper() for c in e.expressions}
                    elif isinstance(e, (exp.Constraint,)):
                        for sub in e.expressions:
                            if isinstance(sub, exp.PrimaryKey):
                                pk_cols |= {c.name.upper() for c in sub.expressions}
                            elif isinstance(sub, exp.ForeignKey):
                                _fk_from_node(sub, tname, add_fk, e.name)
                    elif isinstance(e, exp.ForeignKey):
                        _fk_from_node(e, tname, add_fk, None)
            for c in table.columns:
                if c.name.upper() in pk_cols:
                    c.is_pk, c.is_nullable = True, False
            tables[tname.upper()] = table
        elif isinstance(st, exp.Alter):
            tname = st.this.name
            for action in st.args.get("actions", []):
                for node in action.find_all(exp.ForeignKey):
                    _fk_from_node(node, tname, add_fk, None)
                for node in action.find_all(exp.PrimaryKey):
                    if tname.upper() in tables:
                        keys = {c.name.upper() for c in node.expressions}
                        for c in tables[tname.upper()].columns:
                            if c.name.upper() in keys:
                                c.is_pk, c.is_nullable = True, False
    if not tables and not rels:
        warnings.append("No CREATE TABLE or FOREIGN KEY statements recognised.")
    return MetadataPackage(tables=list(tables.values()), relationships=rels, warnings=warnings)


def _fk_from_node(node: exp.ForeignKey, child: str, add_fk, name) -> None:
    child_cols = [c.name for c in node.expressions]
    ref = node.args.get("reference")
    if ref is None:
        return
    target = ref.this
    if isinstance(target, exp.Schema):
        parent = target.this.name
        parent_cols = [c.name for c in target.expressions]
    else:
        parent, parent_cols = target.name, child_cols
    add_fk(child, child_cols, parent, parent_cols, name)


def parse_sql_joins(text: str, dialect: str | None, provenance: dict) -> MetadataPackage:
    """Extract equi-join conditions from existing SELECT queries. Joins are *candidates*, not facts."""
    found: dict[tuple, dict] = {}
    warnings = []
    try:
        statements = sqlglot.parse(text, read=dialect, error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception as e:  # noqa: BLE001
        raise IngestionError(f"SQL could not be parsed: {e}") from e
    for st in statements:
        if st is None:
            continue
        for select_node in st.find_all(exp.Select):
            alias_map: dict[str, str] = {}
            for t in select_node.find_all(exp.Table):
                if t.name:
                    alias_map[(t.alias or t.name).upper()] = t.name.upper()
            for join in select_node.args.get("joins", []) or []:
                on = join.args.get("on")
                if on is None:
                    continue
                side = (join.args.get("side") or "").upper()
                pairs: dict[tuple, list] = defaultdict(list)
                extra = []
                for eq in on.find_all(exp.EQ):
                    l, r = eq.left, eq.right
                    if isinstance(l, exp.Column) and isinstance(r, exp.Column) and l.table and r.table:
                        lt, rt = alias_map.get(l.table.upper()), alias_map.get(r.table.upper())
                        if lt and rt and lt != rt:
                            pairs[(lt, rt)].append((l.name.upper(), r.name.upper()))
                    else:
                        extra.append(eq.sql())
                for (lt, rt), cols in pairs.items():
                    key = (lt, rt, tuple(sorted(cols)))
                    if key not in found:
                        found[key] = {"count": 0, "side": side, "extra": extra}
                    found[key]["count"] += 1
    rels = []
    for (lt, rt, cols), info in found.items():
        rels.append(RelationshipIn(
            from_table=lt, to_table=rt, join_columns=[JoinPair(**{"from": a, "to": b}) for a, b in cols],
            join_type="LEFT" if info["side"] == "LEFT" else "INNER", cardinality="uncertain",
            relationship_type="reference",
            description=f"Join observed in existing SQL ({info['count']}×)",
            evidence=f"Parsed from uploaded SQL; other predicates on this join: {info['extra'][:3]}",
            **provenance))
    if not rels:
        warnings.append("No table-to-table equi-joins found.")
    return MetadataPackage(relationships=rels, warnings=warnings)


def parse_tcodes(df: pd.DataFrame, provenance: dict) -> MetadataPackage:
    df = _norm_cols(df)
    ccol = _pick(df, "tcode")
    tcol = _pick(df, "tcode_text", exclude={ccol} if ccol else None)
    pcol, mcol = _pick(df, "program"), _pick(df, "module")
    txs = []
    for r in df.to_dict("records"):
        code = _cell(r, ccol)
        if not code:
            continue
        comps = {"program": _cell(r, pcol)} if pcol and _cell(r, pcol) else None
        txs.append(TransactionIn(code=code.upper(), name=_cell(r, tcol) or code, module=_cell(r, mcol),
                                 app_components=comps, **provenance))
    return MetadataPackage(transactions=txs)


def parse_mapping_workbook(df: pd.DataFrame, provenance: dict) -> MetadataPackage:
    df = _norm_cols(df)
    bcol = "business_column" if "business_column" in df.columns else _pick(df, "business_name")
    tcol, ccol = _pick(df, "table"), _pick(df, "column")
    terms = []
    for r in df.to_dict("records"):
        b, t, c = _cell(r, bcol), _cell(r, tcol), _cell(r, ccol)
        if b and t and c:
            terms.append(TermIn(term=b, table=t, column=c, definition=_cell(r, "business_definition"),
                                transformation=_cell(r, "transformation"), **provenance))
    return MetadataPackage(terms=terms)


def extract_document_text(data: bytes, ext: str) -> str:
    if ext == "pdf":
        import fitz  # PyMuPDF
        with fitz.open(stream=data, filetype="pdf") as doc:
            return "\n".join(page.get_text() for page in doc)
    if ext == "docx":
        import docx
        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)
    if ext in ("xlsx", "xls", "csv"):
        return read_tabular(data, ext).to_csv(index=False)
    return data.decode("utf-8", errors="ignore")


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text)
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 1 <= size:
            buf = f"{buf}\n{p}" if buf else p
            continue
        if buf:
            chunks.append(buf)
        while len(p) > size:
            chunks.append(p[:size])
            p = p[size - overlap:]
        buf = p
    if buf:
        chunks.append(buf)
    return chunks


# ----------------------------------------------------------------------------- orchestration
def default_provenance(kind: str, trusted: bool) -> dict:
    if kind in ("ddl", "foreign_keys"):
        return {"source_type": SourceType.DDL_CONSTRAINT.value, "confidence": 0.95,
                "validation_status": ValidationStatus.VERIFIED.value}
    if kind == "sql_queries":
        return {"source_type": SourceType.EXISTING_SQL.value, "confidence": 0.7,
                "validation_status": ValidationStatus.PENDING.value}
    if trusted:
        return {"source_type": SourceType.TRUSTED_DOCUMENTATION.value, "confidence": 0.9,
                "validation_status": ValidationStatus.VERIFIED.value}
    return {"source_type": SourceType.TRUSTED_DOCUMENTATION.value, "confidence": 0.75,
            "validation_status": ValidationStatus.REQUIRES_VALIDATION.value}


def parse_upload(filename: str, data: bytes, kind: str = KIND_AUTO, dialect: str | None = None,
                 trusted: bool = False) -> ParseOutcome:
    settings = get_settings()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise IngestionError(f"File exceeds {settings.max_upload_mb} MB limit.")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in SUPPORTED_EXTENSIONS:
        raise IngestionError(f"Unsupported file type .{ext}. Supported: {sorted(SUPPORTED_EXTENSIONS)}")
    if kind == KIND_AUTO:
        kind = detect_kind(filename, data)
    prov = default_provenance(kind, trusted)
    if kind == "package":
        pkg = parse_package(data)
        if not pkg.defaults:
            pkg.defaults = prov
        return ParseOutcome(kind, package=pkg)
    if kind == "ddl":
        # Columns from DDL are verified structure; FKs carry ddl_constraint provenance.
        return ParseOutcome(kind, package=parse_ddl(data.decode("utf-8", "ignore"), dialect, prov, prov))
    if kind == "sql_queries":
        text = data.decode("utf-8", "ignore")
        return ParseOutcome(kind, package=parse_sql_joins(text, dialect, prov), document_text=text)
    if kind in ("inventory", "foreign_keys", "tcodes", "mapping"):
        df = read_tabular(data, ext)
        if kind == "inventory":
            inv_prov = {"source_type": SourceType.DDL_CONSTRAINT.value if trusted else SourceType.TRUSTED_DOCUMENTATION.value,
                        "confidence": 0.95 if trusted else 0.8,
                        "validation_status": ValidationStatus.VERIFIED.value if trusted else ValidationStatus.REQUIRES_VALIDATION.value}
            return ParseOutcome(kind, package=parse_inventory(df, inv_prov))
        if kind == "foreign_keys":
            return ParseOutcome(kind, package=parse_foreign_keys(df, prov))
        if kind == "tcodes":
            return ParseOutcome(kind, package=parse_tcodes(df, prov))
        return ParseOutcome(kind, package=parse_mapping_workbook(df, prov))
    if kind == "document":
        return ParseOutcome(kind, document_text=extract_document_text(data, ext))
    raise IngestionError(f"Unknown ingestion kind {kind}")


def commit_upload(session: Session, outcome: ParseOutcome, filename: str, data: bytes, erp_code: str | None,
                  erp_id: int | None, user: str, trusted: bool = False) -> tuple[ImportResult | None, int | None]:
    """Persist parsed metadata (idempotent upsert) and index any text for retrieval."""
    result = None
    if outcome.package is not None and (outcome.package.erp or erp_code):
        result = import_package(session, outcome.package, user, erp_code=erp_code)
        if outcome.package.erp is not None:
            from erpdm.services.metadata import get_erp
            erp_id = get_erp(session, outcome.package.erp.code).id
    doc_id = None
    if outcome.document_text:
        digest = hashlib.sha256(data).hexdigest()
        existing = session.scalar(select(Document).where(Document.content_hash == digest,
                                                         Document.erp_id == erp_id))
        if existing is None:
            ext = filename.rsplit(".", 1)[-1].lower()
            doc = Document(erp_id=erp_id, filename=filename[:255], file_type=ext, doc_kind=outcome.kind,
                           content_hash=digest, is_trusted=trusted, uploaded_by=user,
                           summary={"chars": len(outcome.document_text)})
            session.add(doc)
            session.flush()
            for i, chunk in enumerate(chunk_text(outcome.document_text)):
                session.add(DocumentChunk(document_id=doc.id, erp_id=erp_id, chunk_index=i, content=chunk))
            doc_id = doc.id
            audit(session, user, "document.index", "document", doc.id, {"filename": filename})
        else:
            doc_id = existing.id
    return result, doc_id


def package_to_yaml(pkg: MetadataPackage) -> str:
    return yaml.safe_dump(json.loads(pkg.model_dump_json(by_alias=True, exclude_none=True)), sort_keys=False)
