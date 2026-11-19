"""Loads seed / onboarding packages, including derived packages (`extends:`) with explicit overrides."""
from __future__ import annotations

import copy
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from erpdm.schemas import ERPSystemIn, MetadataPackage
from erpdm.services.metadata import ImportResult, get_erp, import_package, upsert_erp

PACKAGE_DIR = Path(__file__).parent / "packages"
LOAD_ORDER = ["sap_ecc.yaml", "sap_s4hana.yaml", "sap_nsap.yaml", "sap_ebs.yaml", "oracle_ebs.yaml"]


def _raw_packages(directory: Path) -> dict[str, dict]:
    out = {}
    for f in sorted(directory.glob("*.y*ml")):
        if f.name == "registry.yaml":
            continue
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        out[data["erp"]["code"]] = data
    return out


def resolve_package(raw: dict, catalogue: dict[str, dict]) -> dict:
    """Materialise `extends` with removals, overrides, confidence caps and forced status."""
    if "extends" not in raw:
        return raw
    parent = resolve_package(catalogue[raw["extends"]], catalogue)
    pkg = copy.deepcopy(parent)
    pkg["erp"] = raw["erp"]
    pkg["defaults"] = {**parent.get("defaults", {}), **raw.get("defaults", {})}
    removed = {t.upper() for t in raw.get("remove_tables", [])}

    def uses_removed(cols: list[str] | None) -> bool:
        return any(c.split(".")[0].upper() in removed for c in (cols or []))

    pkg["tables"] = [t for t in pkg.get("tables", []) if t["name"].upper() not in removed]
    pkg["relationships"] = [r for r in pkg.get("relationships", [])
                            if r["from_table"].upper() not in removed and r["to_table"].upper() not in removed]
    pkg["terms"] = [t for t in pkg.get("terms", []) if (t.get("table") or "").upper() not in removed]
    for tx in pkg.get("transactions", []):
        tx["tables"] = [l for l in tx.get("tables", []) if l["table"].upper() not in removed]
        if uses_removed(tx.get("default_columns")):
            tx["default_columns"] = [c for c in tx["default_columns"] if c.split(".")[0].upper() not in removed]
    for uc in pkg.get("use_cases", []):
        uc["tables"] = [t for t in uc.get("tables", []) if t.upper() not in removed]
        uc["optional_tables"] = [t for t in uc.get("optional_tables", []) if t.upper() not in removed]

    for col_name, ov in (raw.get("column_overrides") or {}).items():
        for t in pkg["tables"]:
            for c in t.get("columns", []):
                if c["name"].upper() == col_name.upper():
                    c.update(ov)
    for tname, ov in (raw.get("table_overrides") or {}).items():
        for t in pkg["tables"]:
            if t["name"].upper() == tname.upper():
                t.update(ov)

    def merge(key: str, ident) -> None:
        items = {ident(x): x for x in pkg.get(key, [])}
        for x in raw.get(key, []):
            items[ident(x)] = x
        pkg[key] = list(items.values())

    merge("tables", lambda t: t["name"].upper())
    merge("relationships", lambda r: (r["from_table"].upper(), r["to_table"].upper(),
                                      tuple(sorted((j["from"], j["to"]) for j in r["join_columns"]))))
    merge("transactions", lambda t: t["code"].upper())
    merge("use_cases", lambda u: u["name"])
    merge("terms", lambda t: t["term"].lower())
    merge("processes", lambda p: p["name"])
    merge("modules", lambda m: m["code"])

    cap = raw.get("confidence_cap")
    status = raw.get("force_status")
    if cap is not None or status:
        ref = raw.get("defaults", {}).get("source_reference")

        def apply(obj: dict) -> None:
            if cap is not None:
                obj["confidence"] = min(float(obj.get("confidence", cap)), cap)
            if status:
                obj["validation_status"] = status
            if ref and "source_reference" in obj:
                obj["source_reference"] = ref

        for t in pkg["tables"]:
            apply(t)
            for c in t.get("columns", []):
                apply(c)
        for key in ("relationships", "transactions", "terms"):
            for x in pkg.get(key, []):
                apply(x)
    for k in ("extends", "remove_tables", "column_overrides", "table_overrides", "confidence_cap", "force_status"):
        pkg.pop(k, None)
    return pkg


def load_seed(session: Session, user: str = "system", directory: Path | None = None,
              only_if_empty: bool = True) -> dict[str, ImportResult]:
    directory = directory or PACKAGE_DIR
    catalogue = _raw_packages(directory)
    results: dict[str, ImportResult] = {}
    order = [f for f in LOAD_ORDER if (directory / f).exists()]
    codes = []
    for fname in order:
        codes.append(yaml.safe_load((directory / fname).read_text(encoding="utf-8"))["erp"]["code"])
    codes += [c for c in catalogue if c not in codes]
    for code in codes:
        if only_if_empty and get_erp(session, code) is not None:
            continue
        pkg = MetadataPackage.model_validate(resolve_package(catalogue[code], catalogue))
        results[code] = import_package(session, pkg, user)
    registry = directory / "registry.yaml"
    if registry.exists():
        for s in yaml.safe_load(registry.read_text(encoding="utf-8")).get("systems", []):
            if get_erp(session, s["code"]) is None:
                s.setdefault("landscape_notes", "Registered without metadata — onboarding required.")
                upsert_erp(session, ERPSystemIn(**s), user)
    return results
