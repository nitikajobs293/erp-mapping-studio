"""ER diagrams and data-flow diagrams (Graphviz DOT + Mermaid), styled by validation status."""
from __future__ import annotations

import html

from sqlalchemy.orm import Session

from erpdm.db.models import ERPSystem, ERPTable
from erpdm.schemas import JoinPlan
from erpdm.services.metadata import get_table, relationships_among

STATUS_STYLE = {
    "verified": ('"#1b7f3b"', "solid"),
    "requires_validation": ('"#c77700"', "dashed"),
    "pending": ('"#b42318"', "dotted"),
    "rejected": ('"#999999"', "dotted"),
}
CLASS_FILL = {"master": "#e8f1fb", "transaction": "#fff4e5", "text": "#f3f3f3", "config": "#f3f3f3",
              "document_flow": "#fdecef", "status": "#f1ecfb", "unknown": "#ffffff"}


def _table_node(t: ERPTable, show_columns: list[str] | None, max_cols: int = 12) -> str:
    fill = CLASS_FILL.get(t.table_class, "#ffffff")
    rows = [f'<TR><TD BGCOLOR="#24364d" ALIGN="LEFT"><FONT COLOR="white"><B>{html.escape(t.name)}</B></FONT></TD></TR>',
            f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="9">{html.escape((t.description or "")[:48])}</FONT></TD></TR>']
    cols = [c for c in t.columns if (show_columns is None and c.is_pk) or (show_columns and c.name in show_columns) or c.is_pk]
    for c in cols[:max_cols]:
        key = "🔑 " if c.is_pk else ""
        rows.append(f'<TR><TD ALIGN="LEFT" PORT="{html.escape(c.name)}"><FONT POINT-SIZE="9">{key}{html.escape(c.name)} '
                    f'<I>{html.escape(c.data_type or "")}</I></FONT></TD></TR>')
    if len(cols) > max_cols:
        rows.append(f'<TR><TD><FONT POINT-SIZE="8">… {len(cols) - max_cols} more</FONT></TD></TR>')
    return (f'"{t.name}" [shape=plain label=<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" BGCOLOR="{fill}">'
            + "".join(rows) + "</TABLE>>];")


def er_diagram_dot(session: Session, erp: ERPSystem, table_names: list[str], plan: JoinPlan | None = None,
                   selected: dict[str, list[str]] | None = None, include_all_relationships: bool = True) -> str:
    lines = ["digraph ER {", 'graph [rankdir=LR fontname="Helvetica" bgcolor="transparent" nodesep=0.4 ranksep=0.9];',
             'node [fontname="Helvetica"]; edge [fontname="Helvetica" fontsize=9];']
    tables = {n: get_table(session, erp.id, n) for n in table_names}
    tables = {k: v for k, v in tables.items() if v}
    for t in tables.values():
        lines.append(_table_node(t, (selected or {}).get(t.name)))
    plan_ids = {e.relationship_id for e in plan.edges} if plan else set()
    ids = [t.id for t in tables.values()]
    for r in relationships_among(session, erp.id, ids):
        if r.from_table.name not in tables or r.to_table.name not in tables:
            continue
        if not include_all_relationships and plan and r.id not in plan_ids:
            continue
        color, style = STATUS_STYLE.get(r.validation_status, ('"#666666"', "solid"))
        on = ", ".join(f"{j['from']}={j['to']}" for j in r.join_columns if j["from"] != (erp.client_column or ""))
        pen = "2.4" if r.id in plan_ids else "1"
        lines.append(f'"{r.from_table.name}" -> "{r.to_table.name}" [label="{r.cardinality} {html.escape(on)}\\n'
                     f'{r.relationship_type} · {r.confidence:.2f}" color={color} style={style} penwidth={pen} '
                     f'arrowhead={"crow" if r.cardinality in ("1:N", "N:M") else "normal"} '
                     f'tooltip="{html.escape(r.description or "")}"];')
    lines.append("}")
    return "\n".join(lines)


def er_diagram_mermaid(session: Session, erp: ERPSystem, table_names: list[str]) -> str:
    card = {"1:1": "||--||", "1:N": "||--o{", "N:1": "}o--||", "N:M": "}o--o{", "uncertain": "}o..o{"}
    out = ["erDiagram"]
    tables = {n: get_table(session, erp.id, n) for n in table_names}
    for t in tables.values():
        if t is None:
            continue
        out.append(f"    {t.name} {{")
        for c in t.columns:
            if c.is_pk or c.business_name:
                dt = (c.data_type or "string").replace(" ", "_")
                out.append(f"        {dt} {c.name}{' PK' if c.is_pk else ''}")
        out.append("    }")
    ids = [t.id for t in tables.values() if t]
    for r in relationships_among(session, erp.id, ids):
        if r.from_table.name in tables and r.to_table.name in tables:
            label = (r.relationship_type + "_" + r.validation_status)[:40]
            out.append(f'    {r.from_table.name} {card.get(r.cardinality, "||--o{")} {r.to_table.name} : "{label}"')
    return "\n".join(out)


def data_flow_dot(erp: ERPSystem, process_name: str | None, flow_steps: list[str] | None, transaction: str | None,
                  app_components: dict | None, tables: list[str], models: dict[str, list[str]] | None = None,
                  bi_consumers: list[str] | None = None) -> str:
    """Business process → transaction → app logic → tables → extraction → RAW → staging → int → marts → BI."""
    models = models or {}
    esc = lambda s: html.escape(str(s)).replace('"', "'")
    L = ['digraph flow {', 'graph [rankdir=LR fontname="Helvetica" bgcolor="transparent" compound=true];',
         'node [shape=box style="rounded,filled" fontname="Helvetica" fontsize=10 fillcolor="#eef3f9"];',
         'edge [color="#5b6b7f"];']

    def cluster(name: str, label: str, nodes: list[tuple[str, str]], color: str) -> None:
        L.append(f'subgraph cluster_{name} {{ label="{esc(label)}" style="rounded" color="{color}" fontsize=11;')
        for nid, text in nodes:
            L.append(f'"{nid}" [label="{esc(text)}"];')
        L.append("}")

    steps = flow_steps or ([process_name] if process_name else ["Business process"])
    cluster("bp", "Business process", [(f"bp{i}", s) for i, s in enumerate(steps)], "#7a9cc6")
    for i in range(len(steps) - 1):
        L.append(f'"bp{i}" -> "bp{i + 1}";')
    if transaction:
        cluster("tx", "ERP transaction / screen", [("tx", transaction)], "#7a9cc6")
        L.append(f'"bp{len(steps) - 1}" -> "tx";')
    comps = [f"{k}: {v}" for k, v in (app_components or {}).items() if v] or ["Application logic (validate in system)"]
    cluster("app", "Application components", [(f"app{i}", c[:70]) for i, c in enumerate(comps)], "#a0a0a0")
    L.append(f'"{"tx" if transaction else f"bp{len(steps) - 1}"}" -> "app0";')
    cluster("db", f"{erp.name} backend tables", [(f"t_{t}", t) for t in tables], "#c77700")
    for t in tables:
        L.append(f'"app0" -> "t_{t}" [style=dashed];')
    cluster("ext", "Extraction", [("ext", erp.extraction_method or "Extraction tool (configure on ERP)")], "#6b5b95")
    for t in tables:
        L.append(f'"t_{t}" -> "ext";')
    cluster("raw", "Snowflake RAW", [("raw", f"RAW.{erp.code}.*  (+ _loaded_at, CDC flags)")], "#1b7f3b")
    L.append('"ext" -> "raw";')
    stg = models.get("stg") or ["dbt staging"]
    ints = models.get("int") or ["dbt intermediate"]
    marts = models.get("mart") or ["Gold dimensions / facts"]
    cluster("stg", "dbt staging", [(f"stg{i}", n) for i, n in enumerate(stg)], "#1b7f3b")
    cluster("int", "dbt intermediate", [(f"int{i}", n) for i, n in enumerate(ints)], "#1b7f3b")
    cluster("mart", "Gold dimensions / facts", [(f"mart{i}", n) for i, n in enumerate(marts)], "#1b7f3b")
    for i in range(len(stg)):
        L.append(f'"raw" -> "stg{i}";')
        L.append(f'"stg{i}" -> "int0";')
    for i in range(len(marts)):
        L.append(f'"int0" -> "mart{i}";')
    bis = bi_consumers or ["BI reports / dashboards"]
    cluster("bi", "Consumption", [(f"bi{i}", b) for i, b in enumerate(bis)], "#24364d")
    for i in range(len(marts)):
        L.append(f'"mart{i}" -> "bi0";')
    L.append("}")
    return "\n".join(L)


def render_png(dot: str) -> bytes | None:
    """Render DOT to PNG with the graphviz binary, if installed."""
    try:
        import graphviz
        return graphviz.Source(dot).pipe(format="png")
    except Exception:  # noqa: BLE001 - binary missing or render error
        return None


def render_svg(dot: str) -> bytes | None:
    try:
        import graphviz
        return graphviz.Source(dot).pipe(format="svg")
    except Exception:  # noqa: BLE001
        return None
