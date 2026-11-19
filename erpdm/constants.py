"""Shared enumerations: provenance, validation, roles, permissions."""
from __future__ import annotations

from enum import Enum


class SourceType(str, Enum):
    USER_VERIFIED = "user_verified"
    DDL_CONSTRAINT = "ddl_constraint"
    TRUSTED_DOCUMENTATION = "trusted_documentation"
    EXISTING_SQL = "existing_sql"
    STANDARD_ERP_KNOWLEDGE = "standard_erp_knowledge"
    AI_INFERRED = "ai_inferred"
    UNVERIFIED_ASSUMPTION = "unverified_assumption"


SOURCE_LABELS = {
    SourceType.USER_VERIFIED: "Verified by user",
    SourceType.DDL_CONSTRAINT: "Database / DDIC constraint",
    SourceType.TRUSTED_DOCUMENTATION: "Trusted documentation",
    SourceType.EXISTING_SQL: "Parsed from existing SQL",
    SourceType.STANDARD_ERP_KNOWLEDGE: "Standard ERP knowledge",
    SourceType.AI_INFERRED: "AI-inferred suggestion",
    SourceType.UNVERIFIED_ASSUMPTION: "Unverified assumption",
}

# Sources that the generators may use without the user opting in.
TRUSTED_SOURCES = {
    SourceType.USER_VERIFIED.value,
    SourceType.DDL_CONSTRAINT.value,
    SourceType.TRUSTED_DOCUMENTATION.value,
    SourceType.STANDARD_ERP_KNOWLEDGE.value,
}


class ValidationStatus(str, Enum):
    VERIFIED = "verified"
    REQUIRES_VALIDATION = "requires_validation"
    PENDING = "pending"
    REJECTED = "rejected"


class Role(str, Enum):
    ADMIN = "admin"
    MODELLER = "modeller"
    VIEWER = "viewer"


class Permission(str, Enum):
    VIEW = "view"
    GENERATE = "generate"          # SQL / dbt / mapping generation
    EDIT_MAPPING = "edit_mapping"
    APPROVE_MAPPING = "approve_mapping"
    MANAGE_METADATA = "manage_metadata"
    VALIDATE_METADATA = "validate_metadata"
    MANAGE_USERS = "manage_users"
    USE_ASSISTANT = "use_assistant"


ROLE_PERMISSIONS: dict[str, set[str]] = {
    Role.ADMIN.value: {p.value for p in Permission},
    Role.MODELLER.value: {
        Permission.VIEW.value, Permission.GENERATE.value, Permission.EDIT_MAPPING.value,
        Permission.USE_ASSISTANT.value,
    },
    Role.VIEWER.value: {Permission.VIEW.value, Permission.USE_ASSISTANT.value},
}

RELATIONSHIP_TYPES = [
    "header_item", "master_detail", "master_transaction", "text", "lookup",
    "document_flow", "reference", "status", "one_to_one",
]
CARDINALITIES = ["1:1", "1:N", "N:1", "N:M", "uncertain"]
JOIN_TYPES = ["INNER", "LEFT"]
TABLE_CLASSES = ["master", "transaction", "text", "config", "document_flow", "status", "unknown"]
TABLE_TYPES = ["table", "view", "cds_view", "cluster", "pool", "unknown"]

SQL_DIALECTS = {
    "Snowflake": "snowflake",
    "SQL Server": "tsql",
    "Oracle": "oracle",
    "PostgreSQL": "postgres",
    "Amazon Redshift": "redshift",
    "Databricks SQL": "databricks",
}

BUSINESS_DOMAINS = [
    "Procurement", "Inventory Management", "Warehouse Management", "Order Management",
    "Sales and Distribution", "Logistics", "Shipment", "Delivery", "Transportation",
    "Manufacturing", "Finance", "Accounts Payable", "Accounts Receivable",
    "Equipment Management", "Plant Maintenance", "Material Management", "Customer Master",
    "Vendor Master", "Product Master", "Supply Chain Management",
]
