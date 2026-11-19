"""Configurable ERP master list.

ERPs live in templates/erp_master.json, so new systems can be added from the UI
(or by editing the JSON) without touching application code. Conventions held
here (date format, client column, change-date columns) are *only* applied to
columns that actually exist in supplied metadata.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config import ERP_MASTER_FILE

DATE_FORMATS = {
    "ISO": "Native DATE/TIMESTAMP or ISO-8601 strings",
    "SAP_DATS": "SAP DATS - CHAR(8) 'YYYYMMDD', '00000000' = no date",
    "JDE_JULIAN": "JDE Julian - NUMBER CYYDDD (C = century since 1900)",
    "YYYYMMDD_NUM": "NUMBER YYYYMMDD",
}

ERP_FIELDS = [
    "name", "aliases", "vendor", "category", "description", "date_format",
    "null_date_values", "client_column", "change_column_candidates",
    "leading_zero_columns", "active",
]


def _blank(name: str) -> dict[str, Any]:
    return {
        "name": name, "aliases": [], "vendor": "", "category": "ERP",
        "description": "User-defined ERP.", "date_format": "ISO", "null_date_values": [],
        "client_column": "", "change_column_candidates": [], "leading_zero_columns": [],
        "active": True,
    }


def load_erps(path: Path = ERP_MASTER_FILE) -> list[dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        erps = data.get("erps", [])
    except (FileNotFoundError, json.JSONDecodeError):
        erps = []
    return [{**_blank(e.get("name", "Unnamed")), **e} for e in erps]


def save_erps(erps: list[dict[str, Any]], path: Path = ERP_MASTER_FILE) -> None:
    payload = {
        "_comment": "ERP master list. Edit here or in the app (ERP Master page).",
        "erps": erps,
    }
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def erp_names(active_only: bool = True) -> list[str]:
    return [e["name"] for e in load_erps() if e.get("active", True) or not active_only]


def get_erp(name: str | None) -> dict[str, Any]:
    """Return the ERP profile for a name or alias; unknown names get a neutral profile."""
    if not name:
        return _blank("Unknown")
    key = name.strip().lower()
    for e in load_erps():
        if e["name"].lower() == key or key in [a.lower() for a in e.get("aliases", [])]:
            return e
    return _blank(name)


def add_erp(name: str, **attrs: Any) -> dict[str, Any]:
    erps = load_erps()
    if any(e["name"].lower() == name.strip().lower() for e in erps):
        raise ValueError(f"ERP '{name}' already exists")
    new = {**_blank(name.strip()), **attrs}
    erps.append(new)
    save_erps(erps)
    return new
