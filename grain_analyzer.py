"""Data-grain identification and deduplication recommendations."""
from __future__ import annotations

from typing import Any

import pandas as pd

from config import STATUS_CONFIRMED, STATUS_REQUIRES_VALIDATION, STATUS_SUGGESTED
from erp_master import get_erp
from join_analyzer import join_pairs, is_unique_on, table_info
from utils.text_utils import coverage, split_list, to_bool, token_set, tokenize

CHANGE_WORDS = {"change", "update", "modify", "modification", "last"}
CREATE_WORDS = {"create", "entry", "entered", "insert"}
GRAIN_FILLER = {"one", "row", "per", "each", "grain", "level", "unique", "combination", "record"}


def find_change_columns(table: str, info: dict[str, dict[str, Any]], erp: str) -> list[str]:
    """Columns usable to order versions (latest first). Only columns present in metadata."""
    t = info.get(table)
    if not t:
        return []
    cols = t["columns"]
    found: list[str] = [c for c in get_erp(erp).get("change_column_candidates", []) if c in cols]
    if found:
        return found
    change, create = [], []
    for c in cols:
        toks = set(tokenize(c)) | set(tokenize(t["descriptions"].get(c, "")))
        dtype = (t["types"].get(c) or "").upper()
        datey = any(k in dtype for k in ("DATE", "TIME", "DATS", "TIMS")) or {"date", "timestamp", "time"} & toks
        if not datey:
            continue
        if toks & CHANGE_WORDS:
            change.append(c)
        elif toks & CREATE_WORDS:
            create.append(c)
    return change + create


def identify_grain(erp: str, meta: pd.DataFrame, mappings: list[dict[str, Any]],
                   joins: list[dict[str, Any]], expected_grain: str = "",
                   current: dict[str, Any] | None = None) -> dict[str, Any]:
    info = table_info(meta)
    used = [m["source_table"] for m in mappings
            if m.get("source_erp") == erp and m.get("source_table") in info]
    candidates = list(dict.fromkeys(used)) or list(info)
    if not candidates:
        return {"grain_table": "", "grain_keys": [], "status": "Missing",
                "reasoning": "No source tables available for this ERP.", "warnings": []}

    # Respect a user-confirmed grain.
    if current and current.get("status") == STATUS_CONFIRMED and current.get("grain_table") in info:
        return current

    reasons, warnings = [], []
    chosen, status, conf = None, STATUS_SUGGESTED, 0.6

    # 1) The user's expected grain text, matched against table names/descriptions.
    if expected_grain.strip():
        eg = token_set(expected_grain) - GRAIN_FILLER
        best, best_score = None, 0.0
        for t in info:
            tt = token_set(t, info[t]["description"])
            named = any(w.upper() == t for w in expected_grain.replace(",", " ").split())
            score = coverage(eg, tt) * 0.7 + (0.3 if named else 0)
            if score > best_score:
                best, best_score = t, score
        if best and best_score >= 0.5:
            chosen, conf = best, round(min(0.95, 0.6 + best_score / 3), 2)
            reasons.append(f"Expected grain '{expected_grain}' matches table {best} "
                           f"('{info[best]['description']}').")

    # 2) Otherwise the most granular table: not the 'one' side of an N:1 join to another candidate.
    parents = set()
    for j in joins:
        if not to_bool(j.get("include", True)) or j.get("source_erp") not in (erp, None, ""):
            continue
        lt, rt = j["left_table"], j["right_table"]
        lc, rc = [a for a, _ in join_pairs(j)], [b for _, b in join_pairs(j)]
        if lt in candidates and rt in candidates:
            if is_unique_on(rt, rc, info) and not is_unique_on(lt, lc, info):
                parents.add(rt)
            elif is_unique_on(lt, lc, info) and not is_unique_on(rt, rc, info):
                parents.add(lt)
    leaves = [t for t in candidates if t not in parents]
    if not chosen:
        counts = {t: used.count(t) for t in candidates}
        leaves_sorted = sorted(leaves or candidates, key=lambda t: (-counts.get(t, 0), -len(info[t]["pk"])))
        chosen = leaves_sorted[0]
        reasons.append(f"{chosen} is the most granular mapped table (it is not the 'one' side of any "
                       f"N:1 join to another mapped table) and supplies {counts.get(chosen, 0)} mapped column(s).")
        if len(leaves) > 1:
            others = [t for t in leaves if t != chosen]
            warnings.append(f"Multiple independent detail tables are mapped ({', '.join(leaves)}). Joining "
                            f"{', '.join(others)} to {chosen} can multiply rows (fan trap). Confirm the grain "
                            "and how the extra detail rows should be reduced.")
            status, conf = STATUS_REQUIRES_VALIDATION, 0.5

    pk = info[chosen]["pk"]
    if not pk:
        warnings.append(f"No primary key is defined for {chosen} in metadata - the grain and "
                        "deduplication keys cannot be confirmed.")
        status, conf = STATUS_REQUIRES_VALIDATION, min(conf, 0.4)
    change_cols = find_change_columns(chosen, info, erp)
    if not change_cols:
        warnings.append(f"No change/create date column identified on {chosen}; ROW_NUMBER ordering "
                        "falls back to the key columns and is not deterministic for true duplicates.")

    desc = info[chosen]["description"] or chosen
    return {
        "grain_table": chosen,
        "grain_keys": pk,
        "grain_description": f"One row per {desc} ({', '.join(pk) or 'keys unknown'})",
        "dedup_partition": pk,
        "dedup_order": [f"{c} DESC" for c in change_cols] or [f"{c} DESC" for c in pk],
        "change_columns": change_cols,
        "status": status,
        "confidence": conf,
        "reasoning": " ".join(reasons),
        "warnings": warnings,
    }


def duplicate_risks(erp: str, meta: pd.DataFrame, tree: dict[str, Any], grain: dict[str, Any]) -> list[str]:
    info = table_info(meta)
    risks = []
    for t in tree.get("tables", []):
        if t in info and not info[t]["pk"]:
            risks.append(f"{t}: no primary key in metadata - duplicate source rows cannot be detected reliably.")
    for s in tree.get("steps", []):
        if s["fanout"]:
            j = s["join"]
            risks.append(f"{j['join_id']} {s['from']} -> {s['to']}: target side not unique on join key; "
                         f"handling = '{j.get('fanout_handling')}'.")
    if grain.get("grain_keys"):
        risks.append("Replicated/CDC sources can hold several versions of the same key; final ROW_NUMBER over "
                     f"({', '.join(grain['grain_keys'])}) keeps one row per grain.")
    return risks
