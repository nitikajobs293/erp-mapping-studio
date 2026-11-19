"""Data model for mapping projects.

A project is a single JSON-serialisable document so it can be saved, reopened,
versioned and exported without any database. Collections are stored as lists of
plain dicts (records) so they round-trip cleanly through pandas DataFrames and
Streamlit data editors.
"""
from __future__ import annotations

import copy
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Record schemas (column order used everywhere: editors, exports, docs)
# ---------------------------------------------------------------------------
SOURCE_METADATA_FIELDS = [
    "erp_name", "schema_name", "table_name", "table_description", "column_name",
    "data_type", "column_description", "is_primary_key", "is_foreign_key",
    "references", "business_description",
]

TARGET_FIELDS = [
    "target_column", "target_data_type", "business_definition", "mandatory",
    "expected_transformation", "business_rules", "source_hint",
]

TABLE_MAPPING_FIELDS = [
    "source_erp", "source_schema", "source_table", "alias", "role", "table_description",
    "table_purpose", "primary_key", "foreign_keys", "join_conditions", "include", "relevance",
]

JOIN_FIELDS = [
    "join_id", "source_erp", "left_table", "left_columns", "right_table", "right_columns",
    "join_type", "cardinality", "status", "evidence", "confidence", "fanout_handling",
    "reasoning", "include",
]

MAPPING_FIELDS = [
    "target_column", "target_data_type", "mandatory", "source_erp", "source_schema",
    "source_table", "source_column", "source_data_type", "transformation_type",
    "transformation_logic", "join_dependency", "business_rule", "status", "evidence",
    "confidence", "reasoning",
]

OPEN_QUESTION_FIELDS = ["id", "category", "item", "detail", "severity", "owner", "resolution_status"]

FANOUT_PICK_LATEST = "Pick latest row per join key"
FANOUT_ALLOW = "Allow (changes grain)"
FANOUT_NOT_APPLICABLE = "Not applicable"
FANOUT_OPTIONS = [FANOUT_NOT_APPLICABLE, FANOUT_PICK_LATEST, FANOUT_ALLOW]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def default_config() -> dict[str, Any]:
    return {
        "project_name": "New Mapping Project",
        "source_erps": ["SAP S/4HANA"],
        "source_module": "Delivery",
        "business_process": "",
        "source_database": "",
        "source_schema": "",
        "target_database": "",
        "target_schema": "",
        "target_object": "",
        "target_object_type": "VIEW",
        "mapping_type": "Single ERP",
        "history_requirement": "Current records only",
        "expected_grain": "",
        "load_type": "Full load",
        "source_filters": "",          # free SQL predicate(s), one per line, applied to the driving table
        "sap_client": "",              # optional MANDT filter for SAP sources
        "strip_leading_zeros_default": False,
        "author": "",
    }


@dataclass
class MappingProject:
    project_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    config: dict[str, Any] = field(default_factory=default_config)
    business_context: str = ""
    source_metadata: list[dict[str, Any]] = field(default_factory=list)
    target_columns: list[dict[str, Any]] = field(default_factory=list)
    table_mapping: list[dict[str, Any]] = field(default_factory=list)
    joins: list[dict[str, Any]] = field(default_factory=list)
    column_mappings: list[dict[str, Any]] = field(default_factory=list)
    grain: dict[str, dict[str, Any]] = field(default_factory=dict)   # keyed by ERP name
    open_questions: list[dict[str, Any]] = field(default_factory=list)
    sql: dict[str, str] = field(default_factory=dict)
    validation_issues: list[dict[str, Any]] = field(default_factory=list)
    ai_log: list[dict[str, Any]] = field(default_factory=list)
    versions: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ helpers
    def touch(self) -> None:
        self.updated_at = _now()

    def df(self, attr: str, fields: list[str]) -> pd.DataFrame:
        records = getattr(self, attr) or []
        frame = pd.DataFrame(records)
        for f in fields:
            if f not in frame.columns:
                frame[f] = None
        return frame[fields + [c for c in frame.columns if c not in fields]]

    def set_records(self, attr: str, frame: pd.DataFrame) -> None:
        clean = frame.astype(object).where(pd.notna(frame), None)
        setattr(self, attr, clean.to_dict(orient="records"))
        self.touch()

    # ---------------------------------------------------------------- versions
    _VERSIONED = ("config", "business_context", "target_columns", "table_mapping", "joins",
                  "column_mappings", "grain", "open_questions", "sql")

    def save_version(self, note: str = "") -> dict[str, Any]:
        snapshot = {k: copy.deepcopy(getattr(self, k)) for k in self._VERSIONED}
        version = {
            "version": len(self.versions) + 1,
            "saved_at": _now(),
            "note": note or "Snapshot",
            "mapped": sum(1 for m in self.column_mappings if m.get("source_column")),
            "snapshot": snapshot,
        }
        self.versions.append(version)
        self.touch()
        return version

    def restore_version(self, version_number: int) -> None:
        for v in self.versions:
            if v["version"] == version_number:
                for k, val in v["snapshot"].items():
                    setattr(self, k, copy.deepcopy(val))
                self.touch()
                return
        raise ValueError(f"Version {version_number} not found")

    # ----------------------------------------------------------- serialisation
    def to_dict(self, include_versions: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if not include_versions:
            data["versions"] = [{k: v for k, v in ver.items() if k != "snapshot"} for ver in self.versions]
        return data

    def to_json(self, include_versions: bool = True) -> str:
        return json.dumps(self.to_dict(include_versions), indent=2, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MappingProject":
        known = {f for f in cls.__dataclass_fields__}
        proj = cls(**{k: v for k, v in data.items() if k in known})
        merged = default_config()
        merged.update(proj.config or {})
        proj.config = merged
        return proj

    @classmethod
    def from_json(cls, text: str) -> "MappingProject":
        return cls.from_dict(json.loads(text))

    # ------------------------------------------------------------ convenience
    @property
    def erps(self) -> list[str]:
        erps = list(self.config.get("source_erps") or [])
        for row in self.source_metadata:
            e = row.get("erp_name")
            if e and e not in erps:
                erps.append(e)
        return erps

    def metadata_for(self, erp: str) -> pd.DataFrame:
        frame = self.df("source_metadata", SOURCE_METADATA_FIELDS)
        return frame[frame["erp_name"] == erp] if not frame.empty else frame
