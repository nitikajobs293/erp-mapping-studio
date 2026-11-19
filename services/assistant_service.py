"""Natural-language request understanding, T-code analysis, offline (knowledge-base) answers and
LLM context assembly."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from config.settings import CONFIDENCE
from services.erp_knowledge_service import ERPKnowledge, KnowledgeRepository
from services.mapping_engine import build_mapping, default_columns_for, parse_columns, plan_joins
from services.rag_service import knowledge_base_chunks

BO_KEYWORDS = [
    ("serial_number", r"serial"),
    ("inventory_movement", r"movement|material document|item ledger|cardex|goods issue posting|stock transaction"),
    ("shipment", r"shipment|transport|freight|carrier|tracking|trip|load\b|route"),
    ("delivery", r"deliver|pick|pack|dispatch|goods issue"),
    ("billing", r"invoice|billing"),
    ("returns", r"return|reverse logistic"),
    ("stock_transfer", r"stock transfer|intercompany|sto\b"),
    ("purchase_order", r"purchase|procure|goods receipt|supplier|vendor"),
    ("production_order", r"production|work order|manufactur|component"),
    ("inventory", r"inventory|stock|on hand|on-hand|warehouse stock|batch"),
    ("sales_order", r"sales order|order header|order item|order line|open order|backorder|so\b"),
]


@dataclass
class Intent:
    text: str
    erp_id: str | None = None
    erp_ids: list = field(default_factory=list)
    tcode: str | None = None
    business_object: str | None = None
    business_objects: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    compare: bool = False
    wants_sql: bool = False


def parse_request(text: str, repo: KnowledgeRepository, default_erp: str | None = None) -> Intent:
    it = Intent(text=text)
    low = text.lower()
    # ERPs mentioned (all, for comparisons)
    found = []
    for e in repo.erps.values():
        names = [e.name.lower(), e.id.replace("_", " ")] + [a.lower() for a in e.meta.get("aliases", [])]
        if any(re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", low) for n in names if len(n) > 1):
            found.append(e.id)
    # 'sap' alone -> S/4HANA unless ECC mentioned
    if re.search(r"(?<![a-z])sap(?![a-z])", low) and not any(f.startswith("sap") for f in found):
        found.append("sap_s4hana")
    if "sap_ecc" in found and "sap_s4hana" in found and not re.search(r"s/?4|nsap", low):
        found.remove("sap_s4hana")
    it.erp_ids = list(dict.fromkeys(found))
    it.erp_id = repo.detect_erp(text) or (it.erp_ids[0] if it.erp_ids else default_erp)
    if it.erp_id not in repo.erps:
        it.erp_id = default_erp
    it.compare = bool(re.search(r"\bcompare|comparison|difference|differences|versus|\bvs\.?\b", low)) and len(it.erp_ids) >= 2
    it.wants_sql = bool(re.search(r"\bsql|query|snowflake|select\b|view|dbt", low))
    # T-code
    m = re.search(r"(?:t-?code|transaction(?: code)?|tcode|report|screen|program)\s*[:=]?\s*([A-Za-z0-9_/]{2,20})", text, flags=re.I)
    erp = repo.get(it.erp_id) if it.erp_id else None
    if m and m.group(1).lower() not in ("for", "the", "code", "is", "of"):
        it.tcode = m.group(1).upper()
    elif erp:
        for code in erp.transactions:
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])", text, flags=re.I) and len(code) >= 3:
                it.tcode = code
                break
    # business object
    hits = [(m.start(), bo) for bo, pat in BO_KEYWORDS for m in [re.search(pat, low)] if m]
    it.business_objects = [bo for _, bo in sorted(hits)]
    if it.business_objects:
        # most specific keyword wins (BO_KEYWORDS is ordered specific -> generic)
        it.business_object = next(bo for bo, _ in BO_KEYWORDS if bo in it.business_objects)
    # columns
    m = re.search(r"(?:i need|need|columns?\s*[:=]|fields?\s*[:=]|attributes?\s*[:=]|with columns)\s*(.+?)(?:\.\s|\. *$|find|generate|$)",
                  text, flags=re.I | re.S)
    if m:
        raw = re.sub(r"\band\b", ",", m.group(1))
        cols = [c for c in parse_columns(raw) if 1 < len(c) < 60]
        if len(cols) >= 2:
            it.columns = cols
    # tables mentioned
    if erp:
        for tok in re.findall(r"[A-Za-z0-9_/]{3,30}", text):
            t = erp.find_table(tok)
            if t and t not in it.tables and (tok.isupper() or erp.family != "SAP"):
                it.tables.append(t)
    return it


# --------------------------------------------------------------------------- T-code analysis
def tcode_analysis(repo: KnowledgeRepository, erp_id: str, code: str, version: str = "", settings: dict | None = None) -> dict:
    erp = repo.get(erp_id)
    code_u = code.strip().upper()
    tx = None
    for k, v in erp.transactions.items():
        if k.upper() == code_u:
            tx, code_u = v, k
            break
    res: dict = {"code": code_u, "erp": erp.name, "found": tx is not None}
    if not tx:
        custom = erp.family == "SAP" and code_u[:1] in ("Z", "Y")
        res["type"] = "Custom (customer namespace Z*/Y*)" if custom else "Not in knowledge base"
        res["message"] = (
            ("Custom transactions are customer-specific: find the program in SE93, then the tables via SE80/SE38 "
             "(or ST05 SQL trace while running the transaction). Upload the result to the metadata repository.")
            if custom else
            ("This transaction/screen is not in the knowledge base. Use the AI assistant (if configured) for a "
             "suggestion - it will be flagged 'AI-suggested' - and confirm with SE93/ST05 (SAP) or the application's "
             "data dictionary.")
        )
        return res
    bo_id = tx.get("business_object")
    tables = [t for t in tx.get("tables", [])]
    known = [erp.find_table(t) for t in tables if erp.find_table(t)]
    unknown = [t for t in tables if not erp.find_table(t)]
    res.update({
        "description": tx.get("description", ""), "type": "Standard" if tx.get("type") == "standard" else tx.get("type", ""),
        "program": tx.get("program", "Not recorded - check SE93 (System > Status) in your system"),
        "program_confidence": tx.get("program_confidence", "validate" if not tx.get("program") else "inferred"),
        "business_object": bo_id, "notes": tx.get("notes", ""), "historical": tx.get("historical", ""),
        "tables_unknown": unknown,
    })
    trows = []
    for t in known:
        td = erp.table(t)
        trows.append({"Table": t, "Description": td.get("description", ""), "Primary Key": ", ".join(td.get("primary_key", []) or []),
                      "Role": td.get("role", ""), "Confidence": CONFIDENCE.get(td.get("confidence"), {}).get("label", "")})
    for t in unknown:
        trows.append({"Table": t, "Description": "(listed for this transaction; field details not in knowledge base)",
                      "Primary Key": "", "Role": "", "Confidence": CONFIDENCE["validate"]["label"]})
    res["tables_df"] = pd.DataFrame(trows)
    bo = erp.business_objects.get(bo_id, {})
    anchor = erp.find_table(bo.get("anchor", "")) or (known[0] if known else None)
    if anchor:
        joins, order, unreach = plan_joins(erp, anchor, known, erp.find_table(bo.get("header", "") or ""))
        res["relationships_df"] = pd.DataFrame([{
            "From": j.left, "To": j.right, "Join Condition": " AND ".join(f"{j.left}.{a} = {j.right}.{b}" for a, b in j.conditions),
            "Filter": (j.filter or "").replace("{", "").replace("}", ""), "Cardinality": j.cardinality,
            "Confidence": CONFIDENCE.get(j.confidence, {}).get("label", ""), "Meaning": j.label or j.note} for j in joins])
        res["unreachable"] = unreach
    cols = default_columns_for(repo, erp_id, bo_id) if bo_id in erp.business_objects else []
    if cols:
        res["mapping"] = build_mapping(repo, erp_id, cols, bo_id, version=version, settings=settings)
    return res


# --------------------------------------------------------------------------- context for the LLM
def table_context(erp: ERPKnowledge, tables: list[str], max_fields: int = 40) -> str:
    out = []
    for t in tables:
        td = erp.table(t)
        if not td:
            continue
        fields = list((td.get("fields") or {}).items())[:max_fields]
        out.append(f"TABLE {t} [{td.get('confidence', 'validate')}]: {td.get('description', '')}. PK: "
                   f"{', '.join(td.get('primary_key', []) or [])}. {td.get('role', '')} {td.get('notes', '')}\n  Fields: " +
                   "; ".join(f"{f} ({v.get('type', '')}) {v.get('description', '')}" for f, v in fields))
    rels = [r for r in erp.relationships if r.get("from") in tables or r.get("to") in tables]
    if rels:
        out.append("RELATIONSHIPS:\n" + "\n".join(
            f"  {r['from']} -> {r['to']} {r.get('cardinality', '')} ON " + " AND ".join(f"{r['from']}.{a} = {r['to']}.{b}" for a, b in r.get("on", []))
            + (f" FILTER {r['filter']}" if r.get("filter") else "") + f" [{r.get('confidence', '')}]" for r in rels))
    return "\n".join(out)


def build_context(intent: Intent, repo: KnowledgeRepository, store, version: str = "") -> tuple[str, list[dict]]:
    parts, sources = [], []
    erp_ids = intent.erp_ids if intent.compare else [intent.erp_id] if intent.erp_id else []
    for eid in erp_ids:
        erp = repo.get(eid)
        tables = list(intent.tables)
        if intent.tcode and intent.tcode in erp.transactions:
            tx = erp.transactions[intent.tcode]
            parts.append(f"TRANSACTION {intent.tcode} ({erp.name}): {tx.get('description', '')}; tables {tx.get('tables')}; "
                         f"program {tx.get('program', 'unknown')}; notes: {tx.get('notes', '')}")
            tables += [erp.find_table(t) for t in tx.get("tables", []) if erp.find_table(t)]
        if intent.business_object and intent.business_object in erp.business_objects:
            bo = erp.business_objects[intent.business_object]
            parts.append(f"BUSINESS OBJECT {intent.business_object} ({erp.name}): anchor {bo.get('anchor')}, grain: "
                         f"{bo.get('grain')}, model: {bo.get('model_type')}, duplicate risks: {bo.get('duplicate_risks')}")
            tables += [erp.find_table(t) for t in bo.get("tables", []) if erp.find_table(t)]
        tables = list(dict.fromkeys(t for t in tables if t))[:18]
        parts.append(f"=== KNOWLEDGE BASE: {erp.name} {version} (storage: {erp.storage}) ===\n{erp.meta.get('notes', '')}\n" +
                     table_context(erp, tables))
        hits = store.search(intent.text, eid, k=5, extra=knowledge_base_chunks(erp))
        for h in hits:
            if h["source"] != "knowledge base":
                sources.append(h)
        uploaded = [h for h in hits if h["source"] != "knowledge base"]
        if uploaded:
            parts.append("=== RETRIEVED UPLOADED DOCUMENTS ===\n" + "\n".join(
                f"[{h['source']} {h['location']}] {h['text'][:900]}" for h in uploaded))
    context = "\n\n".join(parts)
    return context[:24000], sources


# --------------------------------------------------------------------------- offline answer
def offline_answer(intent: Intent, repo: KnowledgeRepository, version: str = "", settings: dict | None = None) -> tuple[str, dict]:
    """Answer from the structured knowledge base only (no LLM). Returns (markdown, payload)."""
    payload: dict = {}
    if not intent.erp_id:
        return ("Please select or mention an ERP (e.g. *SAP S/4HANA*, *JDE*, *SYSPRO*).", payload)
    erp = repo.get(intent.erp_id)
    md = [f"**ERP:** {erp.name} {version} · storage: `{erp.storage}`"]

    if intent.compare:
        from services.comparison_service import compare
        bos = intent.business_objects or ["sales_order"]
        payload["comparisons"] = []
        for bo in bos[:3]:
            matrix, details = compare(repo, bo, intent.erp_ids)
            payload["comparisons"].append((bo, intent.erp_ids, matrix, details))
        names = ", ".join(repo.get(e).name for e in intent.erp_ids)
        md.append(f"### Cross-ERP comparison: {names}\nObjects: " +
                  ", ".join(repo.business_object_catalog().get(b, {}).get('label', b) for b in bos[:3]))
        md.append("Matrices below (🟢 standard · 🔵 uploaded · 🟡 inferred · 🔴 validate · — missing). Open "
                  "**Cross-ERP Comparison** for highlights, the canonical model and UNION SQL.")
        return "\n\n".join(md), payload

    if intent.tcode:
        res = tcode_analysis(repo, erp.id, intent.tcode, version, settings)
        payload["tcode"] = res
        if not res["found"]:
            md.append(f"### {intent.tcode}: {res['type']}\n{res['message']}")
            return "\n\n".join(md), payload
        md.append(f"### {res['code']} - {res['description']}\n- **Type:** {res['type']}\n- **Program:** {res['program']} "
                  f"({CONFIDENCE.get(res['program_confidence'], {}).get('label', '')})\n- **Business object:** {res['business_object']}")
        if res.get("notes"):
            md.append(f"> {res['notes']}")
        if res.get("historical"):
            md.append(f"**Historical availability:** {res['historical']}")
        md.append("Backend tables, joins and a mapping are shown below - open **T-Code Explorer** for the full 15-point analysis.")
        return "\n\n".join(md), payload

    if intent.columns:
        r = build_mapping(repo, erp.id, intent.columns, intent.business_object, version=version, settings=settings)
        payload["mapping"] = r
        avail = sum(1 for x in r.rows if x["_confidence"] != "unavailable")
        md.append(f"### Source mapping - {r.business_object_label}\nMapped **{avail}/{len(r.rows)}** columns. Anchor/grain table "
                  f"`{r.anchor}` - {r.grain}.")
        if r.warnings:
            md.append("**Warnings:**\n" + "\n".join(f"- {w}" for w in r.warnings))
        md.append("The mapping is loaded into the session - open **Data Model Designer** and **SQL Generator** to continue.")
        return "\n\n".join(md), payload

    if intent.business_object:
        bo = erp.business_objects.get(intent.business_object)
        label = repo.business_object_catalog().get(intent.business_object, {}).get("label", intent.business_object)
        if not bo:
            md.append(f"'{label}' is not modelled for {erp.name} in the knowledge base. Modelled objects: "
                      f"{', '.join(erp.business_objects)}. Upload metadata or use the AI assistant (results flagged).")
            return "\n\n".join(md), payload
        md.append(f"### {label} in {erp.name}\n- **Anchor / grain table:** `{bo.get('anchor')}` - {bo.get('grain')}\n"
                  f"- **Header:** `{bo.get('header', '-')}`\n- **Model type:** {bo.get('model_type')}\n"
                  f"- **Business key:** {', '.join(bo.get('business_key', []))}")
        rows = []
        for t in bo.get("tables", []):
            td = erp.table(t)
            rows.append(f"| `{t}` | {td.get('description', '(not in KB)')} | {', '.join(td.get('primary_key', []) or [])} | "
                        f"{CONFIDENCE.get(td.get('confidence', 'validate'), {}).get('icon', '')} |")
        md.append("| Table | Description | Primary key | Conf. |\n|---|---|---|---|\n" + "\n".join(rows))
        rels = [r for r in erp.relationships if r.get("from") in bo.get("tables", []) and r.get("to") in bo.get("tables", [])]
        if rels:
            md.append("**Relationships**\n\n| From | To | Join | Card. |\n|---|---|---|---|\n" + "\n".join(
                f"| {r['from']} | {r['to']} | " + " AND ".join(f"{a}={b}" for a, b in r.get("on", [])) +
                f" | {r.get('cardinality', '')} |" for r in rels))
        if bo.get("duplicate_risks"):
            md.append("**Duplicate risks**\n" + "\n".join(f"- {d}" for d in bo["duplicate_risks"]))
        cols = default_columns_for(repo, erp.id, intent.business_object)
        if cols:
            payload["mapping"] = build_mapping(repo, erp.id, cols, intent.business_object, version=version, settings=settings)
        return "\n\n".join(md), payload

    if intent.tables:
        md.append("### Tables")
        md.append(table_context(erp, intent.tables).replace("\n", "\n\n"))
        return "\n\n".join(md), payload

    md.append("I couldn't identify a T-code, business object, table or column list in the question. Try e.g.\n"
              "- *ERP: SAP S/4HANA, T-Code: IQ09*\n- *Which tables contain delivery information?*\n"
              "- *I need shipment number, delivery number, material, shipped quantity*\n"
              "- *Compare sales order between SAP, JDE and SYSPRO*\n\nConfigure an LLM in **Settings** for free-form reasoning.")
    return "\n\n".join(md), payload
