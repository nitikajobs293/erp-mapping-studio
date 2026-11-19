from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy import select

from erpdm.constants import TABLE_CLASSES
from erpdm.db.models import (BusinessProcess, BusinessTerm, ERPModule, ERPTable, GeneratedArtifact, Mapping,
                             MappingRow, Transaction, TransactionTable, UseCase)
from erpdm.services.audit import versions_for
from erpdm.services.metadata import get_erp, get_table, list_tables
from erpdm.services.relationships import relationship_rows
from erpdm.services.search import search
from ui import components as ui

ENTITY_TYPES = {"table": "Tables", "transaction": "T-Codes / screens", "relationship": "Relationships",
                "term": "Business terms", "process": "Business processes", "use_case": "Use cases", "erp": "ERP systems"}


def render() -> None:
    st.title("🔎 ERP Knowledge Explorer")
    code = ui.need_erp()
    tab_search, tab_tables, tab_tx, tab_proc, tab_terms = st.tabs(
        ["Search", "Tables & columns", "T-Codes", "Processes & use cases", "Business glossary"])
    with tab_search:
        _search(code)
    if not code:
        return
    with ui.db() as s:
        erp = get_erp(s, code)
        with tab_tables:
            _tables(s, erp)
        with tab_tx:
            rows = [{"T-Code": t.code, "Name": t.name, "Type": t.transaction_type,
                     "Module": t.module.code if t.module else "", "Business object": t.business_object,
                     "Tables": ", ".join(l.table.name for l in t.table_links),
                     "Validation": ui.status_badge(t.validation_status, t.confidence)}
                    for t in s.scalars(select(Transaction).where(Transaction.erp_id == erp.id).order_by(Transaction.code))]
            ui.df(rows) if rows else st.info("No transactions/screens registered for this ERP yet.")
        with tab_proc:
            for p in s.scalars(select(BusinessProcess).where(BusinessProcess.erp_id == erp.id)):
                with st.expander(f"**{p.name}** · {p.domain or ''}"):
                    st.write(p.description or "")
                    if p.flow_steps:
                        st.markdown(" → ".join(f"`{x}`" for x in p.flow_steps))
            st.subheader("Use cases")
            ui.df([{"Use case": u.name, "Domain": u.domain, "Grain": u.grain, "Tables": ", ".join(u.tables or []),
                    "Optional": ", ".join(u.optional_tables or []), "Root": u.root_table}
                   for u in s.scalars(select(UseCase).where(UseCase.erp_id == erp.id))])
        with tab_terms:
            ui.df([{"Term": t.term, "Synonyms": ", ".join(t.synonyms or []), "Source": f"{t.table_name}.{t.column_name}",
                    "Definition": t.definition, "Lookup": t.lookup_logic,
                    "Validation": ui.status_badge(t.validation_status, t.confidence)}
                   for t in s.scalars(select(BusinessTerm).where(BusinessTerm.erp_id == erp.id).order_by(BusinessTerm.term))])


def _search(code: str | None) -> None:
    c1, c2, c3 = st.columns([3, 2, 1])
    q = c1.text_input("Search tables, columns, T-codes, processes, terms…",
                      placeholder="e.g. serial number, delivery header, IQ09, MATNR, procurement")
    types = c2.multiselect("Entity types", list(ENTITY_TYPES), format_func=ENTITY_TYPES.get)
    scope_all = c3.toggle("All ERPs", value=not bool(code))
    if not q:
        st.caption("Search runs over the metadata repository (PostgreSQL full-text search; exact names rank first).")
        return
    with ui.db() as s:
        erp = get_erp(s, code) if code and not scope_all else None
        page_size = 25
        page = st.number_input("Page", min_value=1, value=1, step=1, key="search_page")
        hits = search(s, q, erp_id=erp.id if erp else None, entity_types=types or None,
                      limit=page_size, offset=(page - 1) * page_size)
        if not hits:
            st.info("No matches. Try other terms, or upload metadata in Metadata Management.")
            return
        from erpdm.db.models import ERPSystem
        erp_names = {e.id: e.code for e in s.scalars(select(ERPSystem))}
        ui.df([{"Type": ENTITY_TYPES.get(h.entity_type, h.entity_type), "ERP": erp_names.get(h.erp_id, ""),
                "Title": h.title, "Snippet": h.snippet, "Validation": ui.status_badge(h.validation_status),
                "Score": round(h.score, 3)} for h in hits])


def _tables(s, erp) -> None:
    mods = [m.code for m in s.scalars(select(ERPModule).where(ERPModule.erp_id == erp.id).order_by(ERPModule.code))]
    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    text = c1.text_input("Filter by name/description", key="tbl_filter")
    module = c2.selectbox("Module", [""] + mods, key="tbl_mod")
    tclass = c3.selectbox("Class", [""] + TABLE_CLASSES, key="tbl_cls")
    status = c4.selectbox("Validation", ["", "verified", "requires_validation", "pending"], key="tbl_status")
    page = int(st.session_state.get("tbl_page_in", 1))
    rows, total = list_tables(s, erp.id, module or None, tclass or None, text or None, status or None, page, 50)
    if total == 0 and page > 1:
        page = 1
        rows, total = list_tables(s, erp.id, module or None, tclass or None, text or None, status or None, 1, 50)
    if total == 0:
        st.info(f"No tables for {erp.name}. " + ("Metadata onboarding required — upload DDL, an inventory or a package "
                                                 "in Metadata Management." if not text else ""))
        return
    pages = (total + 49) // 50
    st.caption(f"{total} tables · page {page}/{pages}")
    if pages > 1:
        st.number_input("Page", 1, pages, key="tbl_page_in")
    ui.df([{"Table": t.name, "Description": t.description, "Module": t.module.code if t.module else "",
            "Class": t.table_class, "Key": ", ".join(t.primary_key), "Columns": len(t.columns),
            "Validation": ui.status_badge(t.validation_status, t.confidence)} for t in rows])
    name = st.selectbox("Open table", [t.name for t in rows], index=None, placeholder="Choose a table…")
    if not name:
        return
    t = get_table(s, erp.id, name)
    st.subheader(f"{t.name} — {t.description or ''}")
    m = st.columns(4)
    m[0].metric("Class", t.table_class)
    m[1].metric("Columns", len(t.columns))
    m[2].metric("Primary key", ", ".join(t.primary_key) or "—")
    m[3].metric("Validation", t.validation_status.replace("_", " "))
    st.caption(f"Source: {ui.trusted_label(t.source_type)} · confidence {t.confidence:.2f} · "
               f"{t.source_reference or ''} · updated by {t.updated_by or 'system'} · version {t.version}")
    if t.notes:
        st.info(t.notes)
    if t.change_tracking or t.delete_strategy or t.soft_delete_rule:
        st.markdown(f"**Change tracking:** {t.change_tracking or '—'}  \n**Deletes:** {t.delete_strategy or '—'}  \n"
                    f"**Soft-delete rule:** `{t.soft_delete_rule or '—'}`  \n**Default filter:** `{t.default_filter or '—'}`")
    tabs = st.tabs(["Columns", "Relationships", "Used by (lineage)", "Version history"])
    with tabs[0]:
        ui.df([{"#": c.ordinal, "Column": c.name, "PK": "🔑" if c.is_pk else "", "Type": c.data_type,
                "Length": c.length, "Decimals": c.decimals, "Nullable": c.is_nullable, "Description": c.description,
                "Business name": c.business_name, "Check table": c.check_table,
                "Validation": ui.status_badge(c.validation_status, c.confidence)} for c in t.columns])
    with tabs[1]:
        rels = [r for r in relationship_rows(s, erp.id) if t.name in (r["Source table"], r["Target table"])]
        ui.df(rels) if rels else st.caption("No relationships stored for this table.")
    with tabs[2]:
        txs = s.execute(select(Transaction.code, Transaction.name, TransactionTable.role)
                        .join(TransactionTable, TransactionTable.transaction_id == Transaction.id)
                        .where(TransactionTable.table_id == t.id)).all()
        ucs = [u.name for u in s.scalars(select(UseCase).where(UseCase.erp_id == erp.id))
               if t.name in (u.tables or []) + (u.optional_tables or [])]
        maps = s.execute(select(Mapping.name, MappingRow.business_column, MappingRow.target_table, MappingRow.target_column)
                         .join(MappingRow, MappingRow.mapping_id == Mapping.id)
                         .where(Mapping.erp_id == erp.id, MappingRow.source_table == t.name)).all()
        models = [a.name for a in s.scalars(select(GeneratedArtifact).where(
            GeneratedArtifact.erp_id == erp.id, GeneratedArtifact.kind == "dbt_model"))
            if a.content and (t.name.lower() in a.name or f"'{t.name}'" in (a.content or ""))]
        st.markdown("**Transactions:** " + (", ".join(f"{c} ({r})" for c, _, r in txs) or "—"))
        st.markdown("**Use cases:** " + (", ".join(ucs) or "—"))
        st.markdown("**Generated dbt models:** " + (", ".join(sorted(set(models))) or "—"))
        if maps:
            st.markdown("**Mapped to targets:**")
            ui.df(pd.DataFrame(maps, columns=["Mapping", "Business column", "Target table", "Target column"]))
    with tabs[3]:
        ui.df([{"Version": v.version, "Change": v.change_type, "By": v.changed_by,
                "At": v.changed_at.strftime("%Y-%m-%d %H:%M"), "Status": v.snapshot.get("validation_status")}
               for v in versions_for(s, "erp_table", t.id)])
