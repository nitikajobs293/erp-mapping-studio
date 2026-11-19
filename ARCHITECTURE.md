# ERP Data Modelling Intelligence Platform — Architecture

This document covers the eight design items requested before implementation:
requirements analysis, system architecture, database schema, metadata ingestion,
AI/RAG architecture, ERP knowledge maintenance, relationship validation and the
project folder structure.

---

## 1. Requirements analysis

| Theme | What it really means for the design |
|---|---|
| Multi-ERP, system-specific | Every metadata row is scoped by `erp_system_id`. There is **no** shared "SAP" table catalogue; SAP ECC, S/4HANA and SAP NSAP are separate systems even when table names coincide. |
| Onboard ERPs without code changes | ERPs, modules, tables, columns, relationships, transactions, glossary terms, use cases and extraction methods are all **data**. A new ERP is a metadata package (YAML/JSON) or uploaded DDL/DDIC/CSV — never a code change. |
| Trustworthiness over fluency | Each table, column, relationship, transaction and mapping row carries `source_type`, `confidence` and `validation_status`. The generators consume only trusted relationships by default and stamp provenance into every SQL/dbt/doc output. |
| Connected workflow | A single `WorkflowState` (Pydantic model in Streamlit session) carries ERP → T-Code → tables → columns → join plan → SQL → mapping → dbt → documents across pages. |
| Real application | PostgreSQL + SQLAlchemy + Alembic, bcrypt auth with lockout, RBAC, audit trail, metadata versioning, tests, Docker. Every module has deterministic backend logic; the LLM is an **optional enhancer**, never a dependency. |

**Key design decision — the LLM is never the source of truth.** Table names, columns and joins
come from the repository. The LLM may (a) phrase answers from retrieved metadata, (b) propose
relationships or mappings which are stored as `ai_inferred / pending` suggestions for a human
to approve. If no LLM is configured, every module still works.

## 2. System architecture

```
┌──────────────────────────── Streamlit UI (app.py + ui/pages) ─────────────────────────────┐
│ Dashboard │ ERP Explorer │ T-Code │ Use Case │ Relationships │ SQL │ Mapping │ dbt │ Flow │ │
│ Assistant │ Metadata Mgmt (admin) │ Settings          ← WorkflowState in session_state     │
└──────────────┬─────────────────────────────────────────────────────────────────────────────┘
               │ calls (no SQL in the UI layer)
┌──────────────▼──────────────────────── Service layer (erpdm/services) ─────────────────────┐
│ auth  audit  metadata  search  ingestion  tcode  usecase  relationships  sqlgen  mapping   │
│ dbtgen  modelling  docgen  dataflow  diagrams  llm  retrieval  assistant                   │
└──────────────┬───────────────────────────────────────┬─────────────────────────────────────┘
               │ SQLAlchemy 2.x ORM                     │ httpx (optional)
┌──────────────▼──────────────┐              ┌──────────▼───────────────────────────┐
│ PostgreSQL (prod)            │              │ LLM provider (env-configured):        │
│  • metadata repository       │              │ anthropic | openai | azure_openai |   │
│  • search_index + GIN FTS    │              │ ollama | none                          │
│  • audit / versions          │              └───────────────────────────────────────┘
│ SQLite (tests / quick demo)  │
└──────────────────────────────┘
```

Layering rules: UI → services → ORM. Services take an explicit `Session` and return
Pydantic/dataclass results, so they are unit-testable without Streamlit.

## 3. Database schema (core tables)

| Table | Purpose / key columns |
|---|---|
| `users` | username, email, bcrypt `password_hash`, `role` (admin/modeller/viewer), lockout counters |
| `erp_systems` | `code` (unique), name, vendor, version, `extraction_method`, `landscape_notes`, `is_reference` |
| `erp_modules` | erp_id, code, name |
| `business_processes` | erp_id, module_id, domain, name, sub_process |
| `erp_tables` | erp_id, module_id, name, description, `table_type` (table/view/cds_view/cluster), `table_class` (master/transaction/text/config/document_flow), `change_tracking` JSON (columns, reliability), `delete_strategy`, provenance trio |
| `erp_columns` | table_id, name, description, data_type, length, is_pk, is_nullable, ordinal, business_name, business_definition, provenance trio |
| `relationships` | erp_id, from/to table, `join_columns` JSON `[{"from":..,"to":..}]`, `extra_condition`, join_type, cardinality, relationship_type (header_item, master_detail, text, lookup, document_flow, reference), `is_mandatory`, description, business_purpose, provenance trio + evidence, validated_by/at |
| `transactions` | erp_id, code, name, description, business_purpose, transaction_type (tcode/screen/report), app_components JSON, business_logic, process_id, provenance trio |
| `transaction_tables` | transaction ↔ table with `role` (primary/secondary/text/lookup/status) |
| `business_terms` | erp_id, term, synonyms JSON, definition, table/column binding, provenance trio |
| `use_cases` | erp_id, name, domain, description, keywords JSON, table list JSON, root table |
| `documents` / `document_chunks` | uploaded docs, chunked text for retrieval |
| `search_index` | unified searchable projection of every metadata entity (Postgres GIN `to_tsvector` index) |
| `mappings` / `mapping_rows` | source-to-target documents, row-level validation status, approval |
| `generated_artifacts` | SQL scripts, dbt projects, documents (powers dashboard counters, reuse) |
| `metadata_versions` | JSON snapshot per change (entity_type, entity_id, version, change_type, changed_by) |
| `audit_logs` | who did what, when, on which entity |

**Provenance trio** on every knowledge row: `source_type` ∈ {`user_verified`, `ddl_constraint`,
`trusted_documentation`, `existing_sql`, `standard_erp_knowledge`, `ai_inferred`,
`unverified_assumption`}, `confidence` (0–1), `validation_status` ∈ {`verified`, `pending`,
`requires_validation`, `rejected`}.

## 4. Metadata ingestion

```
upload → detect format → parser → normalised ParsedMetadata (Pydantic) → preview/diff → commit
                                                              ↓
                                  metadata service: upsert + version snapshot + search_index + audit
```

| Input | Parser | Produces |
|---|---|---|
| Metadata package `.yaml/.json` | `parse_package` | full ERP (modules, tables, columns, relationships, T-codes, terms, use cases) |
| DDL `.sql` (CREATE TABLE / ALTER TABLE … FOREIGN KEY) | SQLGlot | tables, columns, PKs; FKs → relationships `ddl_constraint`, confidence 0.95 |
| Existing SELECT queries `.sql` | SQLGlot join extraction | relationships `existing_sql`, confidence 0.7, status `pending` |
| Table/column inventory `.csv/.xlsx` | header-alias detection (TABLE_NAME/TABNAME, COLUMN_NAME/FIELDNAME, …) | tables, columns |
| SAP DDIC extracts (DD02L/DD03L/DD02T/DD04T, DD05S/DD08L) | same CSV path with SAP aliases | tables, columns, keys; check-table FKs → relationships `ddl_constraint` |
| Mapping workbook `.xlsx/.csv` | mapping parser | business terms (glossary) bound to source columns |
| T-Code list `.csv/.xlsx` | header alias | transactions |
| Docs `.pdf/.docx/.txt/.md` | PyMuPDF / python-docx | chunks into `document_chunks` for retrieval (never auto-converted into joins) |
| dbt models / YAML | text chunks + `ref`/`source` lineage hints | document chunks |

Imports are idempotent (upsert by natural key) and every change creates a `metadata_versions` row.

## 5. AI / RAG architecture

1. **Entity grounding (deterministic):** detect T-codes, table names and column names in the
   question by exact lookup against the repository for the selected ERP.
2. **Retrieval:** Postgres full-text search over `search_index` + `document_chunks`
   (`websearch_to_tsquery`, `ts_rank`); on SQLite a Python BM25-style scorer. Results are scoped by ERP.
3. **Structured context:** for grounded tables, attach columns, keys and *trusted* relationships;
   for multiple tables, attach the join plan computed by the relationship engine.
4. **Generation:** the LLM receives a strict system prompt — answer only from context, cite
   `[TABLE]`/`[DOC]` ids, say "not in repository" otherwise.
5. **Guardrail:** answer tokens that look like table identifiers are checked against the
   repository; unknown ones are flagged in the UI as *unverified*.
6. **Fallback:** without an LLM (or on provider failure) the assistant answers from templates
   over the same grounded context (relationship explanation, join SQL, table listing, data flow),
   and asks the user to upload metadata when nothing is found.

Vector search (pgvector) is an optional extension point; v1 ships with full-text retrieval only.

## 6. Maintaining ERP-specific knowledge

* Each ERP is isolated by `erp_system_id`. Similar systems are seeded separately and flagged:
  SAP NSAP and SAP EBS are seeded from standard SAP ECC knowledge with
  `validation_status = requires_validation` until a DDIC extract from the actual instance is
  uploaded (DD03L/DD08L parser upgrades them to `ddl_constraint`/`verified`).
* S/4HANA differences are modelled explicitly (e.g. `MATDOC` replaces `MKPF`/`MSEG`, 40-char `MATNR`).
* Unknown systems (JDE, Infor, D365, Blue Yonder, Körber, Manhattan, GT Nexus …) are registered
  with **no tables** — the app says "metadata onboarding required" rather than guessing.

## 7. Relationship validation

```
candidate sources                 default trust                   used by SQL/dbt generators?
ddl_constraint (DDL / DD08L)      verified, 0.95                  yes
user_verified (admin approval)    verified, 1.0                   yes
trusted_documentation / package   verified or requires_validation yes (warning if not verified)
standard_erp_knowledge            requires_validation, 0.8–0.9    yes, with provenance comment
existing_sql (parsed joins)       pending, 0.7                    only if "include pending" is on
ai_inferred (LLM)                 pending, ≤0.6                   only if "include pending" is on
name-similarity discovery         unverified_assumption, ≤0.3     never, until approved
rejected                          —                               never
```

Validation checks: both tables and every join column must exist in the repository; join columns
on the "one" side must cover that table's PK for 1:N/1:1 claims (otherwise cardinality is downgraded
to "uncertain"); data-type compatibility; duplicates. Approval/rejection is audited and versioned.

The join planner (NetworkX) builds a weighted graph (`weight = 1 + (1 - confidence) * 3`, verified edges
cheaper) and computes the minimal set of edges connecting the selected tables from the chosen root
(shortest-path union / Steiner approximation). Every edge in a generated query carries its provenance.

## 8. Project folder structure

```
erp_platform/
├── app.py                       # Streamlit entry: login, RBAC-aware navigation
├── erpdm/
│   ├── config.py                # pydantic-settings, env-based
│   ├── logging_setup.py
│   ├── constants.py             # enums: source types, statuses, roles, permissions
│   ├── schemas.py               # Pydantic DTOs (ParsedMetadata, JoinPlan, MappingRow, …)
│   ├── workflow.py              # WorkflowState carried across pages
│   ├── db/  base.py  models.py  init_db.py
│   ├── services/
│   │   auth.py audit.py metadata.py search.py ingestion.py
│   │   tcode.py usecase.py relationships.py sqlgen.py mapping.py
│   │   modelling.py dbtgen.py docgen.py dataflow.py diagrams.py
│   │   llm.py retrieval.py assistant.py
│   └── seed/  loader.py  packages/*.yaml
├── ui/  components.py  pages/*.py
├── migrations/  (Alembic)
├── tests/  unit + integration (SQLite and PostgreSQL)
├── scripts/  backup.sh  restore.sh  init_db.py
├── Dockerfile  docker-compose.yml  requirements.txt  .env.example  .streamlit/config.toml
└── README.md  ARCHITECTURE.md
```
