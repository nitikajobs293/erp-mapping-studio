# ERP Mapping Studio – Architecture

## 1. Application architecture

```
┌──────────────────────────────── Streamlit UI (app.py, utils/ui.py) ────────────────────────────────┐
│ Projects · Configuration · Business Context · Source Metadata · Target Columns · Generate Mapping   │
│ Review & Preview (10 tabs, editable) · Export · ERP Master · AI Settings                            │
└───────────────┬───────────────────────────────────────────────────────────────┬────────────────────┘
                │ MappingProject (models.py) – one JSON document in session      │
┌───────────────▼──────────────┐   ┌───────────────────────────────┐   ┌───────▼───────────────────┐
│ Input layer                  │   │ Analysis layer                │   │ Output layer              │
│ metadata_parser.py           │   │ mapping_engine.py (orchestr.) │   │ snowflake_sql_generator   │
│  CSV/TSV/XLSX/JSON/paste,    │──▶│  ├ join_analyzer.py           │──▶│ documentation_generator   │
│  DD03L & INFORMATION_SCHEMA, │   │  ├ grain_analyzer.py          │   │ export_manager            │
│  S2T files                   │   │  ├ transformation_engine.py   │   │  xlsx docx pdf sql csv    │
│ erp_master.py (+JSON)        │   │  ├ validation_engine.py       │   │  json + project file      │
└──────────────────────────────┘   │  └ ai_service.py (optional)   │   └───────────────────────────┘
                                   └───────────────────────────────┘
```

**Flow.** Metadata and targets are normalised into canonical records → `run_analysis()`
discovers joins (metadata FKs first, name-based suggestions second), matches targets,
picks the grain/driving table, finds a join path, controls fan-out, builds
transformations, validates, writes open questions and generates SQL. Optional AI
output is merged by `apply_ai_result()` only after every table/column is verified
against metadata. After any manual edit, `regenerate_outputs()` re-derives join
paths, transformations, questions and SQL **without** re-matching columns.

**Traceability contract**

| Item | Confirmed only when | Otherwise |
|---|---|---|
| Column mapping | Source named by the user (hint, S2T file, edit) **and** present in metadata | Suggested / Requires Validation (scored) or Missing |
| Join | Declared in metadata (`References` = `TABLE.COLUMN`) or confirmed by a user | Requires Validation, `-- WARNING` in SQL |
| Grain | User confirms it in the Data Grain tab | Suggested / Requires Validation |
| AI output | Never | Suggested (mappings), Requires Validation (joins); unverifiable items rejected |

Join path search is weighted (confirmed = 1, unconfirmed ≈ 10), so a chain of verified
joins is always preferred to a shortcut through a guessed one. Tables with no path are
never joined; their target columns become typed NULLs with a `MISSING JOIN` comment.

## 2. Folder structure

```
erp_mapping_studio/
├── app.py                      Streamlit UI
├── config.py                   constants, statuses, secrets access
├── models.py                   MappingProject data model + versions
├── erp_master.py               ERP registry (reads templates/erp_master.json)
├── metadata_parser.py          file/paste parsing, header aliasing
├── mapping_engine.py           orchestration, scoring, AI merge
├── join_analyzer.py            FK joins, suggestions, cardinality, join path, diagrams
├── grain_analyzer.py           grain, dedup keys/order, duplicate risks
├── transformation_engine.py    type/date/null logic per target column
├── snowflake_sql_generator.py  extraction, integrated, dedup/history, view, DQ, MERGE
├── documentation_generator.py  10-section technical document model + Markdown
├── export_manager.py           XLSX / DOCX / PDF / SQL / CSV / JSON
├── validation_engine.py        validation rules + open questions
├── ai_service.py               provider interface, Anthropic implementation, prompt
├── templates/erp_master.json   editable ERP list
├── sample_data/                SAP S/4HANA sample metadata, targets, templates, sample project
├── generated_outputs/          optional server-side saves
├── utils/                      text_utils.py (matching, SQL identifiers), ui.py (editors, styling)
├── tests/test_engine.py        rule + SQL + export tests (pytest)
├── docs/ARCHITECTURE.md
└── .streamlit/                 config.toml, secrets.toml.example
```

## 3. UI wireframe and navigation

```
┌ Sidebar ─────────────┐┌ Banner: page title + purpose ─────────────────────────────────────────┐
│ ERP Mapping Studio   ││                                                                         │
│ Project: <name>      ││  Review & Preview                                                       │
│ ✅ Configuration     ││  [Summary][Source Tables][Column Mapping][Joins][Grain][Transforms]     │
│ ✅ Source metadata   ││  [Snowflake SQL][DQ Checks][Technical Doc][Open Questions]              │
│ ✅ Target columns    ││ ┌──────────────────────────────────────────────────────────────────────┐ │
│ ✅ Mapping generated ││ │ 🔍 filter rows…                                                      │ │
│ ⬜ Questions resolved││ │ target │ status ▾ │ confidence ▮▮▮ │ source table ▾ │ column │ SQL … │ │
│ ──────────────────── ││ │ editable grid (st.data_editor)                                       │ │
│ ○ Projects           ││ └──────────────────────────────────────────────────────────────────────┘ │
│ ○ Configuration      ││ [Apply & regenerate SQL]  ⚠ Unapplied edits…                            │
│ ○ Business Context   ││ ▸ Mapping details and reasoning                                         │
│ ○ Source Metadata    │└─────────────────────────────────────────────────────────────────────────┘
│ ○ Target Columns     │
│ ○ Generate Mapping   │   Joins tab: Graphviz diagram (green solid = confirmed, orange dashed =
│ ● Review & Preview   │   recommendation) + text join tree + editable join table.
│ ○ Export             │   Export page: status badges, warnings, six download buttons.
│ ○ ERP Master         │
│ ○ AI Settings        │
│ 🟢5 🟡14 🟠1 🔴1      │
│ [💾 Download project]│
└──────────────────────┘
```

## 4. Data model (`models.py`)

`MappingProject` – serialised as one JSON document:

| Field | Content |
|---|---|
| `project_id`, `created_at`, `updated_at` | identity and audit |
| `config` | project name, source ERPs, module, process, source/target database/schema/object, mapping type, history, expected grain, load type, filters, SAP client |
| `business_context` | free text |
| `source_metadata[]` | erp_name, schema_name, table_name, table_description, column_name, data_type, column_description, is_primary_key, is_foreign_key, references (`TABLE.COLUMN;…`), business_description |
| `target_columns[]` | target_column, target_data_type, business_definition, mandatory, expected_transformation, business_rules, source_hint |
| `column_mappings[]` | target, source ERP/schema/table/column(s), data types, transformation type + SQL, join dependency, business rule, **status, evidence, confidence, reasoning** |
| `joins[]` | join_id, ERP, left (child) table/columns, right (parent) table/columns, join type, cardinality, **status, evidence, confidence**, fan-out handling, reasoning, include |
| `table_mapping[]` | table, alias, role (driving / lookup / fan-out / bridge / unreachable), PK, FKs, join condition, purpose |
| `grain{erp}` | grain table, keys, description, dedup partition/order, change columns, status, confidence, warnings |
| `open_questions[]` | id, category, item, detail, severity, owner, resolution status |
| `sql{}` | source_extraction, integrated, deduplication, create_view, data_quality, merge |
| `validation_issues[]`, `ai_log[]` | findings and AI verification reports |
| `versions[]` | numbered snapshots (config, mappings, joins, grain, questions, SQL) with notes |

## 5. Sample workflow – SAP S/4HANA delivery and shipment

1. **Projects → Load SAP S/4HANA sample** (or build it yourself from `sample_data/*.csv`).
2. **Metadata** – LIKP, LIPS, VTTK, VTTP, T001W with PKs. FK references declared in the
   file: `LIPS.(MANDT,VBELN) → LIKP`, `VTTP.(MANDT,VBELN) → LIKP`, `VTTP.(MANDT,TKNUM) → VTTK`.
   T001W has **no** declared FK.
3. **Targets** – 21 columns; three carry source hints (DELIVERY_NUMBER, DELIVERY_ITEM,
   SHIPMENT_NUMBER), one is a constant, one is a COALESCE of item/header change dates, and
   TRACKING_NUMBER has no source in the metadata.
4. **Generate** – results:
   * Grain: one row per delivery item (LIPS: MANDT, VBELN, POSNR), from the expected-grain text.
   * Joins J01–J03 **Confirmed** (metadata FK). LIPS→T001W on (MANDT, WERKS) **Requires
     Validation** (shared key name only) – PLANT_NAME inherits Requires Validation.
     A name-only LIPS↔VTTP shortcut is found but **not used** because confirmed joins
     already connect those tables.
   * VTTP is not unique per delivery → fan-out risk → `one_vttp` CTE keeps the latest shipment
     item per delivery and the open questions ask the business to confirm that rule.
   * TRACKING_NUMBER → **Missing**, emitted as `CAST(NULL AS VARCHAR(35))`, listed as an open question.
   * SAP DATS dates → `TRY_TO_DATE(NULLIF(..., '00000000'), 'YYYYMMDD')`; leading zeros stripped
     where the requirement asks for it; QUAN → `CAST(... AS NUMBER(15,3))`.
5. **Review** – confirm the T001W join and the grain, assign owners to open questions, apply.
6. **Export** – XLSX (11 sheets), DOCX/PDF (10-section document), SQL, CSV, JSON.

## 6. Extending

* **New ERP** – ERP Master page (or `templates/erp_master.json`): name, aliases, date format
  (`ISO`, `SAP_DATS`, `JDE_JULIAN`, `YYYYMMDD_NUM`), client column, change-date columns.
* **New AI provider** – subclass `AIProvider` in `ai_service.py`, implement `complete()`,
  register it in `get_provider()` and `config.AI_PROVIDERS`.
* **Snowflake connectivity (future)** – add a `snowflake_connector.py` that reads
  `INFORMATION_SCHEMA.COLUMNS` / `TABLE_CONSTRAINTS` into the same canonical metadata
  records, and runs the DQ scripts; no other module needs to change.
