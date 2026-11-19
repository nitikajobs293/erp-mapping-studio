"""Source-to-target mapping engine.

Pipeline: business column text -> canonical concept (synonym matching) -> ERP-specific source
(table/field or expression, honouring business-object overrides) -> join plan from the business
object's anchor table (BFS over the ERP relationship graph) -> grain / fan-out analysis.
Nothing is invented: unknown columns are reported as unavailable with alternatives.
"""
from __future__ import annotations

import difflib
import re
from collections import deque
from dataclasses import asdict, dataclass, field

import pandas as pd

from config.settings import CONFIDENCE, weakest
from services.erp_knowledge_service import ERPKnowledge, KnowledgeRepository
from services.transform_rules import (column_ref, expression_tables, make_alias, render_expression,
                                      transform_for)


# --------------------------------------------------------------------------- data classes
@dataclass
class JoinEdge:
    left: str
    right: str
    conditions: list            # [(left_field, right_field)]
    cardinality: str            # as traversed: left -> right
    confidence: str
    join_type: str = "LEFT JOIN"
    filter: str = ""
    label: str = ""
    note: str = ""
    fan_out: bool = False


@dataclass
class MappingResult:
    erp_id: str
    erp_name: str
    erp_version: str
    business_object: str
    business_object_label: str
    anchor: str
    header: str | None
    grain: str
    model_type: str
    business_key: list
    rows: list = field(default_factory=list)
    tables: list = field(default_factory=list)          # ordered, anchor first
    aliases: dict = field(default_factory=dict)
    joins: list = field(default_factory=list)           # JoinEdge
    warnings: list = field(default_factory=list)
    duplicate_risks: list = field(default_factory=list)
    base_filter: str = ""
    history_union: dict = field(default_factory=dict)
    effective_grain: str = ""
    requested_columns: list = field(default_factory=list)

    def mapping_df(self) -> pd.DataFrame:
        cols = ["Business Column", "Canonical Concept", "Source ERP", "Source Table", "Source Field",
                "Source Field Description", "Data Type", "Key Role", "Join Condition", "Transformation",
                "Null Handling", "Default Value", "Target Snowflake Column", "Target Data Type",
                "Availability", "Alternative Source", "Confidence", "Validation Notes"]
        return pd.DataFrame([{c: r.get(c, "") for c in cols} for r in self.rows], columns=cols)

    def tables_df(self, erp: ERPKnowledge) -> pd.DataFrame:
        out = []
        for t in self.tables:
            td = erp.table(t)
            out.append({
                "Table": t, "Alias": self.aliases.get(t, ""), "Description": td.get("description", ""),
                "Module": td.get("module", ""), "Primary Key": ", ".join(td.get("primary_key", []) or []),
                "Role in dataset": "Anchor (grain)" if t == self.anchor else ("Header" if t == self.header else "Joined"),
                "Why required": td.get("role", ""),
                "Confidence": CONFIDENCE.get(td.get("confidence", "validate"), {}).get("label", ""),
                "Virtual (pre-aggregated CTE)": "Yes" if td.get("virtual") else "",
            })
        return pd.DataFrame(out)

    def joins_df(self) -> pd.DataFrame:
        out = []
        for j in self.joins:
            j = j if isinstance(j, dict) else asdict(j)
            cond = " AND ".join(f"{j['left']}.{a} = {j['right']}.{b}" for a, b in j["conditions"])
            out.append({"From": j["left"], "To": j["right"], "Join Type": j["join_type"], "Join Condition": cond,
                        "Extra Filter": j.get("filter", ""), "Cardinality": j["cardinality"],
                        "Fan-out (grain change)": "YES" if j.get("fan_out") else "",
                        "Confidence": CONFIDENCE.get(j["confidence"], {}).get("label", ""),
                        "Meaning": j.get("label", "") or j.get("note", "")})
        return pd.DataFrame(out)

    @property
    def overall_confidence(self) -> str:
        return weakest([r.get("_confidence") for r in self.rows if r.get("_confidence") != "unavailable"])


# --------------------------------------------------------------------------- column matching
def _norm(s: str) -> str:
    s = s.lower().replace("-", " ").replace("_", " ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_concept(text: str, catalog: dict, business_object: str | None = None) -> tuple[str | None, float]:
    """Return (concept_id, score 0..1) for a free-text business column name."""
    q = _norm(text)
    if not q:
        return None, 0.0
    best, best_score = None, 0.0
    for cid, c in catalog.items():
        cands = [_norm(c.get("label", "")), _norm(cid)] + [_norm(s) for s in c.get("synonyms", [])]
        for cand in cands:
            if not cand:
                continue
            if q == cand:
                score = 1.0
            else:
                score = difflib.SequenceMatcher(None, q, cand).ratio()
                qt, ct = set(q.split()), set(cand.split())
                if qt and ct:
                    jacc = len(qt & ct) / len(qt | ct)
                    score = max(score, 0.5 * score + 0.5 * jacc)
            if business_object and business_object in c.get("objects", []):
                score += 0.03  # tie-breaker toward the selected business object
            if score > best_score:
                best, best_score = cid, score
    return best, min(best_score, 1.0)


def parse_columns(text: str) -> list[str]:
    parts = re.split(r"[\n,;]+", text or "")
    cols = []
    for p in parts:
        p = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", p).strip()
        if p:
            cols.append(p)
    return list(dict.fromkeys(cols))


# --------------------------------------------------------------------------- join planning
def _graph(erp: ERPKnowledge) -> dict[str, list[tuple[str, dict, bool]]]:
    g: dict[str, list] = {}
    for r in erp.relationships:
        a, b = r.get("from"), r.get("to")
        if not a or not b:
            continue
        g.setdefault(a, []).append((b, r, False))
        g.setdefault(b, []).append((a, r, True))
    return g


def _flip(card: str) -> str:
    parts = card.split(":")
    return f"{parts[1]}:{parts[0]}" if len(parts) == 2 else card


def plan_joins(erp: ERPKnowledge, anchor: str, needed: list[str], header: str | None) -> tuple[list[JoinEdge], list[str], list[str]]:
    """BFS from anchor; returns (edges, ordered_tables, unreachable)."""
    g = _graph(erp)
    prev: dict[str, tuple[str, dict, bool] | None] = {anchor: None}
    q = deque([anchor])
    while q:
        cur = q.popleft()
        for nxt, rel, rev in g.get(cur, []):
            if nxt not in prev and rel.get("on"):
                prev[nxt] = (cur, rel, rev)
                q.append(nxt)
    edges: dict[str, JoinEdge] = {}
    order: list[str] = [anchor]
    unreachable: list[str] = []
    for t in needed:
        if t == anchor:
            continue
        if t not in prev:
            unreachable.append(t)
            continue
        path = []
        node = t
        while prev[node] is not None:
            path.append(node)
            node = prev[node][0]
        for node in reversed(path):
            if node in edges:
                continue
            parent, rel, rev = prev[node]
            conds = [(b, a) for a, b in rel["on"]] if rev else [tuple(x) for x in rel["on"]]
            card = _flip(rel.get("cardinality", "N:1")) if rev else rel.get("cardinality", "N:1")
            fan = card.endswith(":N") and not card.startswith("N:N") and card != "N:1"
            jt = "INNER JOIN" if (node == header and card in ("N:1", "1:1")) else "LEFT JOIN"
            edges[node] = JoinEdge(left=parent, right=node, conditions=[list(c) for c in conds], cardinality=card,
                                   confidence=rel.get("confidence", "inferred"), join_type=jt,
                                   filter=rel.get("filter", ""), label=rel.get("label", ""),
                                   note=rel.get("note", ""), fan_out=fan)
            order.append(node)
    return list(edges.values()), order, unreachable


# --------------------------------------------------------------------------- business object helpers
def resolve_business_object(erp: ERPKnowledge, repo: KnowledgeRepository, requested: str | None, concepts: list[str]) -> str | None:
    if requested and requested in erp.business_objects:
        return requested
    catalog = repo.concept_catalog()
    votes: dict[str, float] = {}
    for c in concepts:
        for bo in catalog.get(c, {}).get("objects", []):
            if bo in erp.business_objects:
                votes[bo] = votes.get(bo, 0) + 1
    if requested and requested not in erp.business_objects and votes:
        pass
    if votes:
        return max(votes, key=votes.get)
    return next(iter(erp.business_objects), None)


def _alternatives(erp: ERPKnowledge, concept: str | None, bo: dict) -> str:
    alts = []
    if concept:
        spec = erp.concepts.get(concept) or {}
        alts += spec.get("alternatives", []) or []
    if not alts:
        tables = ", ".join(bo.get("tables", [])[:6])
        alts.append(f"Search the business-object tables ({tables}) in your data dictionary" if tables else
                    "Upload the ERP data dictionary in the Metadata Repository")
        alts.append("Use 'AI suggest' to propose candidates (marked AI-suggested)")
    return " | ".join(alts)


# --------------------------------------------------------------------------- main entry
def build_mapping(repo: KnowledgeRepository, erp_id: str, columns: list[str], business_object: str | None = None,
                  version: str = "", settings: dict | None = None, extra_rows: list[dict] | None = None) -> MappingResult:
    settings = settings or {}
    erp = repo.get(erp_id)
    catalog = repo.concept_catalog()
    case = settings.get("identifier_case", "UPPER")
    date_mode = settings.get("date_mode", "VARCHAR")

    # 1. match columns -> concepts / direct TABLE.FIELD references
    matches = []
    for col in columns:
        direct = re.fullmatch(r"\s*([A-Za-z0-9_/]+)\s*[.\-]\s*([A-Za-z0-9_/]+)\s*", col)
        if direct and erp.find_table(direct.group(1)):
            matches.append((col, None, 1.0, {"table": erp.find_table(direct.group(1)), "field": direct.group(2),
                                             "confidence": "standard" if erp.field_info(direct.group(1), direct.group(2)) else "validate",
                                             "note": "Direct TABLE.FIELD reference"}))
            continue
        cid, score = match_concept(col, catalog, business_object)
        matches.append((col, cid if score >= 0.62 else None, score, None))

    bo_id = resolve_business_object(erp, repo, business_object, [m[1] for m in matches if m[1]])
    warnings: list[str] = []
    if business_object and bo_id != business_object:
        label = repo.business_object_catalog().get(business_object, {}).get("label", business_object)
        warnings.append(f"Business object '{label}' is not modelled for {erp.name} in the knowledge base - "
                        f"using '{bo_id}' as the anchor instead.")
    bo = erp.business_objects.get(bo_id, {}) if bo_id else {}
    anchor = erp.find_table(bo.get("anchor", "")) or (next(iter(erp.tables), "") if erp.tables else "")
    header = erp.find_table(bo.get("header", "")) if bo.get("header") else None

    # 2. resolve source spec per column
    resolved = []
    for col, cid, score, direct in matches:
        spec = direct or (erp.concept_spec(cid, bo_id) if cid else None)
        resolved.append((col, cid, score, spec))
    for extra in extra_rows or []:   # AI-suggested rows
        resolved.append((extra["column"], extra.get("concept"), 0.0, extra))

    needed: list[str] = []
    for _, _, _, spec in resolved:
        if not spec:
            continue
        tabs = expression_tables(spec["expr"]) if spec.get("expr") else [spec.get("table")]
        for t in tabs:
            tk = erp.find_table(t or "")
            if tk and tk not in needed:
                needed.append(tk)
    if header and header not in needed and any(r[3] for r in resolved):
        pass  # header only joined when a column needs it
    joins, order, unreachable = plan_joins(erp, anchor, needed, header)
    for t in unreachable:
        warnings.append(f"No known join path from anchor {anchor} to {t} (relationship missing or join columns "
                        f"unknown). Columns from {t} are listed but excluded from SQL until the relationship is defined.")
    used: set[str] = set()
    aliases = {t: make_alias(t, used) for t in order}

    join_of = {j.right: j for j in joins}

    # 3. build rows
    rows = []
    for col, cid, score, spec in resolved:
        concept = catalog.get(cid, {}) if cid else {}
        target_col = concept.get("target") or re.sub(r"[^A-Z0-9]+", "_", col.upper()).strip("_")
        target_type = concept.get("type", "VARCHAR")
        if not spec or (spec.get("table") and not spec.get("field") and not spec.get("expr")):
            # unavailable OR table-known-but-field-unknown
            tbl = (spec or {}).get("table", "")
            level = "validate" if tbl else "unavailable"
            rows.append({
                "Business Column": col, "Canonical Concept": concept.get("label", "(no match)") if cid else "(no match)",
                "Source ERP": erp.name, "Source Table": tbl or "NOT FOUND", "Source Field": "TBD" if tbl else "NOT FOUND",
                "Source Field Description": "", "Data Type": "", "Key Role": "", "Join Condition": "",
                "Transformation": "", "Null Handling": "", "Default Value": "",
                "Target Snowflake Column": target_col, "Target Data Type": target_type,
                "Availability": "⚠️ Field not confirmed" if tbl else "❌ Not available in knowledge base",
                "Alternative Source": _alternatives(erp, cid, bo),
                "Confidence": CONFIDENCE[level]["icon"] + " " + CONFIDENCE[level]["label"],
                "Validation Notes": (spec or {}).get("note", "") or (f"Matched concept '{cid}' (score {score:.2f}) but no "
                                     f"{erp.name} source is defined" if cid else "Column name not recognised - rename or map manually"),
                "_confidence": level, "_sql": None, "_tables": [], "_concept": cid,
            })
            continue

        level = spec.get("confidence", "inferred")
        if spec.get("expr"):
            tabs = [erp.find_table(t) or t for t in expression_tables(spec["expr"])]
            src_table = ", ".join(tabs)
            src_field = spec["expr"]
            finfo = {}
            ftype = ""
            excluded = any(t in unreachable or not erp.find_table(t) for t in tabs)
            sql = None if excluded else render_expression(spec["expr"], aliases, erp, case)
            transformation = "Derived expression"
            null_h, default = "See expression", "-"
            if sql and target_type == "DATE":
                sql = f"TRY_TO_DATE({sql})" if "TO_DATE" not in sql else sql
        else:
            src_table = erp.find_table(spec.get("table", "")) or spec.get("table", "")
            src_field = spec.get("field", "")
            finfo = erp.field_info(src_table, src_field)
            ftype = finfo.get("type", "")
            excluded = src_table in unreachable or not erp.find_table(src_table)
            col_sql = column_ref(erp, aliases.get(src_table, src_table.lower()), src_field, case)
            sql, transformation, null_h, default = transform_for(erp, ftype, target_type, col_sql, date_mode)
            if excluded:
                sql = None
            tabs = [src_table]
            if not finfo and level in ("standard",) and not spec.get("_override"):
                level = "inferred"
        tdef = erp.table(tabs[0]) if tabs else {}
        pk = tdef.get("primary_key", []) or []
        key_role = []
        if src_field in pk:
            key_role.append("PK" if len(pk) == 1 else f"Part of composite PK ({', '.join(pk)})")
        for j in joins:
            if j.right == tabs[0] and any(c[1] == src_field for c in j.conditions):
                key_role.append(f"FK -> {j.left}")
            if j.left == tabs[0] and any(c[0] == src_field for c in j.conditions):
                key_role.append(f"Referenced by {j.right}")
        jdesc = []
        for t in tabs:
            j = join_of.get(t)
            if t == anchor:
                jdesc.append(f"{t} is the anchor (grain) table")
            elif j:
                jdesc.append(" AND ".join(f"{j.left}.{a} = {j.right}.{b}" for a, b in j.conditions) +
                             (f" [{j.filter.replace('{', '').replace('}', '')}]" if j.filter else ""))
            elif t in unreachable:
                jdesc.append(f"No join path to {t}")
        notes = [spec.get("note", "")]
        if erp.is_field_uploaded(tabs[0] if tabs else "", src_field):
            notes.append("Field existence confirmed by uploaded metadata")
        if cid and score < 0.85 and not spec.get("note") == "Direct TABLE.FIELD reference":
            notes.append(f"Fuzzy match to '{concept.get('label', cid)}' (score {score:.2f}) - confirm meaning")
        if level == "ai":
            notes.append("AI-suggested - not in knowledge base; verify before use")
        rows.append({
            "Business Column": col, "Canonical Concept": concept.get("label", cid or "Direct reference"),
            "Source ERP": erp.name, "Source Table": src_table, "Source Field": src_field,
            "Source Field Description": finfo.get("description", "") if finfo else spec.get("description", ""),
            "Data Type": ftype, "Key Role": "; ".join(dict.fromkeys(key_role)),
            "Join Condition": " | ".join(jdesc), "Transformation": transformation, "Null Handling": null_h,
            "Default Value": default, "Target Snowflake Column": target_col, "Target Data Type": target_type,
            "Availability": "✅ Available" if not excluded else "⚠️ Join path missing",
            "Alternative Source": " | ".join(spec.get("alternatives", []) or []),
            "Confidence": CONFIDENCE.get(level, CONFIDENCE["validate"])["icon"] + " " +
                          CONFIDENCE.get(level, CONFIDENCE["validate"])["label"],
            "Validation Notes": " | ".join(n for n in notes if n),
            "_confidence": level, "_sql": sql, "_tables": tabs, "_concept": cid,
        })

    # 4. grain analysis
    fan = [j for j in joins if j.fan_out]
    eff = bo.get("grain", f"One row per {anchor} record")
    if fan:
        eff = (f"{eff} -> CHANGED by 1:N join(s): " + ", ".join(f"{j.left}->{j.right}" for j in fan) +
               ". Result grain is the most detailed joined table; aggregate or filter to keep the original grain.")
        warnings.append("Fan-out detected: " + "; ".join(f"{j.left} -> {j.right} is {j.cardinality}" for j in fan))
    for j in joins:
        if j.confidence in ("validate", "inferred"):
            warnings.append(f"Join {j.left} -> {j.right} is {CONFIDENCE[j.confidence]['label'].lower()}"
                            + (f": {j.note}" if j.note else ""))
    if erp.storage in ("api", "customer_specific"):
        warnings.append(f"{erp.name} storage is '{erp.storage}' - physical names depend on your extraction / installation.")

    tables_order = [t for t in order if t not in unreachable]
    return MappingResult(
        erp_id=erp.id, erp_name=erp.name, erp_version=version, business_object=bo_id or "",
        business_object_label=repo.business_object_catalog().get(bo_id or "", {}).get("label", bo_id or ""),
        anchor=anchor, header=header, grain=bo.get("grain", ""), model_type=bo.get("model_type", ""),
        business_key=bo.get("business_key", []) or [], rows=rows, tables=tables_order, aliases=aliases,
        joins=[asdict(j) for j in joins], warnings=list(dict.fromkeys(warnings)),
        duplicate_risks=bo.get("duplicate_risks", []) or [], base_filter=bo.get("base_filter", ""),
        history_union=bo.get("history_union", {}) or {}, effective_grain=eff, requested_columns=columns,
    )


def default_columns_for(repo: KnowledgeRepository, erp_id: str, business_object: str) -> list[str]:
    """All canonical concept labels that belong to a business object and are mapped for the ERP."""
    erp = repo.get(erp_id)
    bo = erp.business_objects.get(business_object, {})
    anchor = erp.find_table(bo.get("anchor", ""))
    if not anchor:
        return []
    reachable = set(plan_joins(erp, anchor, list(erp.all_tables()), None)[1])
    out = []
    for cid, c in repo.concept_catalog().items():
        if business_object not in c.get("objects", []):
            continue
        spec = erp.concept_spec(cid, business_object)
        if not spec:
            continue
        tabs = expression_tables(spec["expr"]) if spec.get("expr") else [spec.get("table")]
        if all((erp.find_table(t or "") in reachable) for t in tabs) or not spec.get("field") and not spec.get("expr"):
            out.append(c["label"])
    return out
