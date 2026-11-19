# ERP Data Modelling Intelligence Platform

A Streamlit + PostgreSQL application that helps data engineers and modellers go from an **ERP name, T-Code,
business use case or list of business columns** to:

backend tables → column-level mapping → validated joins → source SQL → Snowflake dimensional model →
dbt project (staging / intermediate / marts / snapshots / tests) → mapping & technical design documents.

All of this comes from a **metadata repository**: every table, column, join and mapping carries a source,
a confidence score and a validation status. The LLM is optional. Without one, every module still works
from metadata alone. With one configured, the assistant writes its answers from that same metadata and
flags any identifier that is not in the repository.

See **ARCHITECTURE.md** for the design: schema, ingestion, RAG, relationship validation and folder layout.

---

## 1. Quick start

### Option A — Docker (recommended)
```bash
cp .env.example .env            # set POSTGRES_PASSWORD, ADMIN_USERNAME, ADMIN_PASSWORD
echo "POSTGRES_PASSWORD=change_me" >> .env
docker compose up --build       # http://localhost:8501
```

### Option B — local Python
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # needs the Graphviz binary: apt install graphviz / brew install graphviz
cp .env.example .env                     # set DATABASE_URL (PostgreSQL or sqlite:///./erpdm.db) and admin creds
python scripts/init_db.py                # Alembic migrations + FTS indexes + admin + sample metadata
streamlit run app.py
```

On start-up, the app creates the first administrator from `ADMIN_USERNAME` / `ADMIN_PASSWORD`. In
non-production environments with no users at all, the login screen instead offers a one-time
"create first administrator" form. With `APP_ENV=production`, the admin must change the bootstrap password
at first sign-in.

### Try the reference workflow (spec §9)
1. Sidebar → ERP **SAP NSAP (internal instance)**.
2. **T-Code Intelligence** → `IQ09`. Review the overview, tables, joins, ER diagram, SQL, model and incremental strategy.
3. **➡️ Use this scope in the workflow**.
4. **SQL Generator** → adjust columns, pick a dialect, add filters, then download.
5. **Column Mapping** → *Generate mapping document* → edit → **Approve**. Approved terms are added to the glossary.
6. **dbt Model Generator** → choose SCD1 or SCD2 and the CDC options → **Generate** → download the ZIP.
7. **Column Mapping → Prepare downloads** for the Excel mapping, the Word/PDF technical design, the ER and
   data-flow PNGs, and the SQL.

---

## 2. Modules

| Module | What it does (backend service) |
|---|---|
| Dashboard | Repository counts, per-ERP coverage, join validation status, recent activity (`metadata`, `audit`) |
| ERP Explorer | Search across tables, columns, T-codes, processes, terms and use cases (PostgreSQL FTS); table browser with pagination, columns, relationships, lineage and version history (`search`, `metadata`) |
| T-Code Intelligence | Transaction → tables (by role), business logic, columns, PK/FK, joins, ER diagram, SQL, Snowflake model, incremental strategy, data flow; flags assumed structures (`tcode`) |
| Business Use Case Explorer | Natural-language requirement → ERP, domain, use case, process, column matches, mandatory and optional joins, SQL, target model (`usecase`) |
| Table Relationship Explorer | Join planner (shortest weighted tree; prefers verified joins; never bridges transactions through master tables), fan-out detection, pinning, approve/reject, name-based *candidates*, AI suggestions (`relationships`) |
| SQL Generator | Snowflake, SQL Server, Oracle, PostgreSQL, Redshift and Databricks via SQLGlot; provenance comments; join, filter, duplicate and null explanations; performance notes; validation queries; editor with syntax check (`sqlgen`) |
| Column Mapping | Upload/type/workflow business columns → matching (explicit / business name / glossary / technical / fuzzy) → full STTM with transformations and DQ rules → edit → approve → Excel/Word/PDF (`mapping`, `docgen`) |
| dbt Model Generator | sources.yml with freshness, staging (rename, cast, blank→NULL, SAP dates, soft-delete, CDC filter), intermediate joins (status tables aggregated, fan-out de-dup), dims/facts (incremental merge/append, SCD1 or SCD2 via snapshot), schema tests, singular tests, README, ZIP (`modelling`, `dbtgen`) |
| Data Flow Explorer | Process → transaction → app components → tables → extraction → RAW → staging → intermediate → marts → BI (`dataflow`, `diagrams`) |
| AI ERP Assistant | Grounded RAG over the repository and uploaded documents, with citations and a hallucination guard; deterministic answers when no LLM is configured (`assistant`, `llm`) |
| Metadata Management | Upload and preview before commit; ERP onboarding; validation queue; manual relationships; document index; versions and audit; package export (`ingestion`, `metadata`) |
| Settings | Password change, LLM status and connection test, user and role administration, system info, recent errors |

---

## 3. Accuracy and validation model

| Source type | Default status | Used by generators? |
|---|---|---|
| `ddl_constraint` (DDL FKs, SAP DD05S) | verified | yes |
| `user_verified` (approved in the UI) | verified | yes |
| `trusted_documentation` / packages | requires_validation or verified | yes, with a warning |
| `standard_erp_knowledge` (shipped seed) | requires_validation | yes, with a provenance comment in the SQL |
| `existing_sql` (joins parsed from your SQL) | pending | only with "Include pending joins" |
| `ai_inferred` (LLM suggestions, confidence ≤ 0.6) | pending | only with "Include pending joins" |
| `unverified_assumption` (name-match candidates) | pending | never, until approved |
| any source, `rejected` | rejected | never |

* Every relationship is structurally checked when it is stored: the tables and columns must exist, the
  data types must be compatible, and the cardinality must be consistent with the keys. For example, the
  sample Oracle `PO_LINES_ALL → MTL_SYSTEM_ITEMS_B` join is flagged because `ORGANIZATION_ID` is not constrained.
* Generated SQL and dbt models carry provenance on every join. Joins that would multiply rows are detected
  from key coverage and de-duplicated with `QUALIFY ROW_NUMBER()`, or with a sub-query in dialects that
  have no `QUALIFY`.

### About the sample metadata
* **SAP ECC**: a curated subset of standard tables (PM/equipment, MM, SD/LE, purchasing, inventory), labelled
  `standard_erp_knowledge / requires_validation`.
* **SAP S/4HANA**: derived from ECC with documented differences (`MATDOC` replaces `MKPF`/`MSEG`, 40-character `MATNR`).
* **SAP NSAP** and **SAP EBS**: internal instances whose real structure is unknown. They are seeded as an
  explicit **assumption** copied from ECC, with confidence capped at 0.6 and 0.5. Upload DDIC extracts to verify them.
* **Oracle EBS R12**: a purchasing sample.
* **JDE, Infor, Dynamics, SYSPRO, iScala, Blue Yonder, Körber, Manhattan, GT Nexus**: registered with no
  tables. The app reports "metadata onboarding required" rather than guessing.

---

## 4. Onboarding a new ERP (no code changes)

1. **Metadata Management → ERP systems**: add the system (code, client/tenant column, language key, extraction method).
2. **Upload & ingest** any of:
   * **DDL** (`CREATE TABLE … PRIMARY KEY`, `ALTER TABLE … FOREIGN KEY`). FKs become verified joins.
   * **SAP DDIC extracts** from SE16/SE16N: `DD03L` (TABNAME, FIELDNAME, KEYFLAG, DATATYPE, LENG, DECIMALS, POSITION
     plus DDTEXT from DD03T/DD04T) and `DD05S` (TABNAME, FIELDNAME, FORTABLE, FORKEY, CHECKTABLE, CHECKFIELD).
     Tick *authoritative extract* to mark the structure as verified.
   * **Table/column inventories** (`TABLE_NAME, COLUMN_NAME, DATA_TYPE, …`; aliases are detected automatically).
   * **FK lists** (`FK_TABLE, FK_COLUMN, PK_TABLE, PK_COLUMN`).
   * **T-code lists**, **mapping workbooks** (these build the glossary), and **existing SQL** (joins become pending candidates).
   * **Metadata packages** (YAML/JSON). This is the full format, with tables, relationships, transactions,
     terms, use cases and processes. See `erpdm/seed/packages/*.yaml`; packages can `extends:` another one.
   * **Documents** (PDF/DOCX/TXT/MD/dbt files) are chunked and indexed for the assistant. They never turn into joins automatically.
3. Review in the **Validation queue** and approve or reject. Every change is versioned (`metadata_versions`) and audited (`audit_logs`).
4. **Export package** at any time to back up or promote metadata between environments.

---

## 5. Configuration

All settings are environment variables (see `.env.example`). Credentials are never stored in code or in the database.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://…` (production) or `sqlite:///…` (demo/tests) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Bootstrap admin, used only if no admin exists |
| `LLM_PROVIDER` | `none` (default), `anthropic`, `openai`, `azure_openai`, `ollama` |
| `LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_API_VERSION` | Provider settings (Azure: base URL is the resource endpoint and the model is the deployment name) |
| `SESSION_TIMEOUT_MINUTES`, `MAX_FAILED_LOGINS`, `LOCKOUT_MINUTES`, `MIN_PASSWORD_LENGTH` | Security policy |
| `RAW_DATABASE`, `TARGET_DATABASE`, `RAW_LOADED_AT_COLUMN`, `DEFAULT_LANGUAGE_KEY` | Generator defaults |

### Roles
| Role | Can |
|---|---|
| `admin` | everything, including metadata management, validation and users |
| `modeller` | explore, generate SQL/mappings/dbt, edit mappings, use the assistant |
| `viewer` | explore and use the assistant (read-only) |

---

## 6. Testing and quality

```bash
pip install -r requirements-dev.txt
pytest                                                    # SQLite: 40 tests (unit + integration + UI smoke)
TEST_DATABASE_URL=postgresql+psycopg2://u:p@host/db pytest   # same suite on PostgreSQL via Alembic migrations
make security                                             # bandit + pip-audit
```
The suite covers auth and lockout, RBAC, seed isolation, S/4 derivation, relationship validation, approval
workflow, FTS search, join planning (bridges, fan-out, rejected joins), SQL in all six dialects (each parsed
by SQLGlot), filter-injection rejection, DDL/DD03L/DD05S/SQL/document ingestion, the full IQ09
end-to-end workflow (SQL → mapping → approval → dbt ZIP → XLSX/DOCX/PDF), the assistant in metadata mode,
LLM mode (with a mocked provider), the hallucination guard, and a Streamlit AppTest that opens all 12 pages.
CI is in `.github/workflows/ci.yml`.

---

## 7. Deployment

* **Docker / any container platform**: `Dockerfile` (non-root user, Graphviz, health check on `/_stcore/health`).
  Start-up runs `scripts/init_db.py`, which is idempotent (migrations, indexes, admin, seed).
* **docker compose**: `db` (PostgreSQL 16 with a volume), `app`, and an optional `backup` profile.
* **Streamlit Community Cloud**: point it at `app.py`. `packages.txt` installs Graphviz. Paste
  `.streamlit/secrets.toml.example` into Secrets (root-level keys become environment variables) and use a
  managed PostgreSQL instance.
* **Migrations**: `alembic upgrade head` (new revisions: `alembic revision --autogenerate -m "…"`).

### Backup and recovery
* `scripts/backup.sh`: `pg_dump` in custom format, an archive-integrity check, and retention
  (`RETENTION_DAYS`, default 14). Schedule it daily (cron / CronJob / `docker compose --profile backup run backup`).
* `scripts/restore.sh <file>`: `pg_restore --clean`, then `alembic upgrade head`.
* Metadata can also be exported per ERP as a YAML package (Metadata Management → Export package).

### Monitoring
* JSON logs on stdout (optional `LOG_FILE`) are ready for any log shipper. Errors are also kept in an
  in-process buffer that is visible under Settings → System.
* A container health check is included. Every login, upload, validation, mapping, generation and
  assistant query is recorded in `audit_logs`.

---

## 8. Security notes
* bcrypt password hashing, a password policy, account lockout, session timeout and forced password change for bootstrap/reset users.
* RBAC is enforced in the navigation and again inside each action.
* All database access goes through the SQLAlchemy ORM with bound parameters. Generated SQL is never executed
  by the app. User filters are parsed with SQLGlot, and statements and comments in them are rejected.
* Uploads are limited by type and size and are previewed before commit. YAML uses `safe_load`.
* XSRF protection is on, CORS is restricted (`.streamlit/config.toml`), and error details are hidden in production.
* LLM keys are read only from the environment. Only repository metadata (no business data) is sent to the provider.

## 9. Known limitations / next steps
* The sample metadata is generic standard knowledge. Validate it against your systems before production use
  (that is what the validation queue is for).
* Vector search (pgvector) is an extension point; v1 retrieval is PostgreSQL full-text search plus BM25 on SQLite.
* Single sign-on (SAML/OIDC) is not included. Front the app with your identity-aware proxy if you need it.
* The SQL editor is a text area with a validate button and a syntax-highlighted preview; it is not a full IDE editor.
