"""Data-model architecture: ER/logical diagrams (Graphviz DOT + Mermaid), fact/dimension classification,
medallion (staging / silver / gold) design and ingestion recommendations."""
from __future__ import annotations

import re
import shutil
import subprocess

import pandas as pd

from config.settings import CONFIDENCE
from services.erp_knowledge_service import ERPKnowledge, KnowledgeRepository
from services.mapping_engine import MappingResult, plan_joins

CONF_COLORS = {"standard": "#2e7d32", "uploaded": "#1565c0", "inferred": "#f9a825", "validate": "#c62828",
               "ai": "#6a1b9a"}

DIMENSION_CONCEPTS = {
    "customer": "DIM_CUSTOMER", "ship_to": "DIM_CUSTOMER (role: ship-to)", "material": "DIM_MATERIAL",
    "plant": "DIM_PLANT", "warehouse": "DIM_WAREHOUSE", "storage_location": "DIM_STORAGE_LOCATION",
    "carrier": "DIM_CARRIER", "supplier": "DIM_SUPPLIER", "sales_org": "DIM_SALES_ORG",
    "distribution_channel": "DIM_SALES_ORG", "division": "DIM_SALES_ORG", "route": "DIM_ROUTE",
    "transport_mode": "DIM_TRANSPORT_MODE", "shipping_point": "DIM_SHIPPING_POINT",
}


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# --------------------------------------------------------------------------- diagrams
def er_dot(erp: ERPKnowledge, tables: list[str], joins: list[dict], anchor: str | None = None,
           used_fields: dict[str, list[str]] | None = None, title: str = "") -> str:
    used_fields = used_fields or {}
    lines = ["digraph ER {", '  graph [rankdir=LR, fontname="Helvetica", fontsize=12, bgcolor="transparent", '
             f'label="{_esc(title)}", labelloc=t, nodesep=0.5, ranksep=0.9];',
             '  node [shape=plain, fontname="Helvetica", fontsize=10];',
             '  edge [fontname="Helvetica", fontsize=9, color="#555555"];']
    for t in tables:
        td = erp.table(t)
        conf = td.get("confidence", "validate")
        color = CONF_COLORS.get(conf, "#777777")
        head_bg = "#0d47a1" if t == anchor else ("#455a64" if not td.get("virtual") else "#6d4c41")
        pk = td.get("primary_key", []) or []
        rows = [f'<TR><TD BGCOLOR="{head_bg}" COLSPAN="2"><FONT COLOR="white"><B>{_esc(t)}</B></FONT></TD></TR>',
                f'<TR><TD COLSPAN="2"><I>{_esc(td.get("description", "")[:48])}</I></TD></TR>']
        fields = list(dict.fromkeys([p for p in pk] + used_fields.get(t, [])))
        join_fields = set()
        for j in joins:
            if j["left"] == t:
                join_fields |= {c[0] for c in j["conditions"]}
            if j["right"] == t:
                join_fields |= {c[1] for c in j["conditions"]}
        fields = list(dict.fromkeys(fields + sorted(join_fields)))[:14]
        for f in fields:
            tag = "PK" if f in pk else ("FK" if f in join_fields else "")
            rows.append(f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="8">{tag or " "}</FONT></TD>'
                        f'<TD ALIGN="LEFT" PORT="{_esc(f)}">{_esc(f)}</TD></TR>')
        if not fields:
            rows.append('<TR><TD COLSPAN="2"><FONT POINT-SIZE="8">(fields to be confirmed)</FONT></TD></TR>')
        rows.append(f'<TR><TD COLSPAN="2" BGCOLOR="{color}"><FONT COLOR="white" POINT-SIZE="8">'
                    f'{_esc(CONFIDENCE.get(conf, {}).get("label", conf))}</FONT></TD></TR>')
        node_id = re.sub(r"\W", "_", t)
        lines.append(f'  {node_id} [label=<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="3">'
                     + "".join(rows) + "</TABLE>>];")
    for j in joins:
        a, b = re.sub(r"\W", "_", j["left"]), re.sub(r"\W", "_", j["right"])
        cond = "\\n".join(f"{x}={y}" for x, y in j["conditions"][:3])
        style = "dashed" if j.get("confidence") in ("inferred", "validate") else "solid"
        color = "#c62828" if j.get("fan_out") else CONF_COLORS.get(j.get("confidence"), "#555555")
        lines.append(f'  {a} -> {b} [label="{_esc(j["cardinality"])}\\n{_esc(cond)}", style={style}, color="{color}", '
                     f'penwidth={2 if j.get("fan_out") else 1}];')
    lines.append("}")
    return "\n".join(lines)


def mapping_dot(result: MappingResult, erp: ERPKnowledge) -> str:
    used: dict[str, list[str]] = {}
    for r in result.rows:
        for t in r.get("_tables", []):
            if r.get("Source Field") and not str(r["Source Field"]).startswith(("{", "NOT", "TBD")):
                used.setdefault(t, []).append(r["Source Field"])
    return er_dot(erp, result.tables, result.joins, result.anchor, used,
                  f"{result.erp_name} - {result.business_object_label} (grain: {result.anchor})")


def business_object_dot(erp: ERPKnowledge, bo_id: str) -> str:
    bo = erp.business_objects.get(bo_id, {})
    anchor = erp.find_table(bo.get("anchor", ""))
    tables = [erp.find_table(t) for t in bo.get("tables", []) if erp.find_table(t)]
    if not anchor:
        return "digraph ER { label=\"No anchor defined\"; }"
    joins, order, _ = plan_joins(erp, anchor, tables, erp.find_table(bo.get("header", "") or ""))
    from dataclasses import asdict
    return er_dot(erp, order, [asdict(j) for j in joins], anchor, title=f"{erp.name} - {bo_id}")


def full_erp_dot(erp: ERPKnowledge, max_tables: int = 60) -> str:
    tables = list(erp.all_tables())[:max_tables]
    joins = []
    for r in erp.relationships:
        if r.get("from") in tables and r.get("to") in tables:
            joins.append({"left": r["from"], "right": r["to"], "conditions": r.get("on", []),
                          "cardinality": r.get("cardinality", ""), "confidence": r.get("confidence", "inferred"),
                          "fan_out": False})
    return er_dot(erp, tables, joins, None, title=f"{erp.name} - knowledge base relationships")


def render_dot(dot: str, fmt: str = "png") -> bytes | None:
    """Render DOT with the Graphviz binary if available (packages.txt installs it on Streamlit Cloud)."""
    exe = shutil.which("dot")
    if not exe:
        return None
    try:
        return subprocess.run([exe, f"-T{fmt}", "-Gdpi=130"], input=dot.encode(), capture_output=True,
                              timeout=30, check=True).stdout
    except Exception:
        return None


def mermaid_er(result: MappingResult, erp: ERPKnowledge) -> str:
    out = ["erDiagram"]
    card_map = {"1:N": "||--o{", "N:1": "}o--||", "1:1": "||--||", "N:N": "}o--o{"}
    for j in result.joins:
        rel = card_map.get(j["cardinality"], "||--o{")
        label = ", ".join(f"{a}={b}" for a, b in j["conditions"][:2]) or "join"
        out.append(f'    {re.sub(r"[^A-Za-z0-9_]", "_", j["left"])} {rel} '
                   f'{re.sub(r"[^A-Za-z0-9_]", "_", j["right"])} : "{label}"')
    for t in result.tables:
        td = erp.table(t)
        out.append(f"    {re.sub(r'[^A-Za-z0-9_]', '_', t)} {{")
        for p in (td.get("primary_key") or [])[:8]:
            out.append(f"        string {re.sub(r'[^A-Za-z0-9_]', '_', p)} PK")
        out.append("    }")
    return "\n".join(out)


# --------------------------------------------------------------------------- modelling
def classify_tables(result: MappingResult, erp: ERPKnowledge) -> pd.DataFrame:
    rows = []
    mt = (result.model_type or "").lower()
    for t in result.tables:
        td = erp.table(t)
        j = next((x for x in result.joins if x["right"] == t), None)
        if t == result.anchor:
            kind = "Bridge table" if "bridge" in mt else ("Snapshot fact" if "snapshot" in mt and "accumulating" not in mt
                                                          else "Transaction fact (item / grain)")
            if "dimension" in mt:
                kind = "Dimension (SCD2)"
        elif t == result.header:
            kind = "Header (fold into fact as degenerate attributes)"
        elif td.get("virtual"):
            kind = "Aggregated helper (CTE / intermediate model)"
        elif j and j["cardinality"] in ("N:1", "1:1") and not j.get("fan_out"):
            kind = "Dimension / lookup"
        elif j and j.get("fan_out"):
            kind = "Child fact (1:N - separate fact or aggregate)"
        else:
            kind = "Related table"
        rows.append({"Table": t, "Description": td.get("description", ""), "Recommended modelling": kind,
                     "Join cardinality from parent": j["cardinality"] if j else "anchor",
                     "Why required": td.get("role", "")})
    return pd.DataFrame(rows)


def grain_and_keys(result: MappingResult, erp: ERPKnowledge) -> pd.DataFrame:
    anchor_td = erp.table(result.anchor)
    rows = [
        {"Aspect": "Dataset grain (business)", "Value": result.grain},
        {"Aspect": "Effective grain of generated SQL", "Value": result.effective_grain},
        {"Aspect": "Primary key (anchor)", "Value": ", ".join(anchor_td.get("primary_key", []) or ["(unknown - validate)"])},
        {"Aspect": "Composite key", "Value": "Yes" if len(anchor_td.get("primary_key", []) or []) > 1 else "No"},
        {"Aspect": "Business key", "Value": ", ".join(result.business_key) or "(validate)"},
        {"Aspect": "Foreign keys", "Value": "; ".join(f"{j['right']}: " + ", ".join(f"{j['left']}.{a}->{j['right']}.{b}"
                                                     for a, b in j["conditions"]) for j in result.joins
                                           if j["cardinality"] in ("N:1", "1:1")) or "-"},
        {"Aspect": "One-to-one relationships", "Value": ", ".join(f"{j['left']}-{j['right']}" for j in result.joins if j["cardinality"] == "1:1") or "-"},
        {"Aspect": "One-to-many relationships", "Value": ", ".join(f"{j['left']}->{j['right']}" for j in result.joins if j["cardinality"] == "1:N") or "-"},
        {"Aspect": "Many-to-one relationships", "Value": ", ".join(f"{j['left']}->{j['right']}" for j in result.joins if j["cardinality"] == "N:1") or "-"},
        {"Aspect": "Many-to-many relationships", "Value": ", ".join(f"{j['left']}<->{j['right']}" for j in result.joins if j["cardinality"] == "N:N") or
         "None direct (bridges resolve M:N, e.g. shipment <-> delivery via VTTP)"},
        {"Aspect": "Recommended model type", "Value": result.model_type},
        {"Aspect": "Potential duplicate scenarios", "Value": " | ".join(result.duplicate_risks) or "-"},
        {"Aspect": "Deduplication strategy", "Value": "Only replication duplicates are removed: ROW_NUMBER() OVER (PARTITION BY "
         "source PK ORDER BY CDC timestamp DESC) = 1 per staging table. Business duplicates (fan-out, splits, "
         "reversals) are handled by pre-aggregation, filters or modelling - never DISTINCT."},
    ]
    return pd.DataFrame(rows)


def medallion_design(result: MappingResult, erp: ERPKnowledge, repo: KnowledgeRepository) -> dict[str, pd.DataFrame]:
    erp_u = erp.id.upper()
    bo_u = (result.business_object or "dataset").upper()
    staging = [{"Layer": "Bronze / Staging", "Object": f"STG_{erp_u}__{re.sub(r'[^A-Z0-9]', '_', t.upper())}",
                "Source": t, "Logic": "1:1 copy of replicated table; typed columns; latest version per PK "
                "(ROW_NUMBER on CDC ts); soft-delete flag kept", "Grain": "Source primary key: " +
                ", ".join(erp.table(t).get("primary_key", []) or ["?"])}
               for t in result.tables if not erp.table(t).get("virtual")]
    silver = [{"Layer": "Silver (conformed)", "Object": f"SLV_{erp_u}__{bo_u}",
               "Source": ", ".join(result.tables), "Logic": "Joins + transformations from the mapping; canonical "
               "column names; date/number conversions; status decoding", "Grain": result.grain}]
    catalog = repo.concept_catalog()
    measures, dims, dates, degenerate = [], set(), [], []
    for r in result.rows:
        cid = r.get("_concept")
        if not cid:
            continue
        c = catalog.get(cid, {})
        if cid in DIMENSION_CONCEPTS:
            dims.add(DIMENSION_CONCEPTS[cid])
        elif c.get("type", "").startswith("NUMBER"):
            measures.append(c.get("target"))
        elif c.get("type") == "DATE":
            dates.append(c.get("target"))
        else:
            degenerate.append(c.get("target"))
    gold = [{"Layer": "Gold (star)", "Object": f"FCT_{bo_u}", "Source": f"SLV_*__{bo_u} (all ERPs, UNION on canonical model)",
             "Logic": f"Measures: {', '.join(measures) or '-'} | Degenerate dims: {', '.join(degenerate) or '-'}",
             "Grain": result.grain}]
    for d in sorted(dims):
        gold.append({"Layer": "Gold (star)", "Object": d, "Source": "Master data tables (KNA1/MARA/... per ERP)",
                     "Logic": "Conformed dimension, SCD2 on descriptive attributes, surrogate key + SOURCE_SYSTEM",
                     "Grain": "One row per business key per validity period"})
    if dates:
        gold.append({"Layer": "Gold (star)", "Object": "DIM_DATE (role-playing)", "Source": "Generated calendar",
                     "Logic": "Roles: " + ", ".join(dates), "Grain": "One row per day"})
    return {"staging": pd.DataFrame(staging), "silver": pd.DataFrame(silver), "gold": pd.DataFrame(gold)}


def layer_dot(result: MappingResult, erp: ERPKnowledge, repo: KnowledgeRepository) -> str:
    d = medallion_design(result, erp, repo)
    nid = lambda x: re.sub(r"[^A-Za-z0-9_]", "_", x)  # noqa: E731
    lines = ['digraph L {', '  rankdir=LR; bgcolor="transparent";',
             '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10];',
             f'  src [label="{_esc(erp.name)}\\n(source ERP)", fillcolor="#eceff1"];']
    silver = d["silver"].iloc[0]["Object"]
    lines.append(f'  {nid(silver)} [label="{_esc(silver)}", fillcolor="#cfd8dc"];')
    for obj in d["staging"]["Object"] if not d["staging"].empty else []:
        lines.append(f'  {nid(obj)} [label="{_esc(obj)}", fillcolor="#ffe0b2"];')
        lines.append(f"  src -> {nid(obj)}; {nid(obj)} -> {nid(silver)};")
    for obj in d["gold"]["Object"]:
        lines.append(f'  {nid(obj)} [label="{_esc(obj)}", fillcolor="#fff59d"];')
        lines.append(f"  {nid(silver)} -> {nid(obj)};")
    lines.append("}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- ingestion
def ingestion_recommendations(result: MappingResult, erp: ERPKnowledge) -> list[str]:
    fam = erp.family
    recs = []
    if fam == "SAP":
        recs += [
            "Replicate with a log/trigger-based CDC tool (SAP SLT, ODP/ODQ via SAP Datasphere or BW extractors, "
            "Fivetran HVR/SAP connector, Qlik Replicate, AWS AppFlow/Theobald) - SD/LE item tables (VBAP, LIPS, VTTP) have "
            "no reliable change timestamp and deletions are invisible to timestamp-based extraction.",
            "Always load MANDT and filter the productive client; keep leading zeros on VBELN/POSNR/MATNR (VARCHAR, not NUMBER).",
            "If CDC is not available, ERDAT/AEDAT on headers can drive a header-led delta (reload all items of changed headers).",
        ]
        if erp.id == "sap_s4hana":
            recs.append("S/4HANA: replicate MATDOC for inventory movements and derive stock from it, or extract released CDS "
                        "views (e.g. I_MaterialStock, I_SalesDocumentItem, I_DeliveryDocumentItem) via ODP for stable, "
                        "upgrade-safe interfaces.")
    elif fam == "JDE":
        recs += ["Use database log-based CDC on the JDE Oracle/SQL Server/DB2 database. UPMJ/TDAY (date/time updated) "
                 "can drive incremental loads when CDC is unavailable.",
                 "Persist F4211 and F42119 both; Sales Update moves lines between them - key on KCOO/DOCO/DCTO/LNID.",
                 "Convert Julian dates and apply data-dictionary display decimals in staging."]
    elif fam == "Oracle" and erp.storage == "relational":
        recs += ["EBS: LAST_UPDATE_DATE exists on almost every table and is a reliable incremental column; "
                 "use CDC (GoldenGate / log-based) to capture deletes (e.g. WSH delivery assignments)."]
    elif fam == "Oracle":
        recs += ["Fusion: use BICC incremental extracts (last-update filter) of the relevant PVOs to object storage, "
                 "then Snowpipe/COPY into Snowflake; schedule full extracts periodically to capture deletes (BICC deleted-record extracts)."]
    elif fam == "Dynamics":
        recs += ["D365: use Synapse Link / Fabric Link (Delta) or Export to Data Lake; incremental via change feed / "
                 "SinkModifiedOn; IsDelete flags soft deletes. Include DATAAREAID in every key."]
    elif erp.storage == "api":
        recs += ["API-based source: pull changed objects with a modified-since filter, land raw JSON in a VARIANT column "
                 "with load timestamp, and flatten in staging. Keep every object version for history."]
    else:
        recs += ["Use log-based CDC from the source database where possible; otherwise a change-timestamp column per table. "
                 "Run periodic full reconciliations to capture deletes."]
    recs += [
        f"Staging dedup: keep the latest record per primary key ({', '.join(erp.table(result.anchor).get('primary_key', []) or ['confirm PK'])}) "
        "by CDC timestamp; keep soft-delete flags.",
        f"Load pattern for '{result.business_object_label}': {result.model_type}. Use MERGE on the grain key into silver; "
        "accumulating snapshots should update milestone dates in place.",
        "History: transaction tables are naturally historical; master data and statuses (customer, material, stock levels, "
        "shipment status) need daily snapshots or SCD2 in Snowflake to answer 'as-of' questions.",
        "Schedule: near-real-time (5-15 min) for shipment/delivery tracking, daily for master data and inventory snapshots.",
    ]
    return recs
