from __future__ import annotations

import streamlit as st
from sqlalchemy import select

from erpdm.constants import SourceType, ValidationStatus
from erpdm.db.models import ERPTable
from erpdm.schemas import JoinPair, RelationshipIn
from erpdm.services.assistant import suggest_relationships
from erpdm.services.diagrams import er_diagram_mermaid, render_png
from erpdm.services.llm import LLMClient, LLMError
from erpdm.services.metadata import get_erp, set_relationship_status, upsert_relationship
from erpdm.services.relationships import PlanningError, discover_candidates, plan_joins, relationship_rows
from ui import components as ui
from ui import workflow_ui as wui


def render() -> None:
    st.title("🔗 Table Relationship & Join Intelligence")
    code = ui.need_erp()
    if not code:
        return
    w = ui.wf()
    with ui.db() as s:
        erp = get_erp(s, code)
        all_tables = [t for t in s.scalars(select(ERPTable.name).where(ERPTable.erp_id == erp.id).order_by(ERPTable.name))]
        if not all_tables:
            st.info("No tables for this ERP yet — onboard metadata first.")
            return
        default = [t for t in w.tables if t in all_tables] or all_tables[:2]
        c1, c2 = st.columns([3, 1])
        tables = c1.multiselect("Tables", all_tables, default=default)
        root = c2.selectbox("Root / grain table", tables, index=tables.index(w.root_table) if w.root_table in tables else 0) if tables else None
        c3, c4 = st.columns([1, 3])
        include_pending = c3.toggle("Include pending joins", value=w.include_pending)
        rows_all = relationship_rows(s, erp.id, tables) if tables else []
        prefer = c4.multiselect("Pin relationships (force the planner to use them)", [r["id"] for r in rows_all],
                                default=[i for i in w.prefer_relationship_ids if i in [r["id"] for r in rows_all]],
                                format_func=lambda i: next(f"#{r['id']} {r['Source table']}→{r['Target table']} ({r['Join columns']})"
                                                           for r in rows_all if r["id"] == i))
        ui.provenance_legend()
        if not tables:
            return
        try:
            plan = plan_joins(s, erp, tables, root=root, include_pending=include_pending, prefer_ids=set(prefer))
        except PlanningError as e:
            st.error(str(e))
            return
        tabs = st.tabs(["Join plan", "ER diagram", "All relationships & validation", "Discover / suggest"])
        with tabs[0]:
            wui.plan_table(plan)
            if plan.unreachable:
                st.error(f"Unreachable with trusted joins: {plan.unreachable}")
            if st.button("Use these tables & joins in the workflow", key="rel_wf"):
                w.tables, w.root_table, w.plan = plan.tables, plan.root_table, plan
                w.include_pending, w.prefer_relationship_ids = include_pending, list(prefer)
                w.columns = [c for c in w.columns if c.table in plan.tables]
                st.success("Workflow updated.")
        with tabs[1]:
            dot = wui.er_diagram(s, erp, plan, w.columns)
            png = render_png(dot)
            d1, d2, d3 = st.columns(3)
            if png:
                d1.download_button("ER diagram (PNG)", png, file_name=f"{erp.code}_er.png", mime="image/png")
            d2.download_button("Graphviz DOT", dot, file_name=f"{erp.code}_er.dot")
            d3.download_button("Mermaid", er_diagram_mermaid(s, erp, plan.tables), file_name=f"{erp.code}_er.mmd")
        with tabs[2]:
            rows = relationship_rows(s, erp.id, tables, include_rejected=True)
            ui.df(rows)
            if ui.can("validate_metadata") and rows:
                st.markdown("**Validate a relationship**")
                v1, v2, v3 = st.columns([2, 1, 2])
                rid = v1.selectbox("Relationship", [r["id"] for r in rows],
                                   format_func=lambda i: next(f"#{r['id']} {r['Source table']}→{r['Target table']} "
                                                              f"[{r['Validation']}]" for r in rows if r["id"] == i))
                status = v2.selectbox("New status", [x.value for x in ValidationStatus])
                note = v3.text_input("Evidence / note", placeholder="e.g. checked in SE11 on PRD")
                if st.button("Apply", key="rel_apply"):
                    set_relationship_status(s, rid, status, ui.user().username, note or None)
                    st.success("Saved and audited.")
                    st.rerun()
        with tabs[3]:
            st.markdown("**Name-based candidate discovery** — suggestions only. A column whose name equals the single "
                        "key of another table *might* be a foreign key. Candidates are never used for SQL until approved.")
            if st.button("Find candidates", key="cand"):
                st.session_state["candidates"] = [c.__dict__ for c in discover_candidates(s, erp)]
            cands = [c for c in st.session_state.get("candidates", [])
                     if c["from_table"] in tables or c["to_table"] in tables or not tables]
            if cands:
                ui.df([{"From": c["from_table"], "To": c["to_table"],
                        "Join": ", ".join(f"{j['from']}={j['to']}" for j in c["join_columns"]),
                        "Reason": c["reason"], "Confidence": c["confidence"]} for c in cands])
                if ui.can("manage_metadata"):
                    idx = st.multiselect("Store as pending suggestions", range(len(cands)),
                                         format_func=lambda i: f"{cands[i]['from_table']}→{cands[i]['to_table']}")
                    if idx and st.button("Store selected", key="cand_store"):
                        for i in idx:
                            c = cands[i]
                            upsert_relationship(s, erp, RelationshipIn(
                                from_table=c["from_table"], to_table=c["to_table"],
                                join_columns=[JoinPair(**j) for j in c["join_columns"]], cardinality="N:1",
                                relationship_type="lookup", description=c["reason"],
                                source_type=SourceType.UNVERIFIED_ASSUMPTION.value, confidence=c["confidence"],
                                validation_status=ValidationStatus.PENDING.value), ui.user().username)
                        st.success(f"Stored {len(idx)} suggestion(s) as pending.")
            elif "candidates" in st.session_state:
                st.caption("No candidates found for the selected tables.")
            st.divider()
            llm = LLMClient()
            st.markdown(f"**AI relationship suggestions** · provider: `{llm.describe()}`")
            if not llm.enabled:
                st.caption("Configure LLM_PROVIDER / LLM_API_KEY to enable. Suggestions are validated against the "
                           "repository and stored as `ai_inferred` / pending.")
            elif ui.can("manage_metadata") and st.button("Ask AI to suggest joins among the selected tables"):
                try:
                    n, rejected = suggest_relationships(s, erp, tables, ui.user().username, llm)
                    st.success(f"Stored {n} AI suggestion(s) as pending.")
                    for r in rejected:
                        st.caption(f"Rejected: {r}")
                except LLMError as e:
                    st.error(str(e))
