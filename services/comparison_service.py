"""Cross-ERP comparison of a business object and canonical Snowflake model generation."""
from __future__ import annotations

import pandas as pd

from config.settings import CONFIDENCE, SQL_DEFAULTS
from services.erp_knowledge_service import KnowledgeRepository
from services.mapping_engine import build_mapping
from services.sql_service import SQLBuilder


def concepts_for_object(repo: KnowledgeRepository, business_object: str) -> list[str]:
    return [cid for cid, c in repo.concept_catalog().items() if business_object in c.get("objects", [])]


def compare(repo: KnowledgeRepository, business_object: str, erp_ids: list[str], concept_ids: list[str] | None = None):
    catalog = repo.concept_catalog()
    concept_ids = concept_ids or concepts_for_object(repo, business_object)
    matrix, details = [], []
    for cid in concept_ids:
        c = catalog.get(cid, {})
        row = {"Business Field": c.get("label", cid), "Canonical Column": c.get("target", ""), "Type": c.get("type", "")}
        mapped = 0
        for eid in erp_ids:
            erp = repo.get(eid)
            spec = erp.concept_spec(cid, business_object if business_object in erp.business_objects else None)
            if not spec:
                row[erp.name] = "— missing"
                details.append({"Business Field": c.get("label", cid), "ERP": erp.name, "Source": "", "Data Type": "",
                                "Confidence": CONFIDENCE["unavailable"]["label"], "Notes / meaning": "Not available"})
                continue
            if spec.get("expr"):
                src = spec["expr"].replace("{", "").replace("}", "")
                ftype = "expression"
            elif spec.get("field"):
                src = f"{spec['table']}.{spec['field']}"
                ftype = erp.field_info(spec["table"], spec["field"]).get("type", "")
            else:
                src = f"{spec.get('table')}.?"
                ftype = ""
            lvl = spec.get("confidence", "inferred")
            row[erp.name] = f"{CONFIDENCE.get(lvl, CONFIDENCE['validate'])['icon']} {src}"
            mapped += 1
            details.append({"Business Field": c.get("label", cid), "ERP": erp.name, "Source": src, "Data Type": ftype,
                            "Confidence": CONFIDENCE.get(lvl, CONFIDENCE["validate"])["label"],
                            "Notes / meaning": spec.get("note", "")})
        row["Coverage"] = f"{mapped}/{len(erp_ids)}"
        row["Status"] = ("Common" if mapped == len(erp_ids) else "Missing in some ERPs" if mapped else "Missing everywhere")
        matrix.append(row)
    matrix_df = pd.DataFrame(matrix)
    details_df = pd.DataFrame(details)
    return matrix_df, details_df


def highlights(details_df: pd.DataFrame, matrix_df: pd.DataFrame) -> dict[str, list[str]]:
    out = {"Common fields": [], "Missing fields": [], "Different data types / transformations": [],
           "Different business meanings (see notes)": [], "Alternative / derived sources": []}
    if matrix_df.empty:
        return out
    out["Common fields"] = matrix_df.loc[matrix_df["Status"] == "Common", "Business Field"].tolist()
    out["Missing fields"] = [f"{r['Business Field']} ({r['Coverage']})" for _, r in matrix_df.iterrows() if r["Status"] != "Common"]
    for field, g in details_df[details_df["Source"] != ""].groupby("Business Field"):
        types = {t for t in g["Data Type"] if t}
        if len(types) > 1:
            out["Different data types / transformations"].append(f"{field}: " + ", ".join(
                f"{e}={t}" for e, t in zip(g["ERP"], g["Data Type"]) if t))
        notes = [f"{e}: {n}" for e, n in zip(g["ERP"], g["Notes / meaning"]) if n]
        if notes:
            out["Different business meanings (see notes)"].append(f"{field} -> " + " | ".join(notes))
        exprs = [e for e, t in zip(g["ERP"], g["Data Type"]) if t == "expression"]
        if exprs:
            out["Alternative / derived sources"].append(f"{field}: derived in {', '.join(exprs)}")
    return out


def canonical_ddl(repo: KnowledgeRepository, business_object: str, concept_ids: list[str], settings: dict | None = None) -> str:
    s = {**SQL_DEFAULTS, **(settings or {})}
    catalog = repo.concept_catalog()
    bo_label = repo.business_object_catalog().get(business_object, {}).get("label", business_object)
    cols = ["    SOURCE_SYSTEM VARCHAR NOT NULL COMMENT 'ERP the row came from'",
            "    SOURCE_GRAIN_KEY VARCHAR NOT NULL COMMENT 'Concatenated source grain key'"]
    for cid in concept_ids:
        c = catalog.get(cid, {})
        cols.append(f"    {c.get('target', cid.upper())} {c.get('type', 'VARCHAR')} COMMENT '{c.get('label', cid)}'")
    cols += ["    _LOADED_AT TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()"]
    name = f"{s['target_database']}.GOLD.CANONICAL_{business_object.upper()}"
    return (f"-- Canonical multi-ERP model for {bo_label}\n-- Primary key: (SOURCE_SYSTEM, SOURCE_GRAIN_KEY)\n"
            f"CREATE TABLE IF NOT EXISTS {name} (\n" + ",\n".join(cols) +
            ",\n    CONSTRAINT PK_CANONICAL_" + business_object.upper() + " PRIMARY KEY (SOURCE_SYSTEM, SOURCE_GRAIN_KEY)\n);")


def canonical_union_sql(repo: KnowledgeRepository, business_object: str, erp_ids: list[str], concept_ids: list[str],
                        settings: dict | None = None) -> str:
    catalog = repo.concept_catalog()
    labels = [catalog[c]["label"] for c in concept_ids if c in catalog]
    targets = [catalog[c]["target"] for c in concept_ids if c in catalog]
    types = {catalog[c]["target"]: catalog[c].get("type", "VARCHAR") for c in concept_ids if c in catalog}
    branches, notes = [], []
    for eid in erp_ids:
        erp = repo.get(eid)
        if business_object not in erp.business_objects:
            notes.append(f"-- {erp.name}: business object '{business_object}' not modelled - skipped")
            continue
        r = build_mapping(repo, eid, labels, business_object, settings=settings)
        b = SQLBuilder(r, erp, settings)
        by_target = {row["Target Snowflake Column"]: row for row in r.rows}
        key_parts = [b.col(r.aliases[r.anchor], p) for p in b.pk(r.anchor) if p != b.client_col]
        key_expr = " || '|' || ".join(f"COALESCE({k}::VARCHAR, '')" for k in key_parts) or "NULL"
        sel = [f"'{erp.id.upper()}' AS SOURCE_SYSTEM", f"{key_expr} AS SOURCE_GRAIN_KEY"]
        for t in targets:
            row = by_target.get(t)
            expr = row.get("_sql") if row else None
            sel.append(f"CAST({expr or 'NULL'} AS {types[t]}) AS {t}" + ("" if expr else "  -- not available in " + erp.name))
        joins = b.join_clauses("dedup")
        where = b.where_clauses("dedup")
        ctes = b.staging_ctes("dedup") + b.virtual_ctes("dedup")
        # prefix CTE names per ERP to keep them unique inside one statement
        prefix = erp.id + "__"
        block = "\n".join(["SELECT", b.render_items(sel), f"FROM {b.source_expr(r.anchor, 'dedup')} {r.aliases[r.anchor]}"] + joins +
                          (["WHERE " + "\n  AND ".join(where)] if where else []))
        cte_text = ",\n".join(ctes)
        for t in b.staged_tables() + [x for x in r.tables if erp.table(x).get("virtual")]:
            old = b.cte_name(t)
            cte_text = cte_text.replace(old + " AS (", prefix + old + " AS (").replace(f"FROM {old}\n", f"FROM {prefix}{old}\n")
            block = block.replace(f" {old} ", f" {prefix}{old} ")
        branches.append((cte_text, f"-- ===== {erp.name} =====\n" + block))
    all_ctes = ",\n".join(c for c, _ in branches if c)
    body = "\nUNION ALL\n".join(b for _, b in branches)
    return ("-- Canonical multi-ERP union (deduplicated sources, canonical column names and types)\n" +
            "\n".join(notes) + ("\n" if notes else "") +
            (f"WITH\n{all_ctes}\n" if all_ctes else "") + body + ";")
