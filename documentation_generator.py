"""Technical mapping document (10 sections) as a structured block list.

One structure feeds every renderer (Markdown preview, DOCX, PDF), so all outputs
show exactly the same reviewed content.
Block types: h3, p, bullets, table (columns + rows), code.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from config import APP_NAME, APP_VERSION, STATUS_CONFIRMED
from join_analyzer import join_diagram_text
from mapping_engine import join_tree_for
from snowflake_sql_generator import SQL_LABELS
from transformation_engine import source_refs
from utils.text_utils import clean_str, to_bool
from validation_engine import summary_counts


def _p(text):
    return {"type": "p", "text": text}


def _h(text):
    return {"type": "h3", "text": text}


def _b(items):
    return {"type": "bullets", "items": [i for i in items if i]}


def _t(columns, rows):
    return {"type": "table", "columns": columns, "rows": [[clean_str(c) for c in r] for r in rows]}


def _c(text):
    return {"type": "code", "text": text}


def build_document(project) -> dict[str, Any]:
    c = project.config
    counts = summary_counts(project)
    erps = sorted({m.get("source_erp") for m in project.column_mappings if m.get("source_erp")}) or project.erps
    target = ".".join(x for x in (c.get("target_database"), c.get("target_schema"), c.get("target_object")) if x)
    sections = []

    # 1 Overview
    sections.append({"title": "1. Overview", "blocks": [
        _t(["Item", "Value"], [
            ["Project name", c.get("project_name")], ["Source ERP(s)", ", ".join(erps)],
            ["Module", c.get("source_module")], ["Business process", c.get("business_process")],
            ["Mapping type", c.get("mapping_type")], ["Source database / schema",
                                                      f"{c.get('source_database')} / {c.get('source_schema') or 'per table'}"],
            ["Target object", f"{target} ({c.get('target_object_type')})"],
            ["History requirement", c.get("history_requirement")], ["Load type", c.get("load_type")],
            ["Expected grain", c.get("expected_grain")], ["Author", c.get("author")],
            ["Generated", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") + f" by {APP_NAME} {APP_VERSION}"],
        ]),
        _p(f"Mapping objective: populate {target or 'the target object'} with {len(project.target_columns)} target "
           f"columns from {', '.join(erps)} source tables."),
        _t(["Status", "Target columns"], [[k, str(v)] for k, v in counts.items()]),
        _p("Status legend: Confirmed = verified against supplied metadata or confirmed by a user; Suggested = engine/AI "
           "recommendation; Requires Validation = ambiguous or depends on an unconfirmed join; Missing = no source "
           "in supplied metadata. Recommendations are never presented as verified ERP relationships."),
    ]})

    # 2 Business context
    assumptions = [f"{erp}: grain assumed as {g.get('grain_description')} ({g.get('status')})."
                   for erp, g in project.grain.items() if g.get("status") != STATUS_CONFIRMED]
    assumptions += [f"{j['join_id']} {j['left_table']} -> {j['right_table']} is a recommendation ({j['evidence']})."
                    for j in project.joins if to_bool(j.get("include", True)) and j.get("status") != STATUS_CONFIRMED]
    if c.get("sap_client"):
        assumptions.append(f"SAP client filter {c.get('client_column', 'MANDT')} = '{c.get('sap_client')}'.")
    sections.append({"title": "2. Business Context", "blocks": [
        _h("Business requirement"), _p(project.business_context or "(not provided)"),
        _h("Functional understanding"),
        _p(f"Process '{c.get('business_process') or c.get('source_module')}' in {', '.join(erps)}. "
           + " ".join(f"{erp}: {g.get('grain_description')}." for erp, g in project.grain.items())),
        _h("Scope"),
        _b([f"{len(project.target_columns)} target columns",
            f"{sum(1 for t in project.table_mapping if not t['role'].startswith('Unreachable'))} source tables in the join path",
            f"{sum(1 for j in project.joins if to_bool(j.get('include', True)))} active joins"]),
        _h("Assumptions"), _b(assumptions or ["None recorded."]),
        _h("Dependencies"),
        _b([f"Replicated source tables available in {c.get('source_database') or '<source database>'}",
            "Supplied metadata is complete and current for the listed tables",
            "Open questions in Section 10 are resolved before production deployment"]),
    ]})

    # 3 Source tables
    sections.append({"title": "3. Source Table Mapping", "blocks": [
        _t(["ERP", "Schema", "Table", "Alias", "Role", "Description", "Purpose", "Primary key", "Foreign keys",
            "Join condition"],
           [[t["source_erp"], t["source_schema"], t["source_table"], t["alias"], t["role"], t["table_description"],
             t["table_purpose"], t["primary_key"], t["foreign_keys"], t["join_conditions"]] for t in project.table_mapping])
    ]})

    # 4 Column mapping
    sections.append({"title": "4. Target Mapping (Column Level)", "blocks": [
        _t(["Target Column", "Source ERP", "Source Table", "Source Column", "Data Type", "Transformation Logic",
            "Join Dependency", "Business Rule", "Status", "Confidence"],
           [[m["target_column"], m.get("source_erp"), m.get("source_table"), m.get("source_column"),
             f"{m.get('source_data_type') or '-'} -> {m.get('target_data_type')}", m.get("transformation_logic"),
             m.get("join_dependency"), m.get("business_rule"), m.get("status"), f"{float(m.get('confidence') or 0):.2f}"]
            for m in project.column_mappings]),
        _h("Mapping evidence"),
        _t(["Target Column", "Evidence", "Reasoning"],
           [[m["target_column"], m.get("evidence"), m.get("reasoning")] for m in project.column_mappings]),
    ]})

    # 5 Logical model
    blocks = []
    for erp in erps:
        tree = join_tree_for(project, erp)
        g = project.grain.get(erp, {})
        blocks += [_h(f"{erp}"), _p(f"Expected data grain: {g.get('grain_description', 'not determined')} "
                                    f"[{g.get('status', '')}]. {g.get('reasoning', '')}"),
                   _c(join_diagram_text(tree))]
    blocks.append(_h("Table relationships"))
    blocks.append(_t(["Join", "ERP", "Left (child)", "Left columns", "Right (parent)", "Right columns", "Type",
                      "Cardinality", "Status", "Evidence", "Confidence", "Used", "Reasoning"],
                     [[j["join_id"], j["source_erp"], j["left_table"], j["left_columns"], j["right_table"],
                       j["right_columns"], j["join_type"], j["cardinality"], j["status"], j["evidence"],
                       f"{float(j.get('confidence') or 0):.2f}", "Yes" if to_bool(j.get("include", True)) else "No",
                       j.get("reasoning")] for j in project.joins]))
    sections.append({"title": "5. Logical Data Model", "blocks": blocks})

    # 6 Transformation rules
    groups: dict[str, list] = {}
    for m in project.column_mappings:
        groups.setdefault(m.get("transformation_type") or "Unmapped", []).append(m)
    blocks = []
    for ttype, ms in groups.items():
        blocks.append(_h(ttype))
        blocks.append(_t(["Target Column", "Expression", "Explanation"],
                         [[m["target_column"], m.get("transformation_logic"), m.get("transformation_note", "")] for m in ms]))
    blocks.append(_h("Null handling"))
    blocks.append(_b(["Blank strings are converted to NULL with NULLIF(TRIM(x), '').",
                      "SAP DATS '00000000' is converted to NULL before TRY_TO_DATE.",
                      "TRY_CAST / TRY_TO_DATE return NULL for invalid values; DQ-05 counts them.",
                      "Mandatory columns are not silently defaulted; DQ-03 reports NULLs."]))
    sections.append({"title": "6. Transformation Rules", "blocks": blocks})

    # 7 Dedup & history
    blocks = []
    for erp, g in project.grain.items():
        blocks += [_h(erp), _t(["Item", "Value"], [
            ["Deduplication strategy", "ROW_NUMBER() over the grain keys, keep row 1 (no DISTINCT)"],
            ["Partition keys", ", ".join(g.get("dedup_partition", []))],
            ["Sort order", ", ".join(g.get("dedup_order", []))],
            ["History", c.get("history_requirement")],
            ["Effective dating", "EFFECTIVE_FROM = latest change date; EFFECTIVE_TO = next version - 1 day; IS_CURRENT flag"
             if str(c.get("history_requirement", "")).startswith("Full") else "Not applicable (current/snapshot view)"],
            ["Current record indicator", "IS_CURRENT" if str(c.get("history_requirement", "")).startswith("Full") else "Row 1 per grain key"],
        ])]
    fan = [f"{j['join_id']} {j['left_table']} - {j['right_table']}: {j.get('fanout_handling')}"
           for j in project.joins if j.get("fanout_handling") not in (None, "", "Not applicable")]
    blocks += [_h("Row multiplication control"), _b(fan or ["No fan-out joins in the join path."])]
    sections.append({"title": "7. Deduplication and History", "blocks": blocks})

    # 8 SQL
    blocks = []
    for k, label in SQL_LABELS.items():
        if k in project.sql:
            blocks += [_h(label), _c(project.sql[k])]
    sections.append({"title": "8. Snowflake SQL", "blocks": blocks})

    # 9 DQ
    sections.append({"title": "9. Data Quality Checks", "blocks": [
        _t(["Check", "Purpose"], [
            ["DQ-01 Record count reconciliation", "Driving source rows vs target rows"],
            ["DQ-02 Duplicate validation", "No duplicates on the grain keys"],
            ["DQ-03 Null validation", "Mandatory target columns are populated"],
            ["DQ-04 Referential integrity", "Orphans per join; fan-out profile for non-unique joins"],
            ["DQ-05 Data type validation", "Source values failing TRY_ conversions"],
            ["DQ-06 Business rule validation", "One check per captured business rule"]]),
        _c(project.sql.get("data_quality", "")),
    ]})

    # 10 Open questions
    sections.append({"title": "10. Open Questions and Risks", "blocks": [
        _t(["ID", "Category", "Item", "Detail", "Severity", "Owner", "Status"],
           [[q["id"], q["category"], q["item"], q["detail"], q["severity"], q.get("owner"), q.get("resolution_status")]
            for q in project.open_questions]),
        _h("Validation findings"),
        _t(["Severity", "Area", "Item", "Message"],
           [[i["severity"], i["area"], i["item"], i["message"]] for i in project.validation_issues]),
    ]})
    return {"title": f"Technical Mapping Document - {c.get('project_name')}", "sections": sections}


def _md_cell(v: str) -> str:
    return clean_str(v).replace("|", "\\|").replace("\n", " ")


def to_markdown(doc: dict[str, Any]) -> str:
    out = [f"# {doc['title']}\n"]
    for s in doc["sections"]:
        out.append(f"## {s['title']}\n")
        for b in s["blocks"]:
            if b["type"] == "h3":
                out.append(f"### {b['text']}\n")
            elif b["type"] == "p":
                out.append(b["text"] + "\n")
            elif b["type"] == "bullets":
                out.append("\n".join(f"- {i}" for i in b["items"]) + "\n")
            elif b["type"] == "code":
                out.append("```sql\n" + b["text"] + "\n```\n")
            elif b["type"] == "table":
                if not b["rows"]:
                    out.append("_No rows._\n")
                    continue
                out.append("| " + " | ".join(b["columns"]) + " |")
                out.append("|" + "---|" * len(b["columns"]))
                out.extend("| " + " | ".join(_md_cell(c) for c in r) + " |" for r in b["rows"])
                out.append("")
    return "\n".join(out)
