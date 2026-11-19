"""Dimensional model recommendation and incremental-load strategy per source table."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from erpdm.db.models import ERPSystem, ERPTable
from erpdm.schemas import JoinPlan, SelectedColumn
from erpdm.services.metadata import get_table
from erpdm.services.sqlgen import snake

MEASURE_TYPES = {"QUAN", "CURR", "DEC", "DECIMAL", "NUMBER", "INT4", "INT8", "FLTP", "NUMERIC", "FLOAT"}


@dataclass
class IncrementalStrategy:
    table: str
    strategy: str              # merge | append | snapshot | full_refresh
    unique_key: list[str]
    watermark: list[str]
    watermark_reliable: bool
    lookback: str
    change_tracking: str
    hard_delete: str
    full_refresh_cadence: str
    notes: list[str] = field(default_factory=list)


@dataclass
class ModelSpec:
    name: str
    kind: str                  # dimension | fact | bridge
    grain: str
    source_tables: list[str]
    natural_key: list[str]
    surrogate_key: str
    scd_type: str              # SCD1 | SCD2 | n/a
    attributes: list[str]
    measures: list[str] = field(default_factory=list)
    foreign_keys: list[dict] = field(default_factory=list)  # {"column":..,"references":"dim_x"}
    rationale: str = ""


@dataclass
class ModelRecommendation:
    entity: str
    models: list[ModelSpec]
    strategies: list[IncrementalStrategy]
    notes: list[str]


def incremental_strategy(erp: ERPSystem, table: ERPTable) -> IncrementalStrategy:
    client = (erp.client_column or "").upper()
    ct = table.change_tracking or {}
    cols = ct.get("columns") or []
    reliable = bool(ct.get("reliable"))
    pk = [c for c in table.primary_key]
    notes = [ct["note"]] if ct.get("note") else []
    insert_only = "insert-only" in (table.delete_strategy or "").lower()
    log_cdc = "Log-based CDC (SAP SLT / ODP delta, Oracle GoldenGate, Qlik/Fivetran) exposing an operation flag"
    if insert_only and cols:
        return IncrementalStrategy(table.name, "append", pk, cols, True, "1 day (late commits)", f"Watermark on {', '.join(cols)}",
                                   "Not applicable — documents are not deleted (archiving only).", "Monthly reconciliation",
                                   notes + ["Append-only; reversals arrive as new documents."])
    if cols and reliable:
        return IncrementalStrategy(table.name, "merge", pk, cols, True, "1 day", f"Timestamp watermark on {', '.join(cols)}",
                                   "Timestamp extraction cannot see hard deletes: use CDC delete flags or a periodic "
                                   "key reconciliation (anti-join of source keys vs target).",
                                   "Weekly full refresh optional", notes)
    if cols and not reliable:
        return IncrementalStrategy(
            table.name, "merge", pk, cols, False, "3–7 days",
            f"Date-only / partially maintained change column {', '.join(cols)} — prefer {log_cdc}",
            "Use CDC delete markers; otherwise weekly full-key reconciliation.",
            "Weekly full refresh recommended until CDC is in place",
            notes + ["Do not rely on the change date alone: it is a date without time and may not change for every update."])
    if table.table_class in ("config", "text"):
        return IncrementalStrategy(table.name, "full_refresh", pk, [], False, "n/a", "Small table — full reload each run",
                                   "Full reload handles deletes", "Every run", notes)
    return IncrementalStrategy(
        table.name, "snapshot", pk, [], False, "n/a",
        f"No reliable change column in metadata. Use {log_cdc}; or replicate fully and detect changes with a dbt "
        "snapshot (check strategy / row hash).",
        "Full replication + snapshot detects deletes (invalidate_hard_deletes).", "Every run (snapshot)", notes)


def recommend_model(session: Session, erp: ERPSystem, plan: JoinPlan, columns: list[SelectedColumn],
                    history_required: bool = False) -> ModelRecommendation:
    root = get_table(session, erp.id, plan.root_table)
    entity = snake(root.business_entity or root.name)
    client = (erp.client_column or "").upper()
    tables = {t: get_table(session, erp.id, t) for t in plan.tables}
    sel_by_table: dict[str, list[SelectedColumn]] = {}
    for c in columns:
        sel_by_table.setdefault(c.table.upper(), []).append(c)

    def attr_names(tbl: ERPTable) -> list[str]:
        return [c.column for c in sel_by_table.get(tbl.name.upper(), [])]

    models: list[ModelSpec] = []
    notes: list[str] = []
    time_dependent = any(t.default_filter and "DATBI" in (t.default_filter or "") for t in tables.values())
    root_nk = [k for k in root.primary_key if k.upper() != client]

    # Other entities that deserve conformed dimensions (non-root masters with their own entity)
    other_dims: dict[str, list[ERPTable]] = {}
    for t in tables.values():
        if t.name == root.name or t.table_class not in ("master", "config", "text"):
            continue
        ent = t.business_entity
        if ent and ent != root.business_entity:
            other_dims.setdefault(snake(ent), []).append(t)

    if root.table_class in ("master", "config"):
        scd = "SCD2" if (history_required or time_dependent) else "SCD1"
        attrs = []
        for t in tables.values():
            if t.business_entity in (None, root.business_entity) or t.table_class in ("text", "status"):
                attrs += [f"{t.name}.{a}" for a in attr_names(t)]
        fks = []
        for ent, tbls in other_dims.items():
            fks.append({"column": f"{ent}_key", "references": f"dim_{ent}",
                        "via": ", ".join(t.name for t in tbls)})
        models.append(ModelSpec(
            name=f"dim_{entity}", kind="dimension", grain=f"One row per {root.name} ({', '.join(root_nk)})"
            + (" per validity period" if scd == "SCD2" else ""),
            source_tables=[t for t in tables if tables[t].business_entity in (None, root.business_entity)],
            natural_key=root_nk, surrogate_key=f"{entity}_sk", scd_type=scd, attributes=attrs, foreign_keys=fks,
            rationale=("Master data root → dimension. "
                       + ("SCD2 because the source is time-dependent (validity segments) or history was requested."
                          if scd == "SCD2" else "SCD1 (current state) unless history is required."))))
    else:
        measures, attrs = [], []
        for t in tables.values():
            for a in attr_names(t):
                col = next((c for c in t.columns if c.name.upper() == a.upper()), None)
                if col and (col.data_type or "").upper() in MEASURE_TYPES:
                    measures.append(f"{t.name}.{a}")
                elif t.business_entity in (None, root.business_entity):
                    attrs.append(f"{t.name}.{a}")
        fks = [{"column": f"{ent}_key", "references": f"dim_{ent}", "via": ", ".join(t.name for t in tbls)}
               for ent, tbls in other_dims.items()]
        models.append(ModelSpec(
            name=f"fct_{entity}", kind="fact", grain=f"One row per {root.name} ({', '.join(root_nk)})",
            source_tables=list(tables), natural_key=root_nk, surrogate_key=f"{entity}_sk", scd_type="n/a",
            attributes=attrs, measures=measures, foreign_keys=fks,
            rationale="Transactional root → fact at the document/item grain; degenerate document numbers kept on the fact."))
        notes.append("Late-arriving dimensions: facts join to dimensions with LEFT JOIN and fall back to the "
                     "'unknown' member (-1) so loads never drop rows; re-key on the next run.")

    for ent, tbls in other_dims.items():
        anchor = next((t for t in tbls if t.table_class in ("master", "config")), tbls[0])
        nk = [k for k in anchor.primary_key if k.upper() not in (client, "SPRAS")]
        attrs = [f"{t.name}.{a}" for t in tbls for a in attr_names(t)]
        models.append(ModelSpec(
            name=f"dim_{ent}", kind="dimension", grain=f"One row per {anchor.name} ({', '.join(nk)})",
            source_tables=[t.name for t in tbls], natural_key=nk, surrogate_key=f"{ent}_sk", scd_type="SCD1",
            attributes=attrs, rationale=f"Conformed dimension reused by other marts ({anchor.business_entity})."))

    if any(t.table_class == "status" for t in tables.values()):
        notes.append("Status tables are aggregated to flags (e.g. is_deleted, is_inactive) in the intermediate layer.")
    if time_dependent:
        notes.append("Time-dependent source segments (DATBI) map naturally to SCD2 validity: valid_from/valid_to.")
    strategies = [incremental_strategy(erp, t) for t in tables.values()]
    return ModelRecommendation(entity=entity, models=models, strategies=strategies, notes=notes)
