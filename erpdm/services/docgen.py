"""Mapping / technical design document generation: Excel (openpyxl), Word (python-docx), PDF (reportlab)."""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

def _plain(text: str) -> str:
    """Strip the light markdown used in UI explanations (bold/code markers) for Office/PDF output."""
    return str(text).replace("**", "").replace("`", "")


STATUS_FILL = {"verified": "D1F2D9", "requires_validation": "FFF1D6", "pending": "FDE2E1", "rejected": "E5E5E5"}
HEADER_FILL = PatternFill("solid", fgColor="24364D")


def _write_sheet(ws, df: pd.DataFrame, status_col: str | None = None, widths: dict[str, int] | None = None) -> None:
    ws.append(list(df.columns))
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for rec in df.itertuples(index=False):
        ws.append([None if (isinstance(v, float) and pd.isna(v)) else v for v in rec])
    for i, col in enumerate(df.columns, start=1):
        w = (widths or {}).get(col) or min(60, max(12, int(df[col].astype(str).str.len().clip(upper=60).mean() if len(df) else 12) + 4, len(col) + 2))
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    if status_col and status_col in df.columns:
        idx = list(df.columns).index(status_col) + 1
        for r in range(2, ws.max_row + 1):
            v = ws.cell(r, idx).value
            if v in STATUS_FILL:
                ws.cell(r, idx).fill = PatternFill("solid", fgColor=STATUS_FILL[v])
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions


def mapping_excel(meta: dict[str, Any], mapping_df: pd.DataFrame, joins: list[dict], tables: list[dict],
                  sql: str | None = None, validation_queries: dict[str, str] | None = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Overview"
    ws.append(["Source-to-Target Mapping"])
    ws["A1"].font = Font(bold=True, size=14)
    for k, v in meta.items():
        ws.append([k, str(v) if v is not None else ""])
    ws.append([])
    ws.append(["Legend", "verified", "requires_validation", "pending"])
    for i, key in enumerate(["verified", "requires_validation", "pending"], start=2):
        ws.cell(ws.max_row, i).fill = PatternFill("solid", fgColor=STATUS_FILL[key])
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 70
    df = mapping_df.drop(columns=[c for c in ("id",) if c in mapping_df.columns])
    ws2 = wb.create_sheet("Mapping")
    _write_sheet(ws2, df, "Validation Status")
    dv = DataValidation(type="list", formula1='"verified,requires_validation,pending,rejected"', allow_blank=True)
    ws2.add_data_validation(dv)
    if "Validation Status" in df.columns:
        col = get_column_letter(list(df.columns).index("Validation Status") + 1)
        dv.add(f"{col}2:{col}{max(2, ws2.max_row)}")
    if joins:
        _write_sheet(wb.create_sheet("Joins"), pd.DataFrame(joins), "Validation")
    if tables:
        _write_sheet(wb.create_sheet("Source Tables"), pd.DataFrame(tables), "Validation")
    if sql:
        ws5 = wb.create_sheet("Source SQL")
        for line in sql.splitlines():
            ws5.append([line])
        ws5.column_dimensions["A"].width = 140
    if validation_queries:
        ws6 = wb.create_sheet("Validation Queries")
        ws6.append(["Check", "SQL"])
        for k, v in validation_queries.items():
            ws6.append([k, v])
        ws6.column_dimensions["A"].width = 45
        ws6.column_dimensions["B"].width = 120
        for row in ws6.iter_rows(min_row=2):
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def technical_design_docx(title: str, meta: dict[str, Any], sections: list[tuple[str, Any]],
                          er_png: bytes | None = None, flow_png: bytes | None = None) -> bytes:
    """sections: list of (heading, content) where content is str | list[str] | pd.DataFrame | ("code", str)."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor, Inches

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(10.5)
    h = doc.add_heading(title, level=0)
    h.alignment = WD_ALIGN_PARAGRAPH.LEFT
    p = doc.add_paragraph(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by the ERP Data Modelling "
                          "Intelligence Platform")
    p.runs[0].font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    t = doc.add_table(rows=0, cols=2)
    t.style = "Light List Accent 1"
    for k, v in meta.items():
        row = t.add_row().cells
        row[0].text, row[1].text = str(k), "" if v is None else str(v)
    for heading, content in sections:
        doc.add_heading(heading, level=1)
        if heading.lower().startswith("er diagram") and er_png:
            doc.add_picture(io.BytesIO(er_png), width=Inches(6.5))
        if heading.lower().startswith("data flow") and flow_png:
            doc.add_picture(io.BytesIO(flow_png), width=Inches(6.5))
        _docx_content(doc, content)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _docx_content(doc, content) -> None:
    from docx.shared import Pt
    if content is None:
        return
    if isinstance(content, tuple) and content and content[0] == "code":
        para = doc.add_paragraph()
        run = para.add_run(content[1])
        run.font.name = "Consolas"
        run.font.size = Pt(8)
        return
    if isinstance(content, pd.DataFrame):
        df = content.fillna("")
        if df.empty:
            doc.add_paragraph("(none)")
            return
        table = doc.add_table(rows=1, cols=len(df.columns))
        table.style = "Light Grid Accent 1"
        for i, c in enumerate(df.columns):
            table.rows[0].cells[i].text = str(c)
        for rec in df.itertuples(index=False):
            cells = table.add_row().cells
            for i, v in enumerate(rec):
                cells[i].text = str(v)[:400]
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    for run in para.runs:
                        run.font.size = Pt(8)
        return
    if isinstance(content, list):
        for item in content:
            doc.add_paragraph(_plain(item), style="List Bullet")
        return
    for para in str(content).split("\n\n"):
        doc.add_paragraph(para)


def technical_design_pdf(title: str, meta: dict[str, Any], sections: list[tuple[str, Any]],
                         er_png: bytes | None = None, flow_png: bytes | None = None) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (Image, LongTable, Paragraph, Preformatted, SimpleDocTemplate, Spacer,
                                    TableStyle)
    from xml.sax.saxutils import escape

    buf = io.BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1.5 * cm, bottomMargin=1.5 * cm, title=title)
    ss = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=ss["BodyText"], fontSize=7, leading=8.5)
    code = ParagraphStyle("code", parent=ss["Code"], fontSize=6.5, leading=8)
    story = [Paragraph(escape(title), ss["Title"]),
             Paragraph(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", ss["Italic"]), Spacer(1, 8)]
    mt = LongTable([[Paragraph(f"<b>{escape(str(k))}</b>", small), Paragraph(escape(str(v or "")), small)]
                    for k, v in meta.items()], colWidths=[5 * cm, 20 * cm])
    mt.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [mt, Spacer(1, 10)]
    width = page[0] - 3 * cm

    def add_img(png: bytes) -> None:
        img = Image(io.BytesIO(png))
        ratio = min(width / img.imageWidth, (page[1] - 5 * cm) / img.imageHeight, 1.0)
        img.drawWidth, img.drawHeight = img.imageWidth * ratio, img.imageHeight * ratio
        story.append(img)

    for heading, content in sections:
        story.append(Paragraph(escape(heading), ss["Heading2"]))
        if heading.lower().startswith("er diagram") and er_png:
            add_img(er_png)
        if heading.lower().startswith("data flow") and flow_png:
            add_img(flow_png)
        if content is None:
            continue
        if isinstance(content, tuple) and content and content[0] == "code":
            story.append(Preformatted(content[1], code, maxLineLength=180))
        elif isinstance(content, pd.DataFrame):
            df = content.fillna("").astype(str)
            if df.empty:
                story.append(Paragraph("(none)", small))
                continue
            cols = list(df.columns)[:10]
            data = [[Paragraph(f"<b>{escape(c)}</b>", small) for c in cols]]
            for rec in df[cols].itertuples(index=False):
                data.append([Paragraph(escape(v[:300]), small) for v in rec])
            tbl = LongTable(data, colWidths=[width / len(cols)] * len(cols), repeatRows=1)
            tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                                     ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dfe7f1")),
                                     ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story.append(tbl)
        elif isinstance(content, list):
            for item in content:
                story.append(Paragraph("• " + escape(_plain(item)), small))
        else:
            for para in str(content).split("\n\n"):
                story.append(Paragraph(escape(para), ss["BodyText"]))
        story.append(Spacer(1, 6))
    doc.build(story)
    return buf.getvalue()


def build_design_sections(*, erp_name: str, tcode: str | None, use_case: str | None, plan, gen_sql, rec,
                          mapping_df: pd.DataFrame | None, relationships_df: pd.DataFrame | None,
                          tables_df: pd.DataFrame | None, dbt_files: list[str] | None) -> list[tuple[str, Any]]:
    secs: list[tuple[str, Any]] = []
    scope = [f"ERP: {erp_name}"] + ([f"T-Code: {tcode}"] if tcode else []) + ([f"Use case: {use_case}"] if use_case else [])
    if plan:
        scope.append(f"Grain / root table: {plan.root_table}")
    secs.append(("1. Scope", scope))
    secs.append(("2. Accuracy and validation notice", [
        "Every table, column and join carries a provenance label (source type, confidence, validation status).",
        "Items not marked 'verified' are standard knowledge or suggestions and must be validated against the "
        "actual system (DDIC/DDL extracts) before production use."] + (plan.warnings if plan else [])))
    if tables_df is not None:
        secs.append(("3. Source tables", tables_df))
    if relationships_df is not None:
        secs.append(("4. Relationships and joins", relationships_df))
    secs.append(("ER diagram", None))
    if gen_sql:
        secs.append(("5. Join explanations", gen_sql.join_explanations))
        secs.append(("6. Filters, duplicates and nulls",
                     gen_sql.filter_explanations + gen_sql.duplicate_handling + gen_sql.null_handling))
        secs.append(("7. Source SQL", ("code", gen_sql.sql)))
        secs.append(("8. Performance considerations", gen_sql.performance_notes))
    if mapping_df is not None:
        cols = [c for c in ["Business Column", "Source Table", "Source Column", "Data Type", "Target Column",
                            "Target Data Type", "Transformation", "Join Dependency", "Validation Status"] if c in mapping_df.columns]
        secs.append(("9. Source-to-target mapping", mapping_df[cols]))
    if rec:
        secs.append(("10. Dimensional model", pd.DataFrame([{
            "Model": m.name, "Kind": m.kind, "Grain": m.grain, "SCD": m.scd_type, "Natural key": ", ".join(m.natural_key),
            "Surrogate key": m.surrogate_key, "Sources": ", ".join(m.source_tables), "Rationale": m.rationale}
            for m in rec.models])))
        secs.append(("11. Incremental load strategy", pd.DataFrame([{
            "Table": s.table, "Strategy": s.strategy, "Unique key": ", ".join(s.unique_key),
            "Watermark": ", ".join(s.watermark), "Reliable": s.watermark_reliable, "Lookback": s.lookback,
            "Change tracking": s.change_tracking, "Hard deletes": s.hard_delete} for s in rec.strategies])))
        if rec.notes:
            secs.append(("12. Modelling notes", rec.notes))
    secs.append(("Data flow", None))
    if dbt_files:
        secs.append(("13. dbt project contents", dbt_files))
    return secs
