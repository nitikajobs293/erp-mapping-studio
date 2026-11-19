"""Join discovery, cardinality and join-path analysis.

Traceability rules:
  * A join is CONFIRMED only when it is declared in supplied metadata
    (FK column with a TABLE.COLUMN reference) or explicitly confirmed by a user.
  * Joins inferred from shared key-column names are REQUIRES VALIDATION with a
    confidence score and written reasoning. They are never auto-confirmed.
  * Tables with no join path are reported as MISSING joins - no join is invented.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

import pandas as pd

from config import (EVIDENCE_METADATA_FK, EVIDENCE_NAME_MATCH, STATUS_CONFIRMED,
                    STATUS_REQUIRES_VALIDATION)
from erp_master import get_erp
from models import FANOUT_ALLOW, FANOUT_NOT_APPLICABLE, FANOUT_PICK_LATEST
from utils.text_utils import split_list, string_ratio, to_bool


# --------------------------------------------------------------------------- table info
def table_info(meta: pd.DataFrame) -> dict[str, dict[str, Any]]:
    info: dict[str, dict[str, Any]] = {}
    if meta is None or meta.empty:
        return info
    for table, grp in meta.groupby("table_name", sort=False):
        info[table] = {
            "schema": next((s for s in grp["schema_name"] if s), ""),
            "description": next((d for d in grp["table_description"] if d), ""),
            "columns": list(grp["column_name"]),
            "pk": [c for c, pk in zip(grp["column_name"], grp["is_primary_key"]) if to_bool(pk)],
            "types": dict(zip(grp["column_name"], grp["data_type"])),
            "descriptions": dict(zip(grp["column_name"], grp["column_description"])),
            "business": dict(zip(grp["column_name"], grp["business_description"])),
            "fk": {c: r for c, r in zip(grp["column_name"], grp["references"]) if r},
        }
    return info


def is_unique_on(table: str, columns: list[str], info: dict[str, dict[str, Any]]) -> bool | None:
    """True if columns cover the table's full PK; None when no PK is known."""
    pk = info.get(table, {}).get("pk") or []
    if not pk:
        return None
    return set(pk) <= set(columns)


def cardinality(left: str, lcols: list[str], right: str, rcols: list[str], info) -> str:
    lu, ru = is_unique_on(left, lcols, info), is_unique_on(right, rcols, info)
    if lu is None or ru is None:
        return "Unknown"
    return {(True, True): "1:1", (False, True): "N:1", (True, False): "1:N", (False, False): "M:N"}[(lu, ru)]


# --------------------------------------------------------------------------- discovery
def joins_from_metadata(meta: pd.DataFrame, erp: str) -> list[dict[str, Any]]:
    info = table_info(meta)
    grouped: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for _, row in meta.iterrows():
        for ref in split_list(row.get("references"), ";"):
            if "." not in ref:
                continue
            rt, rc = ref.rsplit(".", 1)
            grouped[(row["table_name"], rt)].append((row["column_name"], rc))
    joins = []
    for (child, parent), pairs in grouped.items():
        if parent not in info:
            # Reference to a table that is not in the supplied metadata - cannot be used.
            joins.append(_join(erp, child, [p[0] for p in pairs], parent, [p[1] for p in pairs],
                               "Unknown", STATUS_REQUIRES_VALIDATION, EVIDENCE_METADATA_FK, 0.5,
                               f"FK declared on {child} but referenced table {parent} is not in supplied metadata.",
                               include=False))
            continue
        lcols, rcols = [p[0] for p in pairs], [p[1] for p in pairs]
        card = cardinality(child, lcols, parent, rcols, info)
        joins.append(_join(erp, child, lcols, parent, rcols, card, STATUS_CONFIRMED, EVIDENCE_METADATA_FK, 1.0,
                           f"Declared in supplied metadata: {child}.({', '.join(lcols)}) references "
                           f"{parent}.({', '.join(rcols)})."))
    return joins


def suggest_joins(meta: pd.DataFrame, erp: str, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Suggest joins between tables that share PK-column names. Always Requires Validation."""
    info = table_info(meta)
    client_col = get_erp(erp).get("client_column") or ""
    linked = {frozenset((j["left_table"], j["right_table"])) for j in existing}
    comp = _components(list(info), [j for j in existing if j.get("status") == STATUS_CONFIRMED and to_bool(j.get("include", True))])
    tables = list(info)
    out: list[dict[str, Any]] = []
    for i, a in enumerate(tables):
        for b in tables[i + 1:]:
            if frozenset((a, b)) in linked:
                continue
            shared = [c for c in info[a]["columns"] if c in info[b]["columns"]]
            keyish = [c for c in shared if c in info[a]["pk"] or c in info[b]["pk"]]
            meaningful = [c for c in keyish if c != client_col]
            if not meaningful:
                continue
            # Parent = the table whose PK is fully covered by the shared key columns.
            cols = ([client_col] if client_col and client_col in shared else []) + meaningful
            a_unique, b_unique = is_unique_on(a, cols, info), is_unique_on(b, cols, info)
            if b_unique and not a_unique:
                child, parent = a, b
            elif a_unique and not b_unique:
                child, parent = b, a
            else:
                child, parent = a, b
            covers_parent_pk = bool(is_unique_on(parent, cols, info))
            desc_sim = sum(string_ratio(info[a]["descriptions"].get(c, ""), info[b]["descriptions"].get(c, ""))
                           for c in meaningful) / len(meaningful)
            type_match = all(info[a]["types"].get(c, "").split("(")[0].upper() ==
                             info[b]["types"].get(c, "").split("(")[0].upper() for c in meaningful)
            conf = 0.35 + (0.2 if covers_parent_pk else 0) + 0.15 * desc_sim + (0.05 if type_match else 0)
            conf = round(min(conf, 0.75), 2)
            reasons = [f"Tables share key column(s) {', '.join(meaningful)} (name match only - not declared as FK)."]
            reasons.append(f"Shared columns {'cover' if covers_parent_pk else 'do NOT cover'} the full PK of {parent}.")
            if desc_sim < 0.6:
                reasons.append("Column descriptions differ - the shared name may be coincidental.")
            if not type_match:
                reasons.append("Data types differ between tables.")
            reasons.append("Validate with an ERP functional expert before use.")
            card = cardinality(child, cols, parent, cols, info)
            include = conf >= 0.55 and card != "M:N"
            if comp.get(a) == comp.get(b):
                include = False
                reasons.append("Not used by default: the tables are already connected through confirmed joins.")
            if card == "M:N":
                reasons.append("Shared columns are not unique on either side (M:N) - not used by default.")
            out.append(_join(erp, child, cols, parent, cols, card, STATUS_REQUIRES_VALIDATION,
                             EVIDENCE_NAME_MATCH, conf, " ".join(reasons), include=include))
    return out


def _components(tables: list[str], joins: list[dict[str, Any]]) -> dict[str, int]:
    root = {t: t for t in tables}

    def find(x):
        while root.get(x, x) != x:
            x = root[x]
        return x

    for j in joins:
        a, b = find(j["left_table"]), find(j["right_table"])
        if a != b:
            root[a] = b
    ids: dict[str, int] = {}
    return {t: ids.setdefault(find(t), len(ids)) for t in tables}


def _join(erp, lt, lc, rt, rc, card, status, evidence, conf, reasoning, include=True) -> dict[str, Any]:
    return {
        "join_id": "", "source_erp": erp, "left_table": lt, "left_columns": ", ".join(lc),
        "right_table": rt, "right_columns": ", ".join(rc), "join_type": "LEFT JOIN",
        "cardinality": card, "status": status, "evidence": evidence, "confidence": conf,
        "fanout_handling": FANOUT_NOT_APPLICABLE, "reasoning": reasoning, "include": include,
    }


def number_joins(joins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for i, j in enumerate(joins, 1):
        j["join_id"] = f"J{i:02d}"
    return joins


def analyze_joins(meta: pd.DataFrame, erp: str) -> list[dict[str, Any]]:
    confirmed = joins_from_metadata(meta, erp)
    return confirmed + suggest_joins(meta, erp, confirmed)


# --------------------------------------------------------------------------- join path
def join_pairs(j: dict[str, Any]) -> list[tuple[str, str]]:
    lc, rc = split_list(j.get("left_columns")), split_list(j.get("right_columns"))
    return list(zip(lc, rc))


def build_join_tree(driving: str, needed: set[str], joins: list[dict[str, Any]],
                    info: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """BFS from the driving table across included joins.

    Returns ordered steps [{join, from, to, on:[(from_col,to_col)], fanout}] covering the
    tables needed (plus any intermediate tables on the path), and the unreachable tables.
    """
    usable = [j for j in joins if to_bool(j.get("include", True)) and join_pairs(j)]
    adj: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for j in usable:
        adj[j["left_table"]].append((j["right_table"], j))
        adj[j["right_table"]].append((j["left_table"], j))

    def _rank(item):  # prefer confirmed, then higher confidence
        _, j = item
        return (0 if j.get("status") == STATUS_CONFIRMED else 1, -float(j.get("confidence") or 0))

    # Weighted shortest path: confirmed joins cost 1, unconfirmed joins cost 10, so a
    # longer path of verified relationships always beats a shortcut through a guess.
    import heapq

    def _cost(j):
        return 1.0 if j.get("status") == STATUS_CONFIRMED else 10.0 - float(j.get("confidence") or 0)

    parent: dict[str, tuple[str, dict[str, Any]]] = {}
    dist = {driving: 0.0}
    heap = [(0.0, driving)]
    done: set[str] = set()
    while heap:
        d, cur = heapq.heappop(heap)
        if cur in done:
            continue
        done.add(cur)
        for nxt, j in sorted(adj[cur], key=_rank):
            nd = d + _cost(j)
            if nd < dist.get(nxt, float("inf")):
                dist[nxt] = nd
                parent[nxt] = (cur, j)
                heapq.heappush(heap, (nd, nxt))

    # Keep only the paths to needed tables (avoid unnecessary joins).
    keep: set[str] = set()
    unreachable = []
    for t in needed - {driving}:
        if t not in parent:
            unreachable.append(t)
            continue
        node = t
        while node != driving:
            keep.add(node)
            node = parent[node][0]

    steps = []
    order = [t for t in _bfs_order(driving, parent) if t in keep]
    for t in order:
        frm, j = parent[t]
        if j["left_table"] == frm:
            on = join_pairs(j)
        else:
            on = [(r, l) for l, r in join_pairs(j)]
        to_cols = [c for _, c in on]
        unique = is_unique_on(t, to_cols, info)
        steps.append({"join": j, "from": frm, "to": t, "on": on,
                      "fanout": unique is not True, "pk_unknown": unique is None})
    return {"steps": steps, "unreachable": sorted(unreachable), "tables": [driving] + order}


def _bfs_order(root: str, parent: dict[str, tuple[str, Any]]) -> list[str]:
    children: dict[str, list[str]] = defaultdict(list)
    for child, (par, _) in parent.items():
        children[par].append(child)
    out, q = [], deque([root])
    while q:
        n = q.popleft()
        for c in children[n]:
            out.append(c)
            q.append(c)
    return out


def apply_fanout_defaults(joins: list[dict[str, Any]], tree: dict[str, Any]) -> None:
    """Default handling for joins that could multiply rows relative to the grain."""
    for step in tree["steps"]:
        j = step["join"]
        if step["fanout"] and j.get("fanout_handling") in (None, "", FANOUT_NOT_APPLICABLE):
            j["fanout_handling"] = FANOUT_PICK_LATEST
            note = (f" ROW MULTIPLICATION RISK: {step['to']} is not unique on "
                    f"({', '.join(c for _, c in step['on'])}) "
                    + ("(no PK in metadata)." if step["pk_unknown"] else "relative to the grain.")
                    + " Default: keep the latest row per join key; confirm the business rule.")
            if "ROW MULTIPLICATION RISK" not in (j.get("reasoning") or ""):
                j["reasoning"] = (j.get("reasoning") or "") + note
        elif not step["fanout"] and j.get("fanout_handling") in (None, ""):
            j["fanout_handling"] = FANOUT_NOT_APPLICABLE


def join_diagram_dot(tables: list[str], joins: list[dict[str, Any]], grain_table: str | None = None) -> str:
    colors = {STATUS_CONFIRMED: "#2e7d32", STATUS_REQUIRES_VALIDATION: "#ef6c00"}
    lines = ["digraph G {", 'rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#eef3fb", '
             'fontname="Helvetica", fontsize=11]; edge [fontname="Helvetica", fontsize=9];']
    for t in tables:
        fill = "#fff3cd" if t == grain_table else "#eef3fb"
        label = f"{t}\\n(grain)" if t == grain_table else t
        lines.append(f'"{t}" [label="{label}", fillcolor="{fill}"];')
    for j in joins:
        if not to_bool(j.get("include", True)):
            continue
        color = colors.get(j.get("status"), "#c62828")
        style = "solid" if j.get("status") == STATUS_CONFIRMED else "dashed"
        lbl = f"{j.get('join_id','')} {j.get('cardinality','')}\\n{j.get('left_columns','')}"
        lines.append(f'"{j["left_table"]}" -> "{j["right_table"]}" [label="{lbl}", color="{color}", '
                     f'fontcolor="{color}", style={style}];')
    lines.append("}")
    return "\n".join(lines)


def join_diagram_text(tree: dict[str, Any]) -> str:
    if not tree or not tree.get("tables"):
        return "(no tables)"
    lines = [f"{tree['tables'][0]}  [driving table / grain]"]
    for s in tree["steps"]:
        j = s["join"]
        on = " AND ".join(f"{s['from']}.{a} = {s['to']}.{b}" for a, b in s["on"])
        flag = "  !! fan-out: " + j.get("fanout_handling", "") if s["fanout"] else ""
        lines.append(f"  └─ {j['join_type']} {s['to']}  ON {on}   [{j['join_id']} | {j['status']} | "
                     f"{j['cardinality']}]{flag}")
    for t in tree.get("unreachable", []):
        lines.append(f"  ✗ {t}  -- NO JOIN PATH (missing join)")
    return "\n".join(lines)
