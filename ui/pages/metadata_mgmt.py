from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy import select

from erpdm.constants import CARDINALITIES, RELATIONSHIP_TYPES, SourceType, ValidationStatus
from erpdm.db.models import BusinessTerm, Document, ERPTable, MetadataVersion, Relationship, Transaction
from erpdm.schemas import ERPSystemIn, JoinPair, RelationshipIn
from erpdm.services.audit import recent_audit
from erpdm.services.ingestion import (KINDS, IngestionError, commit_upload, package_to_yaml, parse_upload)
from erpdm.services.metadata import (MetadataError, check_relationship, export_package, get_erp, get_table,
                                     list_erps, set_entity_status, set_relationship_status, upsert_erp,
                                     upsert_relationship)
from erpdm.services.sqlgen import snake
from ui import components as ui

DIALECTS = {"Generic / auto": None, "Snowflake": "snowflake", "Oracle": "oracle", "SQL Server": "tsql",
            "PostgreSQL": "postgres", "SAP HANA / other": None}


def render() -> None:
    st.title("🛠️ Metadata Management")
    if not ui.require("manage_metadata"):
        return
    tabs = st.tabs(["Upload & ingest", "ERP systems", "Validation queue", "Add relationship", "Documents",
                    "Versions & audit", "Export package"])
    with tabs[0]:
        _upload()
    with tabs[1]:
        _erp_systems()
    with tabs[2]:
        _validation_queue()
    with tabs[3]:
        _add_relationship()
    with tabs[4]:
        _documents()
    with tabs[5]:
        _versions()
    with tabs[6]:
        _export()


def _upload() -> None:
    st.markdown("Upload **DDL, SAP DDIC extracts (DD03L/DD05S), table inventories, FK lists, T-code lists, mapping "
                "workbooks, existing SQL/dbt, metadata packages (YAML/JSON) or documentation (PDF/DOCX/TXT)**. "
                "Files are parsed into a preview first; nothing is written until you commit.")
    w = ui.wf()
    with ui.db() as s:
        erps = [e.code for e in list_erps(s)]
    c1, c2, c3 = st.columns(3)
    erp_code = c1.selectbox("Target ERP", erps, index=erps.index(w.erp_code) if w.erp_code in erps else 0,
                            help="Packages with their own `erp:` section create/update that ERP instead.")
    kind = c2.selectbox("File type", list(KINDS), format_func=KINDS.get)
    dialect = DIALECTS[c3.selectbox("SQL dialect (DDL / SQL)", list(DIALECTS))]
    trusted = st.checkbox("This is an authoritative extract from the actual system (marks structure as verified)",
                          help="Use for DDIC/DDL extracts from the real ERP. Joins from DDL/DD05S foreign keys are always "
                               "treated as database constraints.")
    files = st.file_uploader("Files", accept_multiple_files=True,
                             type=["xlsx", "xls", "csv", "pdf", "docx", "sql", "json", "yaml", "yml", "txt", "md"])
    if not files:
        return
    for f in files:
        with st.container(border=True):
            st.markdown(f"**{f.name}** · {f.size / 1024:.1f} KB")
            try:
                outcome = parse_upload(f.name, f.getvalue(), kind, dialect, trusted)
            except IngestionError as e:
                st.error(str(e))
                continue
            st.caption(f"Detected: {KINDS.get(outcome.kind, outcome.kind)}")
            pkg = outcome.package
            if pkg:
                if pkg.erp:
                    st.info(f"Package targets ERP **{pkg.erp.code}** ({pkg.erp.name}).")
                st.write(pkg.summary())
                if pkg.tables:
                    ui.df([{"Table": t.name, "Columns": len(t.columns), "Key": ", ".join(c.name for c in t.columns if c.is_pk),
                            "Source": t.source_type or pkg.defaults.get("source_type"), "Status": t.validation_status
                            or pkg.defaults.get("validation_status")} for t in pkg.tables[:200]])
                if pkg.relationships:
                    ui.df([{"From": r.from_table, "To": r.to_table,
                            "Join": ", ".join(f"{j.from_}={j.to}" for j in r.join_columns), "Card.": r.cardinality,
                            "Source": r.source_type, "Status": r.validation_status} for r in pkg.relationships[:200]])
                for wmsg in pkg.warnings[:20]:
                    st.caption(f"⚠ {wmsg}")
            if outcome.document_text:
                st.text_area("Extracted text (preview)", outcome.document_text[:3000], height=140, key=f"prev_{f.name}")
            if st.button(f"Commit {f.name}", key=f"commit_{f.name}", type="primary"):
                with ui.db() as s:
                    erp = get_erp(s, erp_code)
                    try:
                        result, doc_id = commit_upload(s, outcome, f.name, f.getvalue(), erp_code, erp.id if erp else None,
                                                       ui.user().username, trusted)
                    except MetadataError as e:
                        st.error(str(e))
                        continue
                if result:
                    st.success(f"Imported. Created: {result.created} · Updated: {result.updated}")
                    for msg in result.skipped[:30]:
                        st.caption(f"Skipped: {msg}")
                if doc_id:
                    st.success(f"Indexed for retrieval (document #{doc_id}).")
                st.cache_data.clear()


def _erp_systems() -> None:
    with ui.db() as s:
        erps = list_erps(s, active_only=False)
        ui.df([{"Code": e.code, "Name": e.name, "Vendor": e.vendor, "Version": e.version_label, "Category": e.category,
                "Client column": e.client_column, "Extraction": e.extraction_method, "Active": e.is_active,
                "Notes": e.landscape_notes} for e in erps])
        st.markdown("**Add or update an ERP system** — no code changes needed; then upload its metadata.")
        codes = ["<new>"] + [e.code for e in erps]
        pick = st.selectbox("ERP", codes)
        cur = get_erp(s, pick) if pick != "<new>" else None
        with st.form("erp_form"):
            c1, c2, c3 = st.columns(3)
            code = c1.text_input("Code", value=cur.code if cur else "", disabled=bool(cur))
            name = c2.text_input("Name", value=cur.name if cur else "")
            vendor = c3.text_input("Vendor", value=(cur.vendor if cur else "") or "")
            c4, c5, c6 = st.columns(3)
            version = c4.text_input("Version", value=(cur.version_label if cur else "") or "")
            cats = ["ERP", "WMS", "TMS", "SCM", "Other"]
            category = c5.selectbox("Category", cats, index=cats.index(cur.category) if cur and cur.category in cats else 0)
            lang = c6.text_input("Default language key", value=(cur.default_language_key if cur else "") or "")
            c7, c8 = st.columns(2)
            has_client = c7.checkbox("Tables carry a client/tenant column", value=bool(cur and cur.has_client_column))
            client_col = c8.text_input("Client column", value=(cur.client_column if cur else "") or "")
            extraction = st.text_input("Extraction method", value=(cur.extraction_method if cur else "") or "")
            notes = st.text_area("Landscape notes", value=(cur.landscape_notes if cur else "") or "")
            active = st.checkbox("Active", value=cur.is_active if cur else True)
            if st.form_submit_button("Save ERP system", type="primary"):
                try:
                    erp, created = upsert_erp(s, ERPSystemIn(
                        code=cur.code if cur else code, name=name, vendor=vendor or None, version_label=version or None,
                        category=category, landscape_notes=notes or None, extraction_method=extraction or None,
                        default_language_key=lang or None, has_client_column=has_client,
                        client_column=client_col or None), ui.user().username)
                    erp.is_active = active
                    st.success(("Created " if created else "Updated ") + erp.code)
                    st.cache_data.clear()
                except Exception as e:  # pydantic validation
                    st.error(str(e))


def _validation_queue() -> None:
    if not ui.can("validate_metadata"):
        st.info("Only administrators can validate metadata.")
        return
    code = ui.need_erp()
    if not code:
        return
    with ui.db() as s:
        erp = get_erp(s, code)
        c1, c2 = st.columns(2)
        statuses = c1.multiselect("Status", ["pending", "requires_validation"], default=["pending", "requires_validation"])
        sources = c2.multiselect("Source", [x.value for x in SourceType])
        q = select(Relationship).where(Relationship.erp_id == erp.id, Relationship.validation_status.in_(statuses))
        if sources:
            q = q.where(Relationship.source_type.in_(sources))
        rels = list(s.scalars(q.order_by(Relationship.confidence)))
        st.caption(f"{len(rels)} relationships awaiting review (lowest confidence first)")
        frame = pd.DataFrame([{"Select": False, "id": r.id, "From": r.from_table.name, "To": r.to_table.name,
                               "Join": ", ".join(f"{j['from']}={j['to']}" for j in r.join_columns),
                               "Extra": r.extra_condition, "Card.": r.cardinality, "Type": r.relationship_type,
                               "Source": r.source_type, "Confidence": r.confidence, "Status": r.validation_status,
                               "Evidence": r.evidence} for r in rels])
        if frame.empty:
            st.success("Nothing to review.")
        else:
            edited = st.data_editor(frame, hide_index=True, width="stretch",
                                    disabled=[c for c in frame.columns if c != "Select"], key="vq")
            chosen = edited.loc[edited["Select"], "id"].tolist()
            note = st.text_input("Evidence / note for this decision", placeholder="e.g. confirmed in SE11 / DD05S on PRD")
            b1, b2 = st.columns(2)
            if b1.button(f"✅ Approve {len(chosen)}", disabled=not chosen):
                for rid in chosen:
                    set_relationship_status(s, int(rid), "verified", ui.user().username, note or None)
                st.success("Approved.")
                st.rerun()
            if b2.button(f"⛔ Reject {len(chosen)}", disabled=not chosen):
                for rid in chosen:
                    set_relationship_status(s, int(rid), "rejected", ui.user().username, note or None)
                st.success("Rejected.")
                st.rerun()
        st.divider()
        st.markdown("**Validate tables, T-codes and glossary terms**")
        kind = st.radio("Entity", ["Tables", "T-Codes", "Glossary terms"], horizontal=True)
        model = {"Tables": ERPTable, "T-Codes": Transaction, "Glossary terms": BusinessTerm}[kind]
        label = {"Tables": "name", "T-Codes": "code", "Glossary terms": "term"}[kind]
        items = list(s.scalars(select(model).where(model.erp_id == erp.id,
                                                   model.validation_status != "verified").limit(500)))
        if not items:
            st.success("All verified.")
            return
        pick = st.multiselect(f"{kind} to update", [i.id for i in items],
                              format_func=lambda i: next(f"{getattr(x, label)} ({x.validation_status}, {x.confidence:.2f})"
                                                         for x in items if x.id == i))
        status = st.selectbox("Set status", [x.value for x in ValidationStatus], key="ent_status")
        if pick and st.button("Apply status"):
            for i in pick:
                set_entity_status(s, model, i, status, ui.user().username)
            st.success("Updated.")
            st.rerun()


def _add_relationship() -> None:
    code = ui.need_erp()
    if not code:
        return
    with ui.db() as s:
        erp = get_erp(s, code)
        names = list(s.scalars(select(ERPTable.name).where(ERPTable.erp_id == erp.id).order_by(ERPTable.name)))
        if len(names) < 2:
            st.info("Need at least two tables.")
            return
        c1, c2 = st.columns(2)
        ft = c1.selectbox("From table", names, key="ar_from")
        tt = c2.selectbox("To table", names, index=1, key="ar_to")
        fcols = [c.name for c in get_table(s, erp.id, ft).columns]
        tcols = [c.name for c in get_table(s, erp.id, tt).columns]
        n = st.number_input("Number of join column pairs", 1, 6, 2 if erp.client_column else 1)
        pairs = []
        for i in range(int(n)):
            a, b = st.columns(2)
            pairs.append((a.selectbox(f"{ft} column #{i + 1}", fcols, key=f"ar_f{i}"),
                          b.selectbox(f"{tt} column #{i + 1}", tcols, key=f"ar_t{i}")))
        c3, c4, c5, c6 = st.columns(4)
        jt = c3.selectbox("Join type", ["LEFT", "INNER"])
        card = c4.selectbox("Cardinality (from → to)", CARDINALITIES, index=2)
        rtype = c5.selectbox("Relationship type", RELATIONSHIP_TYPES, index=4)
        src = c6.selectbox("Source", [SourceType.USER_VERIFIED.value, SourceType.TRUSTED_DOCUMENTATION.value,
                                      SourceType.STANDARD_ERP_KNOWLEDGE.value, SourceType.UNVERIFIED_ASSUMPTION.value])
        extra = st.text_input("Extra condition (use {from}/{to} for aliases, {lang} for language)", placeholder="{to}.SPRAS = '{lang}'")
        desc = st.text_input("Description")
        purpose = st.text_input("Business purpose")
        evidence = st.text_input("Evidence (where was this confirmed?)")
        rel = RelationshipIn(from_table=ft, to_table=tt, join_columns=[JoinPair(**{"from": a, "to": b}) for a, b in pairs],
                             extra_condition=extra or None, join_type=jt, cardinality=card, relationship_type=rtype,
                             is_mandatory=jt == "INNER", description=desc or None, business_purpose=purpose or None,
                             evidence=evidence or None, source_type=src,
                             confidence=1.0 if src == "user_verified" else 0.8 if src != "unverified_assumption" else 0.3,
                             validation_status="verified" if src == "user_verified" else "requires_validation"
                             if src != "unverified_assumption" else "pending")
        errors, warns = check_relationship(s, erp, rel)
        for e in errors:
            st.error(e)
        for w_ in warns:
            st.warning(w_)
        if st.button("Save relationship", type="primary", disabled=bool(errors)):
            r = upsert_relationship(s, erp, rel, ui.user().username)
            st.success(f"Saved relationship #{r.id} ({r.validation_status}).")


def _documents() -> None:
    with ui.db() as s:
        docs = list(s.scalars(select(Document).order_by(Document.uploaded_at.desc()).limit(200)))
        ui.df([{"id": d.id, "File": d.filename, "Type": d.file_type, "Kind": d.doc_kind, "Trusted": d.is_trusted,
                "Chunks": len(d.chunks), "By": d.uploaded_by, "At": d.uploaded_at.strftime("%Y-%m-%d %H:%M")} for d in docs])
        if docs and ui.can("manage_metadata"):
            did = st.selectbox("Delete document from the retrieval index", [d.id for d in docs],
                               format_func=lambda i: next(d.filename for d in docs if d.id == i), index=None)
            if did and st.button("Delete"):
                s.delete(s.get(Document, did))
                st.success("Deleted.")
                st.rerun()


def _versions() -> None:
    with ui.db() as s:
        c1, c2 = st.columns(2)
        etype = c1.selectbox("Entity type", ["erp_table", "relationship", "transaction", "mapping", "erp_system",
                                             "erp_tables", "transactions", "business_terms"])
        eid = c2.number_input("Entity id (0 = latest changes)", 0, step=1)
        q = select(MetadataVersion).where(MetadataVersion.entity_type == etype)
        if eid:
            q = q.where(MetadataVersion.entity_id == eid)
        rows = list(s.scalars(q.order_by(MetadataVersion.changed_at.desc()).limit(200)))
        ui.df([{"Entity": v.entity_id, "Version": v.version, "Change": v.change_type, "By": v.changed_by,
                "At": v.changed_at.strftime("%Y-%m-%d %H:%M:%S"), "Name": v.snapshot.get("name") or v.snapshot.get("code"),
                "Status": v.snapshot.get("validation_status") or v.snapshot.get("status")} for v in rows])
        if eid and rows:
            st.json(rows[0].snapshot)
        st.subheader("Audit trail")
        ui.df([{"When": a.ts.strftime("%Y-%m-%d %H:%M:%S"), "User": a.username, "Action": a.action,
                "Entity": f"{a.entity_type}:{a.entity_id}", "Details": str(a.details or "")} for a in recent_audit(s, 300)])


def _export() -> None:
    code = ui.need_erp()
    if not code:
        return
    with ui.db() as s:
        erp = get_erp(s, code)
        if st.button("Build metadata package"):
            st.session_state["export_yaml"] = package_to_yaml(export_package(s, erp))
    y = st.session_state.get("export_yaml")
    if y:
        st.download_button("⬇ Download package (.yaml)", y, file_name=f"{snake(code)}_metadata.yaml")
        st.code(y[:5000], language="yaml")
