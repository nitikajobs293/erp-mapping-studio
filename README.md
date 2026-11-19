# ERP Mapping Studio

Multi-ERP source-to-target mapping, Snowflake SQL generation and technical mapping
documentation — built with Streamlit.

ERP Mapping Studio turns supplied ERP metadata, business context and target requirements
into column mappings, join conditions, grain and deduplication logic, Snowflake SQL, data
quality checks and a 10-section technical document. You can preview and edit all of it,
then download it as XLSX, DOCX, PDF, SQL, CSV or JSON.

> **Traceability first.** The app only uses supplied metadata. A join is *Confirmed* only
> when the metadata declares it (`References` column) or a user confirms it. Everything else
> is a scored recommendation, and the SQL marks it `REQUIRES VALIDATION`. When there is no
> source, the target is reported as *Missing*; the app does not invent one.

---

## Quick start (local)

```bash
cd erp_mapping_studio
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Open http://localhost:8501 → **Projects → Load SAP S/4HANA sample** → **Review & Preview**.

Run the tests:

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Workflow

| Step | Page | What happens |
|---|---|---|
| 1 | Projects | New / sample / open a project file; save versions; restore versions |
| 2 | Configuration | Source ERP(s), module, process, source & target database/schema/object, Single vs Cross ERP, history, expected grain, load type, filters, SAP client |
| 3 | Business Context | Paste requirements; the app shows table relevance derived from it |
| 4 | Source Metadata | Upload CSV/TSV/XLSX/JSON, paste, or enter manually; view/edit/search loaded metadata |
| 5 | Target Columns | Targets with type, definition, mandatory flag, expected transformation, rules, optional source hint |
| 6 | Generate Mapping | Metadata engine (+ optional Claude recommendations) with progress display |
| 7 | Review & Preview | 10 tabs: Summary, Source Tables, Column Mapping, Joins, Grain, Transformations, SQL, DQ, Technical Doc, Open Questions. **Apply** edits → SQL and docs regenerate |
| 8 | Export | XLSX (11 sheets), DOCX, PDF, SQL, CSV, JSON — all built from the applied state |

## Input formats

**Source metadata** (header names are flexible; aliases are recognised):

| Column | Aliases recognised (examples) |
|---|---|
| ERP Name | erp, source_system |
| Schema Name | schema, table_schema, owner |
| Table Name | table, tabname |
| Table Description | table_desc, table_comment |
| Column Name | column, fieldname |
| Data Type | datatype, type (+ `LENG`/`length` is appended, e.g. `CHAR(10)`) |
| Column Description | description, ddtext, scrtext_l, comment |
| Primary Key | pk, keyflag (`X`, `Y`, `TRUE`, `1`) |
| Foreign Key | fk |
| References | `TABLE.COLUMN`, several separated by `;` – declares confirmed joins |
| Business Description | business_desc, notes |

* SAP **DD03L/DD03M** extracts load directly. `CHECKTABLE` is kept as a *hint only*, because a check table names a table, not the join columns.
* **INFORMATION_SCHEMA.COLUMNS** extracts load directly. Add PK and FK information to get confirmed joins.

**Target requirements:** Target Column, Target Data Type, Business Definition, Mandatory,
Expected Transformation, Business Rules, Source Hint (`TABLE.COLUMN; …`).

**Source-to-target mapping files** (Target Column, Source Table, Source Column, …) are
converted into targets with source hints.

Expected-transformation keywords: *strip leading zeros*, *upper*, *concat* (with
`separator '-'`), *first non-null* (COALESCE), a full `CASE WHEN … END`, or `Constant 'X'`.

Templates are in `sample_data/` and can also be downloaded from inside the app.

## Generated SQL

| Script | Purpose |
|---|---|
| 1. Source extraction | One profiling SELECT per table, only the columns needed |
| 2. Integrated mapping | CTE pipeline: extraction → fan-out control → joins → transformations |
| 3. Deduplication (final) | `ROW_NUMBER()` over the grain keys, ordered by change dates (no `DISTINCT`); SCD2-style history or as-of snapshot if configured |
| 3b. Create view/table | `CREATE OR REPLACE VIEW/TABLE` over the final query |
| 4. Data quality | Counts, duplicates, mandatory nulls, referential integrity + fan-out profile, failed conversions, business rules |
| 5. Incremental MERGE | Target DDL with audit columns, watermark with 3-day lookback, MERGE on the grain keys |

Cross-ERP projects generate one branch per ERP, then combine them with `UNION ALL` and a
`SOURCE_ERP_NAME` column.

## Configuring AI (optional)

The metadata engine works without AI. To add Claude recommendations:

```toml
# .streamlit/secrets.toml   (copy from .streamlit/secrets.toml.example; never commit it)
ANTHROPIC_API_KEY = "sk-ant-..."
# ANTHROPIC_MODEL = "claude-sonnet-5-5"
```

You can also set `ANTHROPIC_API_KEY` as an environment variable, or paste a key in
**AI Settings**. A key entered there is kept in memory for that browser session only. AI
output is checked against metadata. The app rejects tables, columns and joins it cannot
verify, and lists them under Open Questions. Nothing from the AI is marked Confirmed.

OpenAI and Azure OpenAI are stubbed behind the same `AIProvider` interface. To add one,
implement `complete()` in `ai_service.py`.

## Deploying to Streamlit Community Cloud

1. Push this folder to a GitHub repository. Keep `.streamlit/secrets.toml` out of git; it is already in `.gitignore`.
2. Go to https://share.streamlit.io and click **Create app**. Pick the repository and branch, and set the main file path to `app.py`. If the app is in a subfolder, use `erp_mapping_studio/app.py`.
3. Under **Advanced settings**, choose a Python version (3.11 or later) and paste your secrets (`ANTHROPIC_API_KEY = "…"`).
4. Deploy. Dependencies install from `requirements.txt`. The Graphviz join diagrams render in the browser, so no system packages are needed.

Notes for hosted use:
* The server disk on Community Cloud is temporary. Use **Download project file** to keep
  projects, and **Open project** to continue later.
* Changes made on the ERP Master page are written to `templates/erp_master.json` on the
  server. On Community Cloud, commit that file to make the changes permanent.
* The upload limit is set to 200 MB in `.streamlit/config.toml`.

You can also run the app in any container: `streamlit run app.py --server.port 8080 --server.address 0.0.0.0`.

## Project layout & architecture

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the architecture, folder structure,
UI wireframe, data model, the SAP S/4HANA sample walkthrough, and how to extend the app.

## About the sample data

The SAP S/4HANA sample uses standard delivery and shipment tables (LIKP, LIPS, VTTK, VTTP,
T001W) with a curated subset of fields. It is there to demonstrate the app. Validate it
against your own system's DDIC before relying on it, because field usage varies by
release and configuration.
