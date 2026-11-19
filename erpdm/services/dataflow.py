"""ERP data-flow / architecture views: process → transaction → tables → extraction → Snowflake → dbt → BI."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from erpdm.db.models import BusinessProcess, ERPSystem, Transaction, UseCase
from erpdm.services.diagrams import data_flow_dot
from erpdm.services.metadata import get_table
from erpdm.services.sqlgen import snake


def predicted_models(erp: ERPSystem, tables: list[str], entity: str | None, dbt_models: list[str] | None = None) -> dict:
    if dbt_models:
        return {"stg": [m for m in dbt_models if m.startswith("stg_")],
                "int": [m for m in dbt_models if m.startswith("int_")] or ["intermediate"],
                "mart": [m for m in dbt_models if m.startswith(("dim_", "fct_", "snap_"))] or ["marts"]}
    src = snake(erp.code)
    return {"stg": [f"stg_{src}__{t.lower()}" for t in tables],
            "int": [f"int_{entity or 'entity'}__joined"],
            "mart": [f"dim_{entity}" if entity else "dim / fct models"]}


def flow_for_transaction(session: Session, erp: ERPSystem, tx: Transaction, tables: list[str], wf=None) -> str:
    root = get_table(session, erp.id, tx.root_table or tables[0])
    entity = snake(root.business_entity or root.name) if root else None
    steps = tx.process.flow_steps if tx.process and tx.process.flow_steps else [tx.process.name if tx.process else "Business process"]
    models = predicted_models(erp, tables, entity, wf.dbt_models if wf and wf.tcode == tx.code else None)
    return data_flow_dot(erp, tx.process.name if tx.process else None, steps, f"{tx.code} — {tx.name}",
                         tx.app_components, tables, models, [f"{tx.name} report / dashboard"])


def flow_for_process(session: Session, erp: ERPSystem, process: BusinessProcess, wf=None) -> tuple[str, list[str]]:
    txs = list(session.scalars(select(Transaction).where(Transaction.erp_id == erp.id,
                                                         Transaction.process_id == process.id)))
    tables: list[str] = []
    for t in txs:
        tables += [l.table.name for l in t.table_links if l.role != "status"]
    for uc in session.scalars(select(UseCase).where(UseCase.erp_id == erp.id, UseCase.domain == process.domain)):
        tables += list(uc.tables or [])
    tables = list(dict.fromkeys(tables))
    root = get_table(session, erp.id, tables[0]) if tables else None
    entity = snake(root.business_entity or root.name) if root else None
    tx_label = " / ".join(t.code for t in txs) or None
    comps = {"transactions": tx_label} if tx_label else None
    dot = data_flow_dot(erp, process.name, process.flow_steps or [process.name], tx_label, comps, tables,
                        predicted_models(erp, tables, entity, wf.dbt_models if wf else None),
                        [f"{process.domain or process.name} analytics"])
    return dot, tables
