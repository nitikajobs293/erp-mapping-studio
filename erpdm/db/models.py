"""SQLAlchemy ORM models for the ERP metadata repository."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from erpdm.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProvenanceMixin:
    """Every knowledge row says where it came from and whether it has been validated."""
    source_type: Mapped[str] = mapped_column(String(40), default="standard_erp_knowledge", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    validation_status: Mapped[str] = mapped_column(String(30), default="requires_validation", index=True)
    source_reference: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


# ----------------------------------------------------------------------------- users / audit
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="modeller")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    username: Mapped[str | None] = mapped_column(String(80), index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(80))
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class MetadataVersion(Base):
    __tablename__ = "metadata_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(60), index=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[int] = mapped_column(Integer)
    change_type: Mapped[str] = mapped_column(String(30))  # create|update|validate|reject|delete
    snapshot: Mapped[dict] = mapped_column(JSON)
    changed_by: Mapped[str | None] = mapped_column(String(80))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ----------------------------------------------------------------------------- ERP structure
class ERPSystem(Base, AuditMixin):
    __tablename__ = "erp_systems"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    vendor: Mapped[str | None] = mapped_column(String(80))
    version_label: Mapped[str | None] = mapped_column(String(80))
    category: Mapped[str] = mapped_column(String(30), default="ERP")  # ERP|WMS|TMS|SCM
    description: Mapped[str | None] = mapped_column(Text)
    landscape_notes: Mapped[str | None] = mapped_column(Text)
    extraction_method: Mapped[str | None] = mapped_column(String(200))
    default_language_key: Mapped[str | None] = mapped_column(String(5))
    has_client_column: Mapped[bool] = mapped_column(Boolean, default=False)  # e.g. SAP MANDT
    client_column: Mapped[str | None] = mapped_column(String(30))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    modules: Mapped[list["ERPModule"]] = relationship(back_populates="erp", cascade="all, delete-orphan")
    tables: Mapped[list["ERPTable"]] = relationship(back_populates="erp", cascade="all, delete-orphan")


class ERPModule(Base):
    __tablename__ = "erp_modules"
    __table_args__ = (UniqueConstraint("erp_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(ForeignKey("erp_systems.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    erp: Mapped[ERPSystem] = relationship(back_populates="modules")


class BusinessProcess(Base):
    __tablename__ = "business_processes"
    __table_args__ = (UniqueConstraint("erp_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(ForeignKey("erp_systems.id", ondelete="CASCADE"), index=True)
    module_id: Mapped[int | None] = mapped_column(ForeignKey("erp_modules.id", ondelete="SET NULL"))
    domain: Mapped[str | None] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(160))
    sub_process: Mapped[str | None] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    flow_steps: Mapped[list | None] = mapped_column(JSON)  # ordered business steps
    module: Mapped[ERPModule | None] = relationship()


class ERPTable(Base, ProvenanceMixin, AuditMixin):
    __tablename__ = "erp_tables"
    __table_args__ = (UniqueConstraint("erp_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(ForeignKey("erp_systems.id", ondelete="CASCADE"), index=True)
    module_id: Mapped[int | None] = mapped_column(ForeignKey("erp_modules.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    table_type: Mapped[str] = mapped_column(String(20), default="table")
    table_class: Mapped[str] = mapped_column(String(20), default="unknown")
    business_entity: Mapped[str | None] = mapped_column(String(120))
    change_tracking: Mapped[dict | None] = mapped_column(JSON)
    delete_strategy: Mapped[str | None] = mapped_column(Text)
    soft_delete_rule: Mapped[str | None] = mapped_column(Text)
    default_filter: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    erp: Mapped[ERPSystem] = relationship(back_populates="tables")
    module: Mapped[ERPModule | None] = relationship()
    columns: Mapped[list["ERPColumn"]] = relationship(
        back_populates="table", cascade="all, delete-orphan", order_by="ERPColumn.ordinal")

    @property
    def primary_key(self) -> list[str]:
        return [c.name for c in self.columns if c.is_pk]


class ERPColumn(Base, ProvenanceMixin):
    __tablename__ = "erp_columns"
    __table_args__ = (UniqueConstraint("table_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    table_id: Mapped[int] = mapped_column(ForeignKey("erp_tables.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    data_type: Mapped[str | None] = mapped_column(String(40))
    length: Mapped[int | None] = mapped_column(Integer)
    decimals: Mapped[int | None] = mapped_column(Integer)
    is_pk: Mapped[bool] = mapped_column(Boolean, default=False)
    is_nullable: Mapped[bool] = mapped_column(Boolean, default=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    business_name: Mapped[str | None] = mapped_column(String(160))
    business_definition: Mapped[str | None] = mapped_column(Text)
    check_table: Mapped[str | None] = mapped_column(String(128))
    table: Mapped[ERPTable] = relationship(back_populates="columns")


class Relationship(Base, ProvenanceMixin, AuditMixin):
    __tablename__ = "relationships"
    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(ForeignKey("erp_systems.id", ondelete="CASCADE"), index=True)
    from_table_id: Mapped[int] = mapped_column(ForeignKey("erp_tables.id", ondelete="CASCADE"), index=True)
    to_table_id: Mapped[int] = mapped_column(ForeignKey("erp_tables.id", ondelete="CASCADE"), index=True)
    join_columns: Mapped[list] = mapped_column(JSON)  # [{"from": "EQUNR", "to": "EQUNR"}]
    extra_condition: Mapped[str | None] = mapped_column(Text)  # uses {from}/{to}/{lang} placeholders
    join_type: Mapped[str] = mapped_column(String(10), default="LEFT")
    cardinality: Mapped[str] = mapped_column(String(10), default="1:N")
    relationship_type: Mapped[str] = mapped_column(String(30), default="reference")
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text)
    business_purpose: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)
    validated_by: Mapped[str | None] = mapped_column(String(80))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    from_table: Mapped[ERPTable] = relationship(foreign_keys=[from_table_id])
    to_table: Mapped[ERPTable] = relationship(foreign_keys=[to_table_id])


class Transaction(Base, ProvenanceMixin, AuditMixin):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("erp_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(ForeignKey("erp_systems.id", ondelete="CASCADE"), index=True)
    module_id: Mapped[int | None] = mapped_column(ForeignKey("erp_modules.id", ondelete="SET NULL"))
    process_id: Mapped[int | None] = mapped_column(ForeignKey("business_processes.id", ondelete="SET NULL"))
    code: Mapped[str] = mapped_column(String(60), index=True)
    name: Mapped[str] = mapped_column(String(200))
    transaction_type: Mapped[str] = mapped_column(String(20), default="tcode")
    description: Mapped[str | None] = mapped_column(Text)
    business_purpose: Mapped[str | None] = mapped_column(Text)
    business_logic: Mapped[str | None] = mapped_column(Text)
    business_object: Mapped[str | None] = mapped_column(String(120))
    app_components: Mapped[dict | None] = mapped_column(JSON)
    root_table: Mapped[str | None] = mapped_column(String(128))
    default_columns: Mapped[list | None] = mapped_column(JSON)  # ["EQUI.EQUNR", ...]
    module: Mapped[ERPModule | None] = relationship()
    process: Mapped[BusinessProcess | None] = relationship()
    table_links: Mapped[list["TransactionTable"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan")


class TransactionTable(Base):
    __tablename__ = "transaction_tables"
    __table_args__ = (UniqueConstraint("transaction_id", "table_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"), index=True)
    table_id: Mapped[int] = mapped_column(ForeignKey("erp_tables.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(30), default="secondary")
    notes: Mapped[str | None] = mapped_column(Text)
    transaction: Mapped[Transaction] = relationship(back_populates="table_links")
    table: Mapped[ERPTable] = relationship()


class BusinessTerm(Base, ProvenanceMixin, AuditMixin):
    __tablename__ = "business_terms"
    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(ForeignKey("erp_systems.id", ondelete="CASCADE"), index=True)
    term: Mapped[str] = mapped_column(String(160), index=True)
    synonyms: Mapped[list | None] = mapped_column(JSON)
    definition: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String(80))
    table_name: Mapped[str | None] = mapped_column(String(128))
    column_name: Mapped[str | None] = mapped_column(String(128))
    transformation: Mapped[str | None] = mapped_column(Text)
    lookup_logic: Mapped[str | None] = mapped_column(Text)


class UseCase(Base, ProvenanceMixin, AuditMixin):
    __tablename__ = "use_cases"
    __table_args__ = (UniqueConstraint("erp_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int] = mapped_column(ForeignKey("erp_systems.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[list | None] = mapped_column(JSON)
    tables: Mapped[list | None] = mapped_column(JSON)
    optional_tables: Mapped[list | None] = mapped_column(JSON)
    root_table: Mapped[str | None] = mapped_column(String(128))
    grain: Mapped[str | None] = mapped_column(Text)
    process_id: Mapped[int | None] = mapped_column(ForeignKey("business_processes.id", ondelete="SET NULL"))


# ----------------------------------------------------------------------------- documents / search
class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int | None] = mapped_column(ForeignKey("erp_systems.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(20))
    doc_kind: Mapped[str] = mapped_column(String(40), default="documentation")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    is_trusted: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="indexed")
    summary: Mapped[dict | None] = mapped_column(JSON)
    uploaded_by: Mapped[str | None] = mapped_column(String(80))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    chunks: Mapped[list["DocumentChunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    erp_id: Mapped[int | None] = mapped_column(Integer, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    document: Mapped[Document] = relationship(back_populates="chunks")


class SearchIndex(Base):
    """Denormalised searchable projection of every metadata entity."""
    __tablename__ = "search_index"
    __table_args__ = (UniqueConstraint("entity_type", "entity_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    erp_id: Mapped[int | None] = mapped_column(Integer, index=True)
    entity_type: Mapped[str] = mapped_column(String(30), index=True)
    entity_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    module: Mapped[str | None] = mapped_column(String(60))
    validation_status: Mapped[str | None] = mapped_column(String(30))


# ----------------------------------------------------------------------------- outputs
class Mapping(Base, AuditMixin):
    __tablename__ = "mappings"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    erp_id: Mapped[int] = mapped_column(ForeignKey("erp_systems.id", ondelete="CASCADE"), index=True)
    transaction_code: Mapped[str | None] = mapped_column(String(60))
    use_case: Mapped[str | None] = mapped_column(Text)
    target_database: Mapped[str | None] = mapped_column(String(120))
    target_schema: Mapped[str | None] = mapped_column(String(120))
    target_table: Mapped[str | None] = mapped_column(String(120))
    root_table: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|in_review|approved|rejected
    created_by: Mapped[str | None] = mapped_column(String(80))
    approved_by: Mapped[str | None] = mapped_column(String(80))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    join_plan: Mapped[dict | None] = mapped_column(JSON)
    rows: Mapped[list["MappingRow"]] = relationship(
        back_populates="mapping", cascade="all, delete-orphan", order_by="MappingRow.ordinal")


class MappingRow(Base):
    __tablename__ = "mapping_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("mappings.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    business_column: Mapped[str] = mapped_column(String(200))
    source_erp: Mapped[str | None] = mapped_column(String(60))
    source_module: Mapped[str | None] = mapped_column(String(60))
    source_table: Mapped[str | None] = mapped_column(String(128))
    source_column: Mapped[str | None] = mapped_column(String(128))
    source_data_type: Mapped[str | None] = mapped_column(String(60))
    target_database: Mapped[str | None] = mapped_column(String(120))
    target_schema: Mapped[str | None] = mapped_column(String(120))
    target_table: Mapped[str | None] = mapped_column(String(120))
    target_column: Mapped[str | None] = mapped_column(String(120))
    target_data_type: Mapped[str | None] = mapped_column(String(60))
    transformation: Mapped[str | None] = mapped_column(Text)
    data_type_conversion: Mapped[str | None] = mapped_column(Text)
    default_value: Mapped[str | None] = mapped_column(String(120))
    null_handling: Mapped[str | None] = mapped_column(Text)
    lookup_logic: Mapped[str | None] = mapped_column(Text)
    join_condition: Mapped[str | None] = mapped_column(Text)
    join_dependency: Mapped[str | None] = mapped_column(Text)
    filter_conditions: Mapped[str | None] = mapped_column(Text)
    business_definition: Mapped[str | None] = mapped_column(Text)
    business_logic: Mapped[str | None] = mapped_column(Text)
    data_quality_rules: Mapped[str | None] = mapped_column(Text)
    match_method: Mapped[str | None] = mapped_column(String(40))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source_type: Mapped[str | None] = mapped_column(String(40))
    validation_status: Mapped[str] = mapped_column(String(30), default="requires_validation")
    mapping: Mapped[Mapping] = relationship(back_populates="rows")


class GeneratedArtifact(Base):
    __tablename__ = "generated_artifacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)  # sql|dbt_project|dbt_model|document
    name: Mapped[str] = mapped_column(String(200))
    erp_id: Mapped[int | None] = mapped_column(Integer, index=True)
    content: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSON)
    created_by: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_rel_pair", Relationship.from_table_id, Relationship.to_table_id)
