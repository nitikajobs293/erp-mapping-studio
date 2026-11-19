"""Pydantic DTOs shared by services, ingestion and UI."""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

_IDENT = re.compile(r"^[A-Za-z0-9_/$#.\-]{1,128}$")


def _ident(v: str) -> str:
    v = (v or "").strip()
    if not _IDENT.match(v):
        raise ValueError(f"Invalid identifier: {v!r}")
    return v


# ----------------------------------------------------------------------------- metadata package
class ColumnIn(BaseModel):
    name: str
    description: str | None = None
    data_type: str | None = None
    length: int | None = None
    decimals: int | None = None
    is_pk: bool = False
    is_nullable: bool = True
    business_name: str | None = None
    business_definition: str | None = None
    check_table: str | None = None
    source_type: str | None = None
    confidence: float | None = None
    validation_status: str | None = None

    _v = field_validator("name")(lambda cls, v: _ident(v))


class TableIn(BaseModel):
    name: str
    description: str | None = None
    module: str | None = None
    table_type: str = "table"
    table_class: str = "unknown"
    business_entity: str | None = None
    change_tracking: dict[str, Any] | None = None
    delete_strategy: str | None = None
    soft_delete_rule: str | None = None
    default_filter: str | None = None
    notes: str | None = None
    columns: list[ColumnIn] = Field(default_factory=list)
    source_type: str | None = None
    confidence: float | None = None
    validation_status: str | None = None
    source_reference: str | None = None

    _v = field_validator("name")(lambda cls, v: _ident(v))


class JoinPair(BaseModel):
    from_: str = Field(alias="from")
    to: str
    model_config = {"populate_by_name": True}


class RelationshipIn(BaseModel):
    from_table: str
    to_table: str
    join_columns: list[JoinPair]
    extra_condition: str | None = None
    join_type: str = "LEFT"
    cardinality: str = "1:N"
    relationship_type: str = "reference"
    is_mandatory: bool = False
    description: str | None = None
    business_purpose: str | None = None
    evidence: str | None = None
    source_type: str | None = None
    confidence: float | None = None
    validation_status: str | None = None

    @field_validator("join_type")
    @classmethod
    def _jt(cls, v: str) -> str:
        v = v.upper()
        if v not in ("INNER", "LEFT"):
            raise ValueError("join_type must be INNER or LEFT")
        return v


class TransactionIn(BaseModel):
    code: str
    name: str
    module: str | None = None
    process: str | None = None
    transaction_type: str = "tcode"
    description: str | None = None
    business_purpose: str | None = None
    business_logic: str | None = None
    business_object: str | None = None
    app_components: dict[str, Any] | None = None
    root_table: str | None = None
    default_columns: list[str] | None = None
    tables: list[dict[str, Any]] = Field(default_factory=list)  # {"table": "EQUI", "role": "primary"}
    source_type: str | None = None
    confidence: float | None = None
    validation_status: str | None = None


class TermIn(BaseModel):
    term: str
    synonyms: list[str] = Field(default_factory=list)
    definition: str | None = None
    domain: str | None = None
    table: str | None = None
    column: str | None = None
    transformation: str | None = None
    lookup_logic: str | None = None
    source_type: str | None = None
    confidence: float | None = None
    validation_status: str | None = None


class UseCaseIn(BaseModel):
    name: str
    domain: str | None = None
    description: str | None = None
    keywords: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    optional_tables: list[str] = Field(default_factory=list)
    root_table: str | None = None
    grain: str | None = None
    process: str | None = None


class ProcessIn(BaseModel):
    name: str
    domain: str | None = None
    module: str | None = None
    sub_process: str | None = None
    description: str | None = None
    flow_steps: list[str] = Field(default_factory=list)


class ModuleIn(BaseModel):
    code: str
    name: str
    description: str | None = None


class ERPSystemIn(BaseModel):
    code: str
    name: str
    vendor: str | None = None
    version_label: str | None = None
    category: str = "ERP"
    description: str | None = None
    landscape_notes: str | None = None
    extraction_method: str | None = None
    default_language_key: str | None = None
    has_client_column: bool = False
    client_column: str | None = None

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        v = v.strip().upper().replace(" ", "_")
        if not re.match(r"^[A-Z0-9_\-]{2,40}$", v):
            raise ValueError("ERP code must be 2-40 chars of A-Z, 0-9, _ or -")
        return v


class MetadataPackage(BaseModel):
    """Canonical onboarding format. Every parser produces one of these."""
    erp: ERPSystemIn | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)  # default provenance for the package
    modules: list[ModuleIn] = Field(default_factory=list)
    processes: list[ProcessIn] = Field(default_factory=list)
    tables: list[TableIn] = Field(default_factory=list)
    relationships: list[RelationshipIn] = Field(default_factory=list)
    transactions: list[TransactionIn] = Field(default_factory=list)
    terms: list[TermIn] = Field(default_factory=list)
    use_cases: list[UseCaseIn] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {
            "modules": len(self.modules), "processes": len(self.processes), "tables": len(self.tables),
            "columns": sum(len(t.columns) for t in self.tables),
            "relationships": len(self.relationships), "transactions": len(self.transactions),
            "terms": len(self.terms), "use_cases": len(self.use_cases),
        }


# ----------------------------------------------------------------------------- engine outputs
class JoinEdge(BaseModel):
    relationship_id: int | None
    left_table: str
    right_table: str
    left_alias: str
    right_alias: str
    join_type: str
    condition_sql: str
    cardinality: str
    relationship_type: str
    description: str | None = None
    business_purpose: str | None = None
    source_type: str
    confidence: float
    validation_status: str
    is_mandatory: bool = False
    reversed: bool = False
    fans_out: bool = False
    uncovered_keys: list[str] = Field(default_factory=list)
    join_columns: list[dict] = Field(default_factory=list)  # [{"left": col, "right": col}]


class JoinPlan(BaseModel):
    erp_code: str
    root_table: str
    tables: list[str]
    aliases: dict[str, str]
    edges: list[JoinEdge]
    unreachable: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def min_confidence(self) -> float:
        return min((e.confidence for e in self.edges), default=1.0)


class SelectedColumn(BaseModel):
    table: str
    column: str
    alias: str | None = None
    business_name: str | None = None


class GeneratedSQL(BaseModel):
    dialect: str
    sql: str
    join_explanations: list[str]
    filter_explanations: list[str]
    duplicate_handling: list[str]
    null_handling: list[str]
    performance_notes: list[str]
    validation_queries: dict[str, str]
    column_mapping: list[dict[str, Any]]
    warnings: list[str] = Field(default_factory=list)
