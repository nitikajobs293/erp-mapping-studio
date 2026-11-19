"""Mapping validation and open-question generation.

Validation enforces traceability: every referenced table/column must exist in
supplied metadata, every included join must be backed by evidence, and every
Confirmed item must have confirming evidence (metadata FK or a user).
"""
from __future__ import annotations

from typing import Any

from config import (CONFIRMED_EVIDENCE, STATUS_CONFIRMED, STATUS_MISSING, STATUS_REQUIRES_VALIDATION,
                    STATUS_SUGGESTED)
from join_analyzer import join_pairs, table_info
from transformation_engine import source_refs
from utils.text_utils import clean_str, to_bool

SEV_ERROR, SEV_WARN, SEV_INFO = "Error", "Warning", "Info"


def _issue(sev: str, area: str, item: str, msg: str) -> dict[str, Any]:
    return {"severity": sev, "area": area, "item": item, "message": msg}


def validate_project(project) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not project.source_metadata:
        issues.append(_issue(SEV_ERROR, "Metadata", "-", "No source metadata loaded."))
    if not project.target_columns:
        issues.append(_issue(SEV_ERROR, "Targets", "-", "No target columns defined."))
    cfg = project.config
    if not clean_str(cfg.get("target_object")):
        issues.append(_issue(SEV_WARN, "Configuration", "Target object", "Target table/view name is empty."))

    infos = {erp: table_info(project.metadata_for(erp)) for erp in project.erps}

    for m in project.column_mappings:
        erp, tgt = m.get("source_erp"), m.get("target_column")
        info = infos.get(erp, {})
        for t, c in source_refs(m):
            if t not in info:
                issues.append(_issue(SEV_ERROR, "Column mapping", tgt, f"Table {t} is not in {erp} metadata."))
            elif c not in info[t]["columns"]:
                issues.append(_issue(SEV_ERROR, "Column mapping", tgt, f"Column {t}.{c} is not in {erp} metadata."))
        if m.get("status") == STATUS_CONFIRMED and m.get("evidence") not in CONFIRMED_EVIDENCE:
            issues.append(_issue(SEV_WARN, "Column mapping", tgt,
                                 f"Marked Confirmed but evidence is '{m.get('evidence')}'. Regenerate to record user confirmation."))
        if m.get("status") == STATUS_MISSING and to_bool(m.get("mandatory")):
            issues.append(_issue(SEV_ERROR, "Column mapping", tgt, "Mandatory target column has no source."))
        elif m.get("status") == STATUS_MISSING:
            issues.append(_issue(SEV_WARN, "Column mapping", tgt, "Target column has no source (NULL in SQL)."))

    for j in project.joins:
        if not to_bool(j.get("include", True)):
            continue
        info = infos.get(j.get("source_erp"), {})
        jid = j.get("join_id")
        lt, rt = j.get("left_table"), j.get("right_table")
        pairs = join_pairs(j)
        if not pairs:
            issues.append(_issue(SEV_ERROR, "Join", jid, "Join has no column pairs or unequal column counts."))
        for a, b in pairs:
            if lt not in info or a not in info[lt]["columns"]:
                issues.append(_issue(SEV_ERROR, "Join", jid, f"{lt}.{a} is not in metadata."))
            if rt not in info or b not in info[rt]["columns"]:
                issues.append(_issue(SEV_ERROR, "Join", jid, f"{rt}.{b} is not in metadata."))
            if lt in info and rt in info and a in info[lt]["types"] and b in info[rt]["types"]:
                ta, tb = info[lt]["types"][a].split("(")[0].upper(), info[rt]["types"][b].split("(")[0].upper()
                if ta and tb and ta != tb:
                    issues.append(_issue(SEV_WARN, "Join", jid, f"Type mismatch {lt}.{a} ({ta}) vs {rt}.{b} ({tb})."))
        if j.get("status") != STATUS_CONFIRMED:
            issues.append(_issue(SEV_WARN, "Join", jid,
                                 f"{lt} -> {rt} is '{j.get('status')}' ({j.get('evidence')}); SQL marks it for validation."))
        elif j.get("evidence") not in CONFIRMED_EVIDENCE:
            issues.append(_issue(SEV_WARN, "Join", jid, "Confirmed without metadata/user evidence."))
        if j.get("cardinality") == "M:N":
            issues.append(_issue(SEV_ERROR, "Join", jid, "Many-to-many join: rows will multiply. Resolve the grain."))

    for erp, g in project.grain.items():
        for w in g.get("warnings", []):
            issues.append(_issue(SEV_WARN, "Grain", erp, w))
        if not g.get("grain_keys"):
            issues.append(_issue(SEV_ERROR, "Grain", erp, "No grain keys - deduplication cannot be generated."))
    for t in project.table_mapping:
        if t.get("role", "").startswith("Unreachable"):
            issues.append(_issue(SEV_ERROR, "Join", t["source_table"],
                                 f"{t['source_table']} has mapped columns but no join path to the driving table."))
    return issues


def build_open_questions(project) -> list[dict[str, Any]]:
    keep = [q for q in project.open_questions if q.get("category") in ("User", "AI question")]
    prev_status = {(q.get("category"), q.get("item"), q.get("detail")): q for q in project.open_questions}
    qs: list[dict[str, Any]] = []

    def add(cat, item, detail, sev="Medium"):
        prev = prev_status.get((cat, item, detail), {})
        qs.append({"id": "", "category": cat, "item": item, "detail": detail, "severity": sev,
                   "owner": prev.get("owner", ""), "resolution_status": prev.get("resolution_status", "Open")})

    for m in project.column_mappings:
        tgt = f"{m.get('source_erp')} / {m.get('target_column')}"
        if m.get("status") == STATUS_MISSING:
            add("Missing columns", tgt, "No source in supplied metadata. " + clean_str(m.get("reasoning")),
                "High" if to_bool(m.get("mandatory")) else "Medium")
        elif m.get("status") == STATUS_REQUIRES_VALIDATION:
            add("Unverified assumptions", tgt, clean_str(m.get("reasoning")))
        elif m.get("status") == STATUS_SUGGESTED:
            add("Unverified assumptions", tgt,
                f"Suggested source {m.get('source_table')}.{m.get('source_column')} "
                f"(confidence {m.get('confidence')}) needs business confirmation.", "Low")
        if m.get("transformation_type") == "Derived" and "COALESCE" in clean_str(m.get("transformation_logic")):
            add("Business clarifications", tgt, "Confirm the precedence order for the COALESCE derivation.")

    for j in project.joins:
        if to_bool(j.get("include", True)) and j.get("status") != STATUS_CONFIRMED:
            add("Unconfirmed joins", f"{j.get('join_id')} {j.get('left_table')} -> {j.get('right_table')}",
                clean_str(j.get("reasoning")), "High")
        if "ROW MULTIPLICATION RISK" in clean_str(j.get("reasoning")):
            add("Business clarifications", f"{j.get('join_id')} {j.get('left_table')} -> {j.get('right_table')}",
                f"Fan-out join handled as '{j.get('fanout_handling')}'. Confirm which row should be kept "
                "(or whether the target grain should change).", "High")
    for t in project.table_mapping:
        if t.get("role", "").startswith("Unreachable"):
            add("Missing source tables", t["source_table"],
                "Mapped columns come from this table but no join to the driving table exists in metadata. "
                "Supply FK metadata or confirm a join.", "High")
    for erp, g in project.grain.items():
        for w in g.get("warnings", []):
            add("Business clarifications", f"{erp} grain", w, "High")
        if g.get("status") != STATUS_CONFIRMED and g.get("grain_table"):
            add("Unverified assumptions", f"{erp} grain",
                f"Grain assumed as {g.get('grain_description')}. Confirm in the Data Grain tab.", "Medium")
    for log in project.ai_log:
        for r in log.get("report", {}).get("rejected", []):
            add("AI rejected output", log.get("erp", ""), r, "Low")
    # Referenced FK tables not supplied
    for j in project.joins:
        if not to_bool(j.get("include", True)) and "not in supplied metadata" in clean_str(j.get("reasoning")):
            add("Missing source tables", j.get("right_table"), clean_str(j.get("reasoning")), "Medium")

    out = qs + keep
    for i, q in enumerate(out, 1):
        q["id"] = f"Q{i:03d}"
    return out


def summary_counts(project) -> dict[str, int]:
    counts = {s: 0 for s in (STATUS_CONFIRMED, STATUS_SUGGESTED, STATUS_REQUIRES_VALIDATION, STATUS_MISSING)}
    for m in project.column_mappings:
        counts[m.get("status", STATUS_MISSING)] = counts.get(m.get("status", STATUS_MISSING), 0) + 1
    return counts
