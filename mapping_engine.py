"""Mapping engine: orchestrates table relevance, column matching, joins, grain,
transformations, table mapping, open questions and SQL regeneration.

Status rules (never relaxed):
  Confirmed           - source named by the user (target hint / mapping file / manual edit)
                        AND it exists in supplied metadata.
  Suggested           - heuristic or AI match on supplied metadata, with confidence + reasoning.
  Requires Validation - ambiguous match, or depends on an unconfirmed join.
  Missing             - no credible source in supplied metadata. Nothing is invented.
"""
from __future__ import annotations

import re
from typing import Any, Callable

import pandas as pd

from config import (CONFIRMED_EVIDENCE, EVIDENCE_AI, EVIDENCE_NONE, EVIDENCE_SIMILARITY, EVIDENCE_USER,
                    EVIDENCE_USER_HINT, MAX_CANDIDATES_PER_TARGET, SIMILARITY_SUGGEST_THRESHOLD,
                    STATUS_CONFIRMED, STATUS_MISSING, STATUS_REQUIRES_VALIDATION, STATUS_SUGGESTED)
from grain_analyzer import identify_grain
from join_analyzer import (analyze_joins, apply_fanout_defaults, build_join_tree, number_joins, table_info)
from models import MappingProject
from transformation_engine import base_type, build_expression, source_refs
from utils.text_utils import (clean_str, coverage, jaccard, make_alias, split_list, string_ratio, to_bool,
                              token_set, tokenize)

DATE_WORDS = {"date", "timestamp", "time"}
GENERIC_WORDS = {"number", "identifier", "code", "type", "date", "name", "quantity", "unit", "value", "text",
                 "description", "flag", "status", "key", "time", "timestamp", "amount", "indicator"}
QTY_WORDS = {"quantity", "amount", "weight", "volume", "value", "price"}

ProgressFn = Callable[[float, str], None]


# --------------------------------------------------------------------------- relevance
def table_relevance(info: dict[str, dict[str, Any]], context: str, targets: list[dict[str, Any]]) -> dict[str, float]:
    ctx = token_set(context, *[t.get("business_definition") for t in targets], *[t.get("target_column") for t in targets])
    ctx_upper = {w.strip(".,;:()").upper() for w in (context or "").split()}
    scores = {}
    for t, ti in info.items():
        if t in ctx_upper:
            scores[t] = 1.0
            continue
        tt = token_set(ti["description"])
        col_tokens = token_set(*ti["descriptions"].values(), *ti.get("business", {}).values())
        scores[t] = round(min(1.0, 0.6 * coverage(tt, ctx) + 0.4 * coverage(ctx, col_tokens)), 2) if ctx else 0.5
    return scores


# --------------------------------------------------------------------------- column scoring
def _score(target: dict[str, Any], table: str, col: str, ti: dict[str, Any], relevance: float,
           driving: str | None) -> float:
    tname = clean_str(target.get("target_column"))
    name_tokens = set(tokenize(tname))
    def_tokens = set(tokenize(target.get("business_definition")))
    desc = ti["descriptions"].get(col, "")
    src_tokens = token_set(desc, col, ti.get("business", {}).get(col, ""))
    tgt_type, src_type = base_type(target.get("target_data_type")), base_type(ti["types"].get(col, ""))

    if tname.upper() == col.upper():
        score = 0.9
    else:
        score = (0.6 * coverage(name_tokens, src_tokens) + 0.25 * jaccard(def_tokens | name_tokens, src_tokens)
                 + 0.15 * string_ratio(tname, desc or col))
    # A match on generic words only ("number", "date"...) is not evidence of meaning.
    # Type compatibility: penalise semantic clashes (e.g. a date column for a text key).
    src_is_date = bool(src_tokens & DATE_WORDS) or src_type in {"DATS", "DATE", "TIMESTAMP"}
    tgt_is_date = tgt_type in {"DATE", "TIMESTAMP", "TIMESTAMP_NTZ", "DATETIME"} or bool(name_tokens & DATE_WORDS)
    if src_is_date != tgt_is_date:
        score -= 0.2
    tgt_is_qty = bool(name_tokens & QTY_WORDS) or tgt_type in {"NUMBER", "DECIMAL", "NUMERIC", "FLOAT"}
    src_is_qty = bool(src_tokens & QTY_WORDS) or src_type in {"QUAN", "CURR", "DEC"}
    if tgt_is_qty and not src_is_qty:
        score -= 0.1
    tgt_numeric = tgt_type in {"NUMBER", "DECIMAL", "NUMERIC", "FLOAT", "INT", "INTEGER"}
    if tgt_numeric and src_type in {"UNIT", "CUKY", "CLNT", "LANG", "CHAR", "VARCHAR"}:
        score -= 0.2
    if tgt_type in {"VARCHAR", "CHAR", "STRING", "TEXT"} and src_type in {"QUAN", "CURR", "DEC", "FLTP"}:
        score -= 0.15
    score += 0.08 * relevance
    if driving and table == driving:
        score += 0.05
    if col in ti["pk"]:
        score += 0.02
    # A match on generic words only ("number", "date"...) is not evidence of meaning.
    specific = name_tokens - GENERIC_WORDS
    if tname.upper() != col.upper() and specific and not (specific & src_tokens):
        score = min(score, 0.35)
    return round(max(0.0, min(score, 0.95)), 3)


def _parse_hint(hint: str) -> list[tuple[str, str]]:
    out = []
    for item in split_list(hint, ";"):
        if "." in item:
            t, c = item.rsplit(".", 1)
            out.append((t.strip().upper(), c.strip().upper()))
    return out


def map_targets(project: MappingProject, erp: str, driving: str | None = None,
                keep_confirmed: bool = True) -> list[dict[str, Any]]:
    meta = project.metadata_for(erp)
    info = table_info(meta)
    rel = table_relevance(info, project.business_context, project.target_columns)
    existing = {(m.get("source_erp"), m.get("target_column")): m for m in project.column_mappings}
    rows = []
    for tgt in project.target_columns:
        tname = tgt["target_column"]
        base = {
            "target_column": tname, "target_data_type": tgt.get("target_data_type") or "VARCHAR",
            "mandatory": to_bool(tgt.get("mandatory")), "source_erp": erp,
            "business_rule": clean_str(tgt.get("business_rules")), "join_dependency": "",
            "transformation_type": "", "transformation_logic": "",
        }
        prev = existing.get((erp, tname))
        if keep_confirmed and prev and (prev.get("status") == STATUS_CONFIRMED or prev.get("evidence") == EVIDENCE_USER):
            rows.append({**base, **prev})
            continue

        # 1) Explicit source hint from the user -> Confirmed if it exists in metadata
        hints = _parse_hint(tgt.get("source_hint", ""))
        if hints:
            found = [(t, c) for t, c in hints if t in info and c in info[t]["columns"]]
            if found and len(found) == len(hints):
                tables = list(dict.fromkeys(t for t, _ in found))
                cols = [c if t == tables[0] else f"{t}.{c}" for t, c in found]
                rows.append({**base, "source_schema": info[tables[0]]["schema"], "source_table": tables[0],
                             "source_column": ", ".join(cols),
                             "source_data_type": ", ".join(info[t]["types"].get(c, "") for t, c in found),
                             "status": STATUS_CONFIRMED, "evidence": EVIDENCE_USER_HINT, "confidence": 1.0,
                             "reasoning": f"Source specified in target requirement ({tgt['source_hint']}) and "
                                          "verified against supplied metadata."})
                continue
            if any(t in info for t, _ in hints):  # hint targets this ERP but column(s) absent
                missing = [f"{t}.{c}" for t, c in hints if not (t in info and c in info[t]["columns"])]
                rows.append({**base, "source_schema": "", "source_table": "", "source_column": "",
                             "source_data_type": "", "status": STATUS_MISSING, "evidence": EVIDENCE_NONE,
                             "confidence": 0.0,
                             "reasoning": f"Hint {tgt['source_hint']} references {', '.join(missing)}, "
                                          "which is not in supplied metadata. Not invented - please supply metadata."})
                continue

        # 2) Constant targets
        expected = clean_str(tgt.get("expected_transformation"))
        if not hints and "'" in expected and re.match(r"\s*(constant|literal|hard[- ]?code|always|set to|default)",
                                                      expected, re.I):
            rows.append({**base, "source_schema": "", "source_table": "", "source_column": "",
                         "source_data_type": "", "status": STATUS_CONFIRMED, "evidence": EVIDENCE_USER_HINT,
                         "confidence": 1.0, "expected_transformation": tgt.get("expected_transformation"),
                         "reasoning": "Constant/derived value defined in the target requirement."})
            continue

        # 3) Heuristic match on metadata text
        cands = []
        for t, ti in info.items():
            for c in ti["columns"]:
                cands.append((_score(tgt, t, c, ti, rel.get(t, 0.0), driving), t, c))
        cands.sort(reverse=True)
        top = cands[:MAX_CANDIDATES_PER_TARGET]
        if not top or top[0][0] < SIMILARITY_SUGGEST_THRESHOLD:
            best = f" Best candidate {top[0][1]}.{top[0][2]} scored only {top[0][0]:.2f}." if top else ""
            rows.append({**base, "source_schema": "", "source_table": "", "source_column": "",
                         "source_data_type": "", "status": STATUS_MISSING, "evidence": EVIDENCE_NONE,
                         "confidence": round(top[0][0], 2) if top else 0.0,
                         "reasoning": "No source column in supplied metadata matches this target." + best})
            continue
        s, t, c = top[0]
        status = STATUS_SUGGESTED
        alts = [f"{t2}.{c2} ({s2:.2f})" for s2, t2, c2 in top[1:]]
        bdesc = info[t].get("business", {}).get(c, "")
        reasoning = (f"Matched on metadata text: target '{tname}' vs {t}.{c} "
                     f"('{info[t]['descriptions'].get(c, '')}'{'; ' + bdesc if bdesc else ''}).")
        close = [x for x in top[1:] if s - x[0] < 0.05 and x[2] != c]
        if close:
            status = STATUS_REQUIRES_VALIDATION
            reasoning += " Ambiguous: other columns score almost as high."
        if alts:
            reasoning += " Alternatives: " + ", ".join(alts) + "."
        rows.append({**base, "source_schema": info[t]["schema"], "source_table": t, "source_column": c,
                     "source_data_type": info[t]["types"].get(c, ""), "status": status,
                     "evidence": EVIDENCE_SIMILARITY, "confidence": round(s, 2), "reasoning": reasoning})
    return rows


# --------------------------------------------------------------------------- joins (merge keeps user edits)
def _join_key(j: dict[str, Any]) -> tuple:
    return (j.get("source_erp"), j.get("left_table"), clean_str(j.get("left_columns")).replace(" ", ""),
            j.get("right_table"), clean_str(j.get("right_columns")).replace(" ", ""))


def merge_joins(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_by_key = {_join_key(j): j for j in old}
    merged, seen = [], set()
    for j in new:
        k = _join_key(j)
        seen.add(k)
        prev = old_by_key.get(k)
        if prev and (prev.get("evidence") == EVIDENCE_USER or prev.get("status") != j.get("status")
                     or prev.get("join_type") != j.get("join_type") or prev.get("include") != j.get("include")
                     or prev.get("fanout_handling") != j.get("fanout_handling")):
            merged.append({**j, **{f: prev[f] for f in ("join_type", "status", "evidence", "include",
                                                         "fanout_handling", "confidence") if f in prev},
                           "reasoning": prev.get("reasoning") or j.get("reasoning")})
        else:
            merged.append(j)
    # Keep user-added / AI joins that metadata analysis does not produce.
    for k, j in old_by_key.items():
        if k not in seen and j.get("evidence") in (EVIDENCE_USER, EVIDENCE_AI):
            merged.append(j)
    return merged


# --------------------------------------------------------------------------- transformations & aliases
def table_aliases(project: MappingProject, erp: str) -> dict[str, str]:
    used: set[str] = set()
    tables = list(table_info(project.metadata_for(erp)))
    return {t: make_alias(t, used) for t in tables}


def apply_transformations(project: MappingProject, erp: str) -> None:
    info = table_info(project.metadata_for(erp))
    aliases = table_aliases(project, erp)
    types = {(t, c): ti["types"].get(c, "") for t, ti in info.items() for c in ti["columns"]}
    targets = {t["target_column"]: t for t in project.target_columns}
    strip = bool(project.config.get("strip_leading_zeros_default"))
    for m in project.column_mappings:
        if m.get("source_erp") != erp:
            continue
        tgt = targets.get(m["target_column"], {})
        res = build_expression({**tgt, **m, "expected_transformation": tgt.get("expected_transformation")},
                               aliases, types, erp, strip)
        m["transformation_type"] = res["transformation_type"]
        m["transformation_logic"] = res["transformation_logic"]
        m["transformation_note"] = res["explanation"]


def refresh_join_dependencies(project: MappingProject, erp: str, tree: dict[str, Any]) -> None:
    path_join: dict[str, list[dict[str, Any]]] = {}
    by_to = {s["to"]: s for s in tree["steps"]}
    for t in tree["tables"]:
        chain, node = [], t
        while node in by_to:
            chain.append(by_to[node]["join"])
            node = by_to[node]["from"]
        path_join[t] = list(reversed(chain))
    for m in project.column_mappings:
        if m.get("source_erp") != erp:
            continue
        tables = {t for t, _ in source_refs(m)}
        deps = []
        unconfirmed = False
        for t in tables:
            if t in tree["unreachable"]:
                deps.append(f"{t}: NO JOIN PATH")
                unconfirmed = True
            for j in path_join.get(t, []):
                deps.append(j["join_id"])
                if j.get("status") != STATUS_CONFIRMED:
                    unconfirmed = True
        m["join_dependency"] = ", ".join(dict.fromkeys(deps)) or ("Driving table" if tables else "")
        if unconfirmed and m.get("status") in (STATUS_CONFIRMED, STATUS_SUGGESTED):
            m["status"] = STATUS_REQUIRES_VALIDATION
            note = " Depends on a join that is not confirmed."
            if note not in (m.get("reasoning") or ""):
                m["reasoning"] = (m.get("reasoning") or "") + note


def build_table_mapping(project: MappingProject, erp: str, tree: dict[str, Any], grain: dict[str, Any],
                        relevance: dict[str, float]) -> list[dict[str, Any]]:
    info = table_info(project.metadata_for(erp))
    aliases = table_aliases(project, erp)
    by_to = {s["to"]: s for s in tree["steps"]}
    mapped = {t for m in project.column_mappings if m.get("source_erp") == erp for t, _ in source_refs(m)}
    rows = []
    for t in tree["tables"] + tree["unreachable"]:
        ti = info.get(t, {})
        if t == grain.get("grain_table"):
            role, cond = "Driving (grain)", ""
        elif t in tree["unreachable"]:
            role, cond = "Unreachable - missing join", "MISSING"
        else:
            s = by_to[t]
            role = ("Joined (fan-out 1:N)" if s["fanout"] else "Joined (lookup N:1)") if t in mapped else "Bridge (path only)"
            cond = " AND ".join(f"{aliases.get(s['from'])}.{a} = {aliases.get(t)}.{b}" for a, b in s["on"])
            cond = f"[{s['join']['join_id']} {s['join']['status']}] {cond}"
        fks = "; ".join(f"{c} -> {r}" for c, r in ti.get("fk", {}).items())
        rows.append({
            "source_erp": erp, "source_schema": ti.get("schema", ""), "source_table": t,
            "alias": aliases.get(t, ""), "role": role, "table_description": ti.get("description", ""),
            "table_purpose": _purpose(t, role, project, erp), "primary_key": ", ".join(ti.get("pk", [])),
            "foreign_keys": fks, "join_conditions": cond, "include": True, "relevance": relevance.get(t, 0.0),
        })
    return rows


def _purpose(table: str, role: str, project: MappingProject, erp: str) -> str:
    cols = [m["target_column"] for m in project.column_mappings
            if m.get("source_erp") == erp and table in {t for t, _ in source_refs(m)}]
    if cols:
        return f"{role}; supplies {', '.join(cols[:6])}{' ...' if len(cols) > 6 else ''}"
    return role


# --------------------------------------------------------------------------- orchestration
def run_analysis(project: MappingProject, keep_confirmed: bool = True,
                 progress: ProgressFn | None = None) -> MappingProject:
    from snowflake_sql_generator import generate_all_sql
    from validation_engine import build_open_questions, validate_project

    def _p(x, msg):
        if progress:
            progress(x, msg)

    erps = [e for e in project.erps if not project.metadata_for(e).empty]
    if project.config.get("mapping_type") == "Single ERP" and erps:
        primary = (project.config.get("source_erps") or erps)[0]
        erps = [primary] if primary in erps else erps[:1]

    all_joins, all_maps, all_tables = [], [], []
    old_joins = list(project.joins)
    for i, erp in enumerate(erps):
        base = i / max(len(erps), 1)
        meta = project.metadata_for(erp)
        info = table_info(meta)
        _p(base + 0.05, f"{erp}: analysing joins")
        joins = merge_joins([j for j in old_joins if j.get("source_erp") == erp], analyze_joins(meta, erp))
        _p(base + 0.15, f"{erp}: matching target columns")
        first = map_targets(project, erp, None, keep_confirmed)
        project.column_mappings = [m for m in project.column_mappings if m.get("source_erp") != erp] + first
        grain = identify_grain(erp, meta, first, joins, project.config.get("expected_grain", ""), project.grain.get(erp))
        # Second pass prefers columns from the driving table when candidates tie.
        second = map_targets(project, erp, grain.get("grain_table"), keep_confirmed)
        project.column_mappings = [m for m in project.column_mappings if m.get("source_erp") != erp] + second
        grain = identify_grain(erp, meta, second, joins, project.config.get("expected_grain", ""), project.grain.get(erp))
        project.grain[erp] = grain
        _p(base + 0.25, f"{erp}: building join path")
        needed = {t for m in second for t, _ in source_refs(m) if t in info}
        tree = build_join_tree(grain.get("grain_table", ""), needed, joins, info) if grain.get("grain_table") else \
            {"steps": [], "unreachable": sorted(needed), "tables": []}
        apply_fanout_defaults(joins, tree)
        all_joins.extend(joins)
        all_maps.extend(second)
        project.joins = number_joins(all_joins + [j for j in old_joins if j.get("source_erp") not in erps])
        project.column_mappings = all_maps
        apply_transformations(project, erp)
        refresh_join_dependencies(project, erp, tree)
        all_tables.extend(build_table_mapping(project, erp, tree, grain, table_relevance(
            info, project.business_context, project.target_columns)))

    project.table_mapping = all_tables
    _p(0.85, "Validating mapping")
    project.validation_issues = validate_project(project)
    project.open_questions = build_open_questions(project)
    _p(0.95, "Generating Snowflake SQL")
    project.sql = generate_all_sql(project)
    project.touch()
    _p(1.0, "Done")
    return project


def regenerate_outputs(project: MappingProject) -> MappingProject:
    """Re-derive transformations, join paths, open questions and SQL after manual edits
    without re-matching columns."""
    from snowflake_sql_generator import generate_all_sql
    from validation_engine import build_open_questions, validate_project

    tables = []
    for erp in {m.get("source_erp") for m in project.column_mappings if m.get("source_erp")}:
        meta = project.metadata_for(erp)
        info = table_info(meta)
        grain = project.grain.get(erp) or identify_grain(erp, meta, project.column_mappings, project.joins,
                                                         project.config.get("expected_grain", ""))
        project.grain[erp] = grain
        maps = [m for m in project.column_mappings if m.get("source_erp") == erp]
        for m in maps:  # sources must exist in supplied metadata - nothing is taken on trust
            unknown = [f"{t}.{c}" for t, c in source_refs(m) if t not in info or c not in info[t]["columns"]]
            if unknown:
                m["status"], m["confidence"] = STATUS_MISSING, 0.0
                note = f"Source {', '.join(unknown)} is not in supplied metadata - add its metadata or correct the source."
                if note not in clean_str(m.get("reasoning")):
                    m["reasoning"] = note + " " + clean_str(m.get("reasoning"))
        for m in maps:  # user-touched rows become user evidence
            if m.get("status") == STATUS_CONFIRMED and m.get("evidence") not in CONFIRMED_EVIDENCE:
                m["evidence"] = EVIDENCE_USER
        needed = {t for m in maps for t, _ in source_refs(m) if t in info}
        joins = [j for j in project.joins if j.get("source_erp") == erp]
        for j in joins:
            if j.get("status") == STATUS_CONFIRMED and j.get("evidence") not in CONFIRMED_EVIDENCE:
                j["evidence"] = EVIDENCE_USER
        tree = build_join_tree(grain.get("grain_table", ""), needed, joins, info)
        apply_fanout_defaults(joins, tree)
        apply_transformations(project, erp)
        refresh_join_dependencies(project, erp, tree)
        tables.extend(build_table_mapping(project, erp, tree, grain,
                                          table_relevance(info, project.business_context, project.target_columns)))
    project.table_mapping = tables
    project.validation_issues = validate_project(project)
    project.open_questions = build_open_questions(project)
    project.sql = generate_all_sql(project)
    project.touch()
    return project


def join_tree_for(project: MappingProject, erp: str) -> dict[str, Any]:
    meta = project.metadata_for(erp)
    info = table_info(meta)
    grain = project.grain.get(erp, {})
    maps = [m for m in project.column_mappings if m.get("source_erp") == erp]
    needed = {t for m in maps for t, _ in source_refs(m) if t in info}
    joins = [j for j in project.joins if j.get("source_erp") == erp]
    if not grain.get("grain_table"):
        return {"steps": [], "unreachable": sorted(needed), "tables": []}
    return build_join_tree(grain["grain_table"], needed, joins, info)


# --------------------------------------------------------------------------- AI results
def apply_ai_result(project: MappingProject, erp: str, result: dict[str, Any]) -> dict[str, Any]:
    """Merge an AI response after strict verification against metadata.

    Anything referencing a table/column that is not in supplied metadata is rejected
    and logged; nothing from the AI is ever marked Confirmed.
    """
    info = table_info(project.metadata_for(erp))
    report = {"accepted_mappings": 0, "accepted_joins": 0, "rejected": []}
    by_target = {m["target_column"]: m for m in project.column_mappings if m.get("source_erp") == erp}

    for s in result.get("column_mappings", []) or []:
        tgt = clean_str(s.get("target_column")).upper()
        table = clean_str(s.get("source_table")).upper()
        cols = [clean_str(c).upper() for c in (s.get("source_columns") or split_list(s.get("source_column")))]
        if tgt not in by_target:
            report["rejected"].append(f"Mapping for unknown target '{tgt}'.")
            continue
        if not table or not cols:
            continue
        missing = [c for c in cols if table not in info or c not in info[table]["columns"]]
        if table not in info or missing:
            report["rejected"].append(f"{tgt}: AI proposed {table}.{','.join(cols)} - not in supplied metadata (rejected).")
            continue
        m = by_target[tgt]
        if m.get("status") == STATUS_CONFIRMED:
            continue
        ai_conf = min(float(s.get("confidence") or 0.6), 0.9)
        if m.get("status") == STATUS_MISSING or ai_conf > float(m.get("confidence") or 0):
            m.update({"source_table": table, "source_column": ", ".join(cols), "source_schema": info[table]["schema"],
                      "source_data_type": ", ".join(info[table]["types"].get(c, "") for c in cols),
                      "status": STATUS_SUGGESTED, "evidence": EVIDENCE_AI, "confidence": round(ai_conf, 2),
                      "reasoning": "AI recommendation (verified to exist in metadata): " + clean_str(s.get("reasoning"))})
            if clean_str(s.get("transformation_logic")):
                m["ai_transformation_hint"] = clean_str(s.get("transformation_logic"))
            report["accepted_mappings"] += 1

    existing = {frozenset((j["left_table"], j["right_table"])) for j in project.joins if j.get("source_erp") == erp}
    for j in result.get("joins", []) or []:
        lt, rt = clean_str(j.get("left_table")).upper(), clean_str(j.get("right_table")).upper()
        lc = [clean_str(c).upper() for c in (j.get("left_columns") or [])]
        rc = [clean_str(c).upper() for c in (j.get("right_columns") or [])]
        if isinstance(j.get("left_columns"), str):
            lc, rc = split_list(j["left_columns"].upper()), split_list(clean_str(j.get("right_columns")).upper())
        bad = (lt not in info or rt not in info or not lc or len(lc) != len(rc)
               or any(c not in info[lt]["columns"] for c in lc) or any(c not in info[rt]["columns"] for c in rc))
        if bad:
            report["rejected"].append(f"Join {lt}->{rt} on {lc}={rc}: not verifiable in metadata (rejected).")
            continue
        if frozenset((lt, rt)) in existing:
            continue
        from join_analyzer import cardinality
        project.joins.append({
            "join_id": "", "source_erp": erp, "left_table": lt, "left_columns": ", ".join(lc), "right_table": rt,
            "right_columns": ", ".join(rc), "join_type": "LEFT JOIN", "cardinality": cardinality(lt, lc, rt, rc, info),
            "status": STATUS_REQUIRES_VALIDATION, "evidence": EVIDENCE_AI,
            "confidence": round(min(float(j.get("confidence") or 0.5), 0.7), 2), "fanout_handling": "Not applicable",
            "reasoning": "AI recommendation - columns exist in metadata but the relationship is NOT declared. "
                         + clean_str(j.get("reasoning")), "include": True,
        })
        report["accepted_joins"] += 1
    number_joins(project.joins)

    for q in result.get("open_questions", []) or []:
        project.open_questions.append({"id": "", "category": "AI question", "item": erp,
                                       "detail": clean_str(q if isinstance(q, str) else q.get("question", q)),
                                       "severity": "Medium", "owner": "", "resolution_status": "Open"})
    project.ai_log.append({"erp": erp, "report": report, "assumptions": result.get("assumptions", [])})
    return report
