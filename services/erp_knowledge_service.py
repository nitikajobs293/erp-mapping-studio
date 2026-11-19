"""ERP knowledge base: loads YAML knowledge files, resolves inheritance, merges uploaded metadata
and user-defined ERPs, and offers lookup helpers used by every other service.

Adding a new ERP never requires code changes - drop a YAML file into knowledge_base/<folder>/ or
create one from the "ERP Metadata Repository" page (saved to data/custom_erps/).
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from config.settings import CONFIG_DIR, CUSTOM_ERP_DIR, KB_DIR, UPLOAD_DIR

PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_/$#]+)\.([A-Za-z0-9_/$#]+)\}")


# --------------------------------------------------------------------------- helpers
def deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into a copy of base (lists are replaced)."""
    out = copy.deepcopy(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        elif k == "relationships" and isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = out[k] + copy.deepcopy(v)  # child relationships extend the parent's
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_canonical_model() -> dict:
    return _read_yaml(CONFIG_DIR / "canonical_model.yaml")


# --------------------------------------------------------------------------- ERP wrapper
class ERPKnowledge:
    """Resolved knowledge for one ERP."""

    def __init__(self, doc: dict, source_file: str = ""):
        self.doc = doc
        self.source_file = source_file
        erp = doc.get("erp", {})
        self.id: str = erp["id"]
        self.name: str = erp.get("name", self.id)
        self.meta: dict = erp
        self.tables: dict = doc.get("tables", {}) or {}
        self.virtual_tables: dict = doc.get("virtual_tables", {}) or {}
        self.relationships: list = [self._norm_rel(r) for r in (doc.get("relationships", []) or [])]
        self.concepts: dict = doc.get("concepts", {}) or {}
        self.business_objects: dict = doc.get("business_objects", {}) or {}
        self.transactions: dict = doc.get("transactions", {}) or {}
        self.uploaded_fields: dict[str, dict] = {}  # TABLE -> {FIELD: {...}}

    @staticmethod
    def _norm_rel(rel: dict) -> dict:
        """YAML 1.1 parses the key `on` as boolean True - normalise it back."""
        rel = dict(rel)
        if True in rel:
            rel["on"] = rel.pop(True)
        rel["on"] = [list(x) for x in (rel.get("on") or [])]
        return rel

    # ---- basic lookups
    @property
    def family(self) -> str:
        return self.meta.get("family", "")

    @property
    def versions(self) -> list[str]:
        return self.meta.get("versions", []) or ["(unspecified)"]

    @property
    def storage(self) -> str:
        return self.meta.get("storage", "relational")

    def all_tables(self) -> dict:
        merged = dict(self.tables)
        for k, v in self.virtual_tables.items():
            merged[k] = {**v, "virtual": True}
        return merged

    def find_table(self, name: str) -> str | None:
        if not name:
            return None
        allt = self.all_tables()
        if name in allt:
            return name
        low = name.lower()
        for t in allt:
            if t.lower() == low:
                return t
        return None

    def table(self, name: str) -> dict:
        key = self.find_table(name)
        return self.all_tables().get(key, {}) if key else {}

    def field_info(self, table: str, field: str) -> dict:
        t = self.table(table)
        info = (t.get("fields") or {}).get(field)
        if info is None:
            for f, v in (t.get("fields") or {}).items():
                if f.lower() == str(field).lower():
                    info = v
                    break
        return info or {}

    def is_field_uploaded(self, table: str, field: str) -> bool:
        t = self.find_table(table) or table
        return str(field).upper() in {f.upper() for f in self.uploaded_fields.get(t, {})}

    def concept_spec(self, concept: str, business_object: str | None = None) -> dict | None:
        bo = self.business_objects.get(business_object or "", {}) or {}
        override = (bo.get("field_overrides") or {}).get(concept)
        base = self.concepts.get(concept)
        if override:
            spec = dict(base or {})
            spec.pop("expr", None)
            spec.update(override)
            spec.setdefault("confidence", (base or {}).get("confidence", "inferred"))
            spec["_override"] = True
            return spec
        return dict(base) if base else None

    def relationships_for(self, table: str) -> list[dict]:
        return [r for r in self.relationships if table in (r.get("from"), r.get("to"))]

    def summary(self) -> dict:
        return {
            "ERP": self.name,
            "Id": self.id,
            "Family": self.family,
            "Storage": self.storage,
            "Tables": len(self.tables),
            "Relationships": len(self.relationships),
            "Mapped concepts": len(self.concepts),
            "Business objects": len(self.business_objects),
            "T-codes / screens": len(self.transactions),
            "Uploaded tables": len(self.uploaded_fields),
        }


# --------------------------------------------------------------------------- repository
class KnowledgeRepository:
    """Loads every ERP knowledge file (built-in + custom) and uploaded metadata overlays."""

    def __init__(self, kb_dir: Path = KB_DIR, custom_dir: Path = CUSTOM_ERP_DIR, upload_dir: Path = UPLOAD_DIR):
        self.kb_dir = kb_dir
        self.custom_dir = custom_dir
        self.upload_dir = upload_dir
        self.canonical = load_canonical_model()
        self.erps: dict[str, ERPKnowledge] = {}
        self.load_errors: list[str] = []
        self.reload()

    # ---- loading
    def _raw_docs(self) -> dict[str, tuple[dict, str]]:
        docs: dict[str, tuple[dict, str]] = {}
        for folder in (self.kb_dir, self.custom_dir):
            for path in sorted(folder.rglob("*.yaml")) + sorted(folder.rglob("*.yml")):
                try:
                    doc = _read_yaml(path)
                    erp_id = doc.get("erp", {}).get("id")
                    if not erp_id:
                        self.load_errors.append(f"{path.name}: missing erp.id")
                        continue
                    if erp_id in docs and folder == self.custom_dir:
                        # custom file extends/overrides a built-in ERP
                        docs[erp_id] = (deep_merge(docs[erp_id][0], doc), str(path))
                    else:
                        docs[erp_id] = (doc, str(path))
                except Exception as exc:  # pragma: no cover - surfaced in UI
                    self.load_errors.append(f"{path.name}: {exc}")
        return docs

    def _resolve(self, erp_id: str, docs: dict, seen: tuple = ()) -> dict:
        doc, _ = docs[erp_id]
        parent = doc.get("erp", {}).get("inherits")
        if parent and parent in docs and parent not in seen:
            base = self._resolve(parent, docs, seen + (erp_id,))
            base = copy.deepcopy(base)
            base.get("erp", {}).pop("abstract", None)
            merged = deep_merge(base, doc)
            merged["erp"]["id"] = erp_id
            return merged
        return doc

    def reload(self) -> None:
        self.load_errors = []
        self.canonical = load_canonical_model()
        docs = self._raw_docs()
        self.erps = {}
        for erp_id, (doc, path) in docs.items():
            if doc.get("erp", {}).get("abstract"):
                continue
            resolved = self._resolve(erp_id, docs)
            self.erps[erp_id] = ERPKnowledge(resolved, path)
        for erp in self.erps.values():
            self._apply_uploaded_metadata(erp)

    # ---- uploaded metadata (data dictionaries / table inventories)
    def _metadata_path(self, erp_id: str) -> Path:
        return self.upload_dir / f"metadata_{erp_id}.json"

    def _apply_uploaded_metadata(self, erp: ERPKnowledge) -> None:
        path = self._metadata_path(erp.id)
        if not path.exists():
            return
        records = json.loads(path.read_text(encoding="utf-8"))
        for rec in records:
            table = str(rec.get("table", "")).strip()
            field = str(rec.get("field", "")).strip()
            if not table:
                continue
            tkey = erp.find_table(table) or table
            tdef = erp.tables.setdefault(tkey, {
                "description": rec.get("table_description") or "(from uploaded metadata)",
                "module": rec.get("module", ""),
                "primary_key": [],
                "confidence": "uploaded",
                "role": "Added from uploaded metadata",
                "fields": {},
            })
            if tdef.get("confidence") in ("inferred", "validate"):
                tdef["confidence"] = "uploaded"
            tdef.setdefault("fields", {})
            if field:
                existing = tdef["fields"].get(field) or {}
                tdef["fields"][field] = {
                    "description": rec.get("description") or existing.get("description", ""),
                    "type": rec.get("data_type") or existing.get("type", ""),
                    "source": "uploaded",
                }
                erp.uploaded_fields.setdefault(tkey, {})[field] = rec
                if str(rec.get("key", "")).strip().upper() in ("PK", "Y", "YES", "TRUE", "X", "1"):
                    pk = tdef.setdefault("primary_key", [])
                    if field not in pk:
                        pk.append(field)
                concept = str(rec.get("business_concept", "") or "").strip()
                if concept and concept in self.canonical.get("concepts", {}):
                    erp.concepts[concept] = {"table": tkey, "field": field, "confidence": "uploaded",
                                             "note": "Mapped in uploaded metadata"}
        # promote concept confidence when the referenced field is confirmed by the upload
        for c, spec in erp.concepts.items():
            if spec.get("table") and spec.get("field") and spec.get("confidence") in ("inferred", "validate"):
                if erp.is_field_uploaded(spec["table"], spec["field"]):
                    spec["confidence"] = "uploaded"
                    spec["note"] = (spec.get("note", "") + " | field confirmed by uploaded metadata").strip(" |")

    def save_uploaded_metadata(self, erp_id: str, df: pd.DataFrame, replace: bool = False) -> int:
        cols = {c.lower().strip().replace(" ", "_"): c for c in df.columns}
        aliases = {
            "table": ["table", "table_name", "tabname", "tablename", "object", "entity"],
            "field": ["field", "field_name", "column", "column_name", "fieldname", "attribute"],
            "description": ["description", "field_description", "ddtext", "scrtext_l", "comment", "label"],
            "data_type": ["data_type", "datatype", "type", "rollname", "datatype_name"],
            "key": ["key", "keyflag", "is_key", "primary_key", "pk"],
            "table_description": ["table_description", "table_desc", "table_text"],
            "module": ["module", "application", "package"],
            "business_concept": ["business_concept", "concept", "canonical_field"],
        }
        rename = {}
        for target, opts in aliases.items():
            for o in opts:
                if o in cols:
                    rename[cols[o]] = target
                    break
        norm = df.rename(columns=rename)
        if "table" not in norm.columns:
            raise ValueError("Uploaded metadata needs at least a 'table' column (and ideally 'field', 'description').")
        keep = [c for c in aliases if c in norm.columns]
        records = norm[keep].fillna("").astype(str).to_dict(orient="records")
        path = self._metadata_path(erp_id)
        existing = [] if replace or not path.exists() else json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(existing + records, indent=1), encoding="utf-8")
        self.reload()
        return len(records)

    def clear_uploaded_metadata(self, erp_id: str) -> None:
        p = self._metadata_path(erp_id)
        if p.exists():
            p.unlink()
        self.reload()

    # ---- custom ERPs
    def add_custom_erp(self, erp_id: str, name: str, family: str, storage: str, versions: list[str],
                       notes: str = "", inherits: str | None = None) -> Path:
        erp_id = re.sub(r"[^a-z0-9_]", "_", erp_id.lower()).strip("_")
        if not erp_id:
            raise ValueError("ERP id is required")
        doc = {
            "erp": {"id": erp_id, "name": name or erp_id, "family": family or name, "storage": storage,
                    "versions": versions or ["(unspecified)"], "notes": notes, "aliases": [name.lower()]},
            "tables": {}, "relationships": [], "concepts": {}, "business_objects": {}, "transactions": {},
        }
        if inherits:
            doc["erp"]["inherits"] = inherits
        path = self.custom_dir / f"{erp_id}.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
        self.reload()
        return path

    def save_custom_yaml(self, text: str) -> str:
        doc = yaml.safe_load(text)
        erp_id = doc.get("erp", {}).get("id")
        if not erp_id:
            raise ValueError("YAML must contain erp.id")
        (self.custom_dir / f"{erp_id}.yaml").write_text(text, encoding="utf-8")
        self.reload()
        return erp_id

    def add_custom_entry(self, erp_id: str, section: str, key: str, value: Any) -> None:
        """Append a table / relationship / concept / transaction to the custom overlay of an ERP."""
        path = self.custom_dir / f"{erp_id}.yaml"
        doc = _read_yaml(path) if path.exists() else {"erp": {"id": erp_id}}
        if section == "relationships":
            doc.setdefault("relationships", []).append(value)
        else:
            doc.setdefault(section, {})[key] = value
        path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
        self.reload()

    def export_erp_yaml(self, erp_id: str) -> str:
        return yaml.safe_dump(self.erps[erp_id].doc, sort_keys=False, allow_unicode=True)

    # ---- lookups across ERPs
    def erp_names(self) -> dict[str, str]:
        return {e.name: e.id for e in sorted(self.erps.values(), key=lambda e: e.name)}

    def get(self, erp_id: str) -> ERPKnowledge:
        return self.erps[erp_id]

    def detect_erp(self, text: str) -> str | None:
        low = f" {text.lower()} "
        best = None
        best_len = 0
        for e in self.erps.values():
            names = [e.name.lower(), e.id.replace("_", " ")] + [a.lower() for a in e.meta.get("aliases", [])]
            for n in names:
                if n and re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", low) and len(n) > best_len:
                    best, best_len = e.id, len(n)
        return best

    def concept_catalog(self) -> dict:
        return self.canonical.get("concepts", {})

    def business_object_catalog(self) -> dict:
        return self.canonical.get("business_objects", {})


_REPO: KnowledgeRepository | None = None


def get_repository(force_reload: bool = False) -> KnowledgeRepository:
    global _REPO
    if _REPO is None:
        _REPO = KnowledgeRepository()
    elif force_reload:
        _REPO.reload()
    return _REPO
