from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import func, select

from erpdm.db.models import GeneratedArtifact, Relationship
from erpdm.services.audit import recent_audit
from erpdm.services.metadata import dashboard_stats, per_erp_stats
from ui import components as ui


@st.cache_data(ttl=30, show_spinner=False)
def _stats() -> tuple[dict, list[dict], list[dict], list[dict], list[dict]]:
    with ui.db() as s:
        stats = dashboard_stats(s)
        per = per_erp_stats(s)
        status = [{"Status": st_, "Count": n} for st_, n in s.execute(
            select(Relationship.validation_status, func.count()).group_by(Relationship.validation_status)).all()]
        audit = [{"When": a.ts.strftime("%Y-%m-%d %H:%M"), "User": a.username, "Action": a.action,
                  "Entity": f"{a.entity_type or ''}:{a.entity_id or ''}"} for a in recent_audit(s, 15)]
        arts = [{"When": a.created_at.strftime("%Y-%m-%d %H:%M"), "Kind": a.kind, "Name": a.name, "By": a.created_by}
                for a in s.scalars(select(GeneratedArtifact).order_by(GeneratedArtifact.created_at.desc()).limit(10))]
    return stats, per, status, audit, arts


def render() -> None:
    st.title("📊 Dashboard")
    ui.workflow_banner()
    stats, per, status, audit, arts = _stats()
    c = st.columns(4)
    c[0].metric("ERP systems", stats["erp_systems"])
    c[1].metric("Modules", stats["modules"])
    c[2].metric("Tables", stats["tables"])
    c[3].metric("Columns", stats["columns"])
    c = st.columns(4)
    c[0].metric("T-Codes / screens", stats["transactions"])
    c[1].metric("Business processes", stats["processes"])
    c[2].metric("Mappings generated", stats["mappings"])
    c[3].metric("dbt models generated", stats["dbt_models"])
    c = st.columns(4)
    c[0].metric("Relationships", stats["relationships"])
    c[1].metric("Verified joins", stats["relationships_verified"])
    c[2].metric("Joins awaiting validation", stats["relationships_pending"])
    c[3].metric("SQL scripts generated", stats["sql_scripts"])

    left, right = st.columns([1.6, 1])
    with left:
        st.subheader("Repository coverage by ERP")
        frame = pd.DataFrame(per)
        if not frame.empty:
            fig = px.bar(frame.sort_values("Tables"), x="Tables", y="ERP", orientation="h", color="Status",
                         color_discrete_map={"Active": "#2f6db5", "Metadata onboarding required": "#c9ced6"},
                         height=420)
            fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), legend_title_text="")
            st.plotly_chart(fig, width="stretch")
    with right:
        st.subheader("Join validation status")
        if status:
            fig = px.pie(pd.DataFrame(status), names="Status", values="Count", hole=0.55, color="Status",
                         color_discrete_map={"verified": "#1b7f3b", "requires_validation": "#e3a008",
                                             "pending": "#d14343", "rejected": "#9ca3af"}, height=420)
            fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig, width="stretch")
    ui.df(per)
    a, b = st.columns(2)
    with a:
        st.subheader("Recent activity")
        ui.df(audit)
    with b:
        st.subheader("Recently generated")
        ui.df(arts) if arts else st.caption("Nothing generated yet.")
    ui.provenance_legend()
