"""Reusable UI blocks for the connected workflow (column picker, plan, ER diagram, send-to-workflow)."""
from __future__ import annotations

import streamlit as st

from erpdm.schemas import JoinPlan, SelectedColumn
from erpdm.services.diagrams import er_diagram_dot
from erpdm.services.metadata import get_table
from erpdm.services.relationships import plan_joins
from ui import components as ui


def column_picker(session, erp, tables: list[str], selected: list[SelectedColumn], key: str) -> list[SelectedColumn]:
    chosen: list[SelectedColumn] = []
    sel_set = {(c.table.upper(), c.column.upper()) for c in selected}
    biz = {(c.table.upper(), c.column.upper()): c.business_name for c in selected}
    cols = st.columns(2)
    for i, tname in enumerate(tables):
        t = get_table(session, erp.id, tname)
        if t is None:
            continue
        options = [c.name for c in t.columns]
        labels = {c.name: f"{c.name}{' 🔑' if c.is_pk else ''} — {c.business_name or c.description or ''}" for c in t.columns}
        default = [c.name for c in t.columns if (t.name.upper(), c.name.upper()) in sel_set]
        with cols[i % 2]:
            picked = st.multiselect(f"{t.name} · {t.description or ''}", options, default=default,
                                    format_func=lambda x, l=labels: l[x], key=f"{key}_{t.name}")
        for p in picked:
            chosen.append(SelectedColumn(table=t.name, column=p, business_name=biz.get((t.name.upper(), p.upper()))))
    return chosen


def replan(session, erp, columns: list[SelectedColumn], root: str | None, extra_tables: list[str] | None = None,
           include_pending: bool = False, prefer: list[int] | None = None) -> JoinPlan:
    tables = list(dict.fromkeys(([root] if root else []) + [c.table for c in columns] + (extra_tables or [])))
    return plan_joins(session, erp, tables, root=root, include_pending=include_pending, prefer_ids=set(prefer or []))


def plan_table(plan: JoinPlan) -> None:
    ui.df([{"From": e.left_table, "To": e.right_table, "Join": e.join_type, "Condition": e.condition_sql,
            "Cardinality": e.cardinality, "Type": e.relationship_type, "Fan-out": "⚠" if e.fans_out else "",
            "Purpose": e.business_purpose, "Source": e.source_type,
            "Validation": ui.status_badge(e.validation_status, e.confidence)} for e in plan.edges])
    for w in plan.warnings:
        st.caption(f"⚠ {w}")


def er_diagram(session, erp, plan: JoinPlan, columns: list[SelectedColumn] | None = None) -> str:
    selected: dict[str, list[str]] = {}
    for c in columns or []:
        selected.setdefault(c.table, []).append(c.column)
    dot = er_diagram_dot(session, erp, plan.tables, plan, selected)
    ui.graphviz(dot)
    st.caption("Bold edges = joins used in the plan. Green solid = verified · orange dashed = requires validation · "
               "red dotted = pending.")
    return dot


def send_to_workflow(plan: JoinPlan, columns: list[SelectedColumn], *, tcode=None, use_case_text=None,
                     use_case_name=None, sql: str | None = None, sql_meta: dict | None = None, key: str = "send") -> None:
    if st.button("➡️ Use this scope in the workflow (SQL · Mapping · dbt)", type="primary", key=key):
        w = ui.wf()
        w.set_scope(plan.tables, plan.root_table, columns, tcode=tcode, use_case_text=use_case_text,
                    use_case_name=use_case_name, plan=plan)
        w.sql, w.sql_meta = sql, sql_meta
        st.success("Saved to workflow. Continue in **SQL Generator**, **Column Mapping** or **dbt Model Generator**.")
