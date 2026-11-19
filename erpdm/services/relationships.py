"""Table relationship engine: trusted join graph, join-path planning, fan-out detection, candidate discovery.

Joins are only ever produced from relationships stored in the repository. Name similarity is used
solely to *propose* candidates for human review (unverified_assumption) — never to generate SQL.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import networkx as nx
from sqlalchemy import select
from sqlalchemy.orm import Session

from erpdm.config import get_settings
from erpdm.constants import TRUSTED_SOURCES, SourceType, ValidationStatus
from erpdm.db.models import ERPColumn, ERPSystem, ERPTable, Relationship
from erpdm.schemas import JoinEdge, JoinPlan
from erpdm.services.metadata import get_table, relationships_among


FLIP = {"1:N": "N:1", "N:1": "1:N"}


class PlanningError(Exception):
    pass


def make_alias(name: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]", "_", name.lower())
    if len(base) > 8:
        parts = [p for p in base.split("_") if p]
        base = "".join(p[0] for p in parts) if len(parts) > 1 else base[:8]
    if base[0].isdigit():
        base = "t_" + base
    alias, i = base, 2
    while alias in used:
        alias, i = f"{base}{i}", i + 1
    used.add(alias)
    return alias


def is_usable(rel: Relationship, include_pending: bool) -> bool:
    if rel.validation_status == ValidationStatus.REJECTED.value:
        return False
    if rel.validation_status == ValidationStatus.VERIFIED.value:
        return True
    if rel.source_type in TRUSTED_SOURCES and rel.validation_status == ValidationStatus.REQUIRES_VALIDATION.value:
        return True
    return include_pending and rel.source_type != SourceType.UNVERIFIED_ASSUMPTION.value


def edge_weight(rel: Relationship, preferred: bool) -> float:
    w = 1.0 + (1.0 - float(rel.confidence or 0)) * 3
    if rel.validation_status == ValidationStatus.VERIFIED.value:
        w -= 0.2
    if rel.relationship_type in ("status", "document_flow"):
        w += 1.5  # avoid routing through 1:N status / flow tables unless needed
    if preferred:
        w = 0.05
    return w


def build_graph(session: Session, erp_id: int, include_pending: bool = False,
                prefer_ids: set[int] | None = None, exclude_ids: set[int] | None = None) -> nx.MultiGraph:
    g = nx.MultiGraph()
    prefer_ids, exclude_ids = prefer_ids or set(), exclude_ids or set()
    for rel in relationships_among(session, erp_id):
        if rel.id in exclude_ids or not is_usable(rel, include_pending):
            continue
        g.add_edge(rel.from_table.name, rel.to_table.name, key=rel.id, rel=rel,
                   weight=edge_weight(rel, rel.id in prefer_ids))
    return g


def _best_edge(g: nx.MultiGraph, a: str, b: str):
    data = g.get_edge_data(a, b)
    key = min(data, key=lambda k: data[k]["weight"])
    return data[key]["rel"], len(data)


def _covered_keys(table: ERPTable, joined_cols: set[str], extra: str, client: str) -> list[str]:
    extra_up = (extra or "").upper()
    pk = [c for c in table.primary_key if c.upper() != client]
    return [k for k in pk if k.upper() not in joined_cols and not re.search(rf"\b{re.escape(k.upper())}\b", extra_up)]


def render_condition(rel: Relationship, alias_from: str, alias_to: str, lang: str) -> str:
    parts = [f"{alias_from}.{jc['from']} = {alias_to}.{jc['to']}" for jc in rel.join_columns]
    if rel.extra_condition:
        parts.append(rel.extra_condition.replace("{from}", alias_from).replace("{to}", alias_to)
                     .replace("{lang}", lang))
    return " AND ".join(parts)


def plan_joins(session: Session, erp: ERPSystem, tables: list[str], root: str | None = None,
               include_pending: bool = False, prefer_ids: set[int] | None = None,
               exclude_ids: set[int] | None = None, lang: str | None = None) -> JoinPlan:
    """Minimal join tree connecting `tables`, starting at `root` (Steiner approximation)."""
    lang = lang or erp.default_language_key or get_settings().default_language_key
    client = (erp.client_column or "").upper()
    wanted: list[str] = []
    meta: dict[str, ERPTable] = {}
    for t in tables:
        tbl = get_table(session, erp.id, t)
        if tbl is None:
            raise PlanningError(f"Table {t} is not in the {erp.code} repository.")
        if tbl.name not in wanted:
            wanted.append(tbl.name)
            meta[tbl.name] = tbl
    if not wanted:
        raise PlanningError("No tables selected.")
    root_name = (get_table(session, erp.id, root).name if root and get_table(session, erp.id, root) else wanted[0])
    if root_name not in wanted:
        wanted.insert(0, root_name)
        meta[root_name] = get_table(session, erp.id, root_name)

    g = build_graph(session, erp.id, include_pending, prefer_ids, exclude_ids)
    # Bridge penalty: never link two transactions through a shared master/lookup table (e.g. orders and
    # deliveries via the customer master) when a document path exists — that creates a many-to-many.
    wanted_set = set(wanted)
    for a, b, k, d in g.edges(keys=True, data=True):
        rel = d["rel"]
        for name, cls in ((rel.from_table.name, rel.from_table.table_class), (rel.to_table.name, rel.to_table.table_class)):
            if name not in wanted_set and cls in ("master", "config", "text"):
                d["weight"] += 2.0
    connected = {root_name}
    order = [root_name]
    edges: list[JoinEdge] = []
    warnings: list[str] = []
    unreachable: list[str] = []
    used_aliases: set[str] = set()
    aliases = {root_name: make_alias(root_name, used_aliases)}
    optional = {root_name: False}
    unverified: list[str] = []

    remaining = [t for t in wanted if t != root_name]
    while remaining:
        if root_name not in g:
            unreachable.extend(remaining)
            break
        lengths, paths = nx.multi_source_dijkstra(g, connected, weight="weight")
        reachable = [t for t in remaining if t in lengths]
        if not reachable:
            unreachable.extend(remaining)
            break
        target = min(reachable, key=lambda t: lengths[t])
        path = paths[target]
        # ambiguity: another, different path of (almost) equal cost?
        try:
            alt = [p for p in nx.shortest_simple_paths(nx.Graph(g), path[0], target, weight="weight")][:2]
            if len(alt) > 1 and alt[1] != path:
                cost = lambda p: sum(min(d["weight"] for d in g.get_edge_data(a, b).values()) for a, b in zip(p, p[1:]))
                if abs(cost(alt[1]) - cost(path)) < 0.25:
                    warnings.append(f"{target} is reachable via more than one path "
                                    f"({' → '.join(path)} vs {' → '.join(alt[1])}). Chose the first; pin a "
                                    "relationship to change it.")
        except (nx.NetworkXNoPath, nx.NodeNotFound, nx.NetworkXError):
            pass
        for a, b in zip(path, path[1:]):
            if b in connected:
                continue
            rel, parallel = _best_edge(g, a, b)
            if parallel > 1:
                warnings.append(f"{a} and {b} have {parallel} usable relationships; used #{rel.id} "
                                f"({', '.join(jc['from'] + '=' + jc['to'] for jc in rel.join_columns)}).")
            if b not in meta:
                meta[b] = get_table(session, erp.id, b)
                warnings.append(f"Bridge table {b} added to connect {target}.")
            aliases[b] = make_alias(b, used_aliases)
            reversed_ = rel.from_table.name != a
            alias_from = aliases[rel.from_table.name]
            alias_to = aliases[rel.to_table.name]
            cond = render_condition(rel, alias_from, alias_to, lang)
            # Fan-out: does joining b multiply rows of the current result?
            right_cols = {jc["from" if reversed_ else "to"].upper() for jc in rel.join_columns}
            extra = (rel.extra_condition or "").replace("{from}" if reversed_ else "{to}", "@@")
            extra_right = " ".join(re.findall(r"@@\.(\w+)", extra))
            uncovered = _covered_keys(meta[b], right_cols, extra_right, client)
            jt = rel.join_type
            if optional.get(a) and jt == "INNER":
                jt = "LEFT"
                warnings.append(f"Join to {b} downgraded to LEFT because {a} is optionally joined.")
            optional[b] = optional.get(a, False) or jt == "LEFT"
            edges.append(JoinEdge(
                relationship_id=rel.id, left_table=a, right_table=b, left_alias=aliases[a], right_alias=aliases[b],
                join_type=jt, condition_sql=cond,
                cardinality=FLIP.get(rel.cardinality, rel.cardinality) if reversed_ else rel.cardinality, relationship_type=rel.relationship_type,
                description=rel.description, business_purpose=rel.business_purpose, source_type=rel.source_type,
                confidence=rel.confidence, validation_status=rel.validation_status, is_mandatory=rel.is_mandatory,
                reversed=reversed_, fans_out=bool(uncovered), uncovered_keys=uncovered,
                join_columns=[{"left": jc["to" if reversed_ else "from"], "right": jc["from" if reversed_ else "to"]}
                              for jc in rel.join_columns]))
            if uncovered:
                warnings.append(f"Joining {b} can multiply rows: key column(s) {uncovered} are not constrained.")
            if rel.validation_status != ValidationStatus.VERIFIED.value:
                unverified.append(f"{a}↔{b} ({rel.source_type}, {rel.validation_status}, {rel.confidence:.2f})")
            connected.add(b)
            order.append(b)
        remaining = [t for t in remaining if t not in connected]

    if unverified:
        warnings.insert(0, f"{len(unverified)} of {len(edges)} joins are not verified — validate before production: "
                        + "; ".join(unverified))
    for t in unreachable:
        warnings.append(f"No trusted relationship path to {t}. Add/approve a relationship or enable pending ones.")
    return JoinPlan(erp_code=erp.code, root_table=root_name, tables=order, aliases=aliases, edges=edges,
                    unreachable=unreachable, warnings=list(dict.fromkeys(warnings)))


# ----------------------------------------------------------------------------- candidate discovery
@dataclass
class Candidate:
    from_table: str
    to_table: str
    join_columns: list[dict]
    reason: str
    confidence: float


def discover_candidates(session: Session, erp: ERPSystem, limit: int = 100) -> list[Candidate]:
    """Name-based *suggestions only*: a column of table A equals the full (non-client) PK of table B.
    Returned as unverified assumptions for human review. Never used for SQL until approved."""
    client = (erp.client_column or "").upper()
    tables = list(session.scalars(select(ERPTable).where(ERPTable.erp_id == erp.id)))
    existing = {(r.from_table_id, r.to_table_id) for r in relationships_among(session, erp.id, include_rejected=True)}
    existing |= {(b, a) for a, b in existing}
    out: list[Candidate] = []
    for b in tables:
        pk = [c for c in b.primary_key if c.upper() != client]
        if len(pk) != 1:
            continue
        key = pk[0].upper()
        for a in tables:
            if a.id == b.id or (a.id, b.id) in existing:
                continue
            col = next((c for c in a.columns if c.name.upper() == key and not c.is_pk), None)
            if col is None:
                continue
            jc = ([{"from": client, "to": client}] if client and any(c.name.upper() == client for c in a.columns) else [])
            jc.append({"from": col.name, "to": pk[0]})
            out.append(Candidate(a.name, b.name, jc,
                                 f"{a.name}.{col.name} has the same name as the single key of {b.name}. "
                                 "Name match only — requires validation.", 0.3))
            if len(out) >= limit:
                return out
    return out


def relationship_rows(session: Session, erp_id: int, table_names: list[str] | None = None,
                      include_rejected: bool = False) -> list[dict]:
    ids = None
    if table_names:
        ids = [t.id for t in (get_table(session, erp_id, n) for n in table_names) if t]
    rows = []
    for r in relationships_among(session, erp_id, ids, include_rejected):
        if table_names and not (r.from_table.name in table_names and r.to_table.name in table_names):
            continue
        rows.append({
            "id": r.id, "Source table": r.from_table.name, "Source description": r.from_table.description,
            "Target table": r.to_table.name, "Target description": r.to_table.description,
            "Join columns": ", ".join(f"{r.from_table.name}.{j['from']} = {r.to_table.name}.{j['to']}"
                                      for j in r.join_columns),
            "Extra condition": r.extra_condition or "", "Join type": r.join_type, "Cardinality": r.cardinality,
            "Relationship type": r.relationship_type, "Mandatory": r.is_mandatory,
            "Description": r.description or "", "Business purpose": r.business_purpose or "",
            "Source": r.source_type, "Confidence": round(r.confidence or 0, 2), "Validation": r.validation_status,
            "Evidence": r.evidence or "",
        })
    return rows


def key_columns(table: ERPTable) -> list[ERPColumn]:
    return [c for c in table.columns if c.is_pk]
