# 🚚 ERP Logistics Data Architect

AI-powered **multi-ERP mapping, data-modelling and Snowflake SQL assistant**, focused on logistics: sales orders,
deliveries, shipments, transportation, inventory, procurement, production and order fulfilment.

Ask *"ERP: SAP S/4HANA, T-Code: IQ09 — give me all backend tables, joins, grain and Snowflake query"* or paste a list of
business columns. You get back the source tables and fields, join keys, cardinality, grain, a fact/dimension design, a
Snowflake medallion design, seven SQL variants, data-quality queries and downloadable documents. Every mapping carries
an **evidence level**.

---

## 1. What it does

| Page | Purpose |
|---|---|
| 🏠 Home Dashboard | Knowledge-base coverage per ERP, quick starts |
| 💬 Ask ERP Assistant | Chat. The knowledge base answers first, then an optional LLM adds reasoning grounded in the KB and your uploaded docs (RAG) |
| 🔎 T-Code Explorer | SAP T-codes, or screens / reports / APIs for other ERPs. Gives the 15-point analysis: description, standard/custom, program, tables, keys, joins, grain, field mapping, SQL, history, dedup, incremental and validation |
| 📦 Business Object Mapping | Canonical field coverage for a business object, its KB data model and duplicate risks |
| 🧩 Column Mapping | Business column list → full source-to-target mapping with 16 attributes per column. Unmapped columns are highlighted, never dropped. AI can suggest sources for them (flagged 🟣) |
| 🗺️ Data Model Designer | **Architecture preview before documents**: ER diagram, PK/FK, cardinality, fan-out detection, fact/dim roles, grain, staging → silver → gold |
| ❄️ SQL Generator | Basic, joined, deduplicated, incremental, historical, `CREATE OR REPLACE VIEW` and dbt model. Includes sqlglot syntax validation, an editor, copy and `.sql` download |
| ⚖️ Cross-ERP Comparison | Field matrix across ERPs, highlights (common, missing, different meaning), canonical DDL and a multi-ERP `UNION ALL` |
| 🗄️ ERP Metadata Repository | Browse the KB. Upload data dictionaries (CSV/XLSX) and documents for RAG (PDF/DOCX/MD/JSON/YAML). Add new ERPs and extend them with tables, joins, mappings and T-codes. YAML import/export |
| 📄 Documentation & Downloads | Approve the architecture, preview each deliverable, then download DOCX, PDF, an 11-sheet Excel S2T, SQL, DQ SQL, ER diagrams (PNG/SVG/DOT/Mermaid), grain documentation, ingestion notes, or a ZIP of everything |
| ⚙️ Settings | LLM provider (Anthropic Claude / OpenAI / OpenAI-compatible) and Snowflake naming/CDC conventions |

### Accuracy model (no invented tables)

Every table, join and field mapping has one of these levels:

| | Level | Meaning |
|---|---|---|
| 🟢 | Standard / documented | Standard data-dictionary object, consistent with vendor documentation |
| 🔵 | Confirmed – uploaded metadata | Present in a data dictionary **you** uploaded |
| 🟡 | Inferred | Based on known ERP conventions; validate before production |
| 🔴 | Needs validation | Customer-specific or uncertain |
| 🟣 | AI-suggested | Proposed by the LLM, not in the KB |
| ⚫ | Not available | No source found; alternatives are suggested |

The SQL engine follows these rules:

- Joins come only from defined relationships, using breadth-first search from the business object's **anchor (grain)
  table**. It never joins on "same-looking" document numbers.
- `INNER JOIN` is used only for item → header joins. Everything else is a `LEFT JOIN`.
- 1:N joins that change the grain are flagged (**FAN-OUT**). The KB ships pre-aggregations that avoid them, for example
  delivered quantity and confirmed quantity per sales-order item.
- `ROW_NUMBER()` is used only for justified replication dedup (latest version per source primary key, ordered by the CDC
  timestamp). It never uses `DISTINCT`.
- It applies SAP specifics: the `MANDT` client in every join, the `SPRAS` language filter on text tables,
  `DATS '00000000'` → NULL, NUMC fields kept as VARCHAR, and decoding of status fields A/B/C.
- It applies JDE specifics: Julian `CYYDDD` → DATE, implied decimals, and `F4211 ∪ F42119` history.

## 2. Project structure

```
erp_mapping_app/
├── app.py                      # entry point (st.navigation)
├── app_pages/                  # 11 Streamlit pages (named app_pages so Streamlit's legacy pages/ auto-discovery is not triggered)
├── services/
│   ├── erp_knowledge_service.py  # loads YAML KB, inheritance (ECC/S4 share sap_common), uploads, custom ERPs
│   ├── mapping_engine.py         # column → concept → source; join planning; grain / fan-out analysis
│   ├── transform_rules.py        # ERP type-aware transformations (DATS, Julian, NUMC, quantities…)
│   ├── sql_service.py            # 7 Snowflake query types + dbt sources.yml
│   ├── validation_service.py     # sqlglot validation + 7 families of DQ / reconciliation queries
│   ├── model_service.py          # ER diagrams (Graphviz/Mermaid), fact/dim, medallion layers, ingestion advice
│   ├── comparison_service.py     # cross-ERP matrix, canonical DDL, multi-ERP UNION
│   ├── assistant_service.py      # intent parsing, T-code analysis, offline answers, LLM context
│   ├── llm_service.py            # Anthropic / OpenAI / OpenAI-compatible (streaming)
│   ├── rag_service.py            # per-ERP document collections, chunking, TF-IDF retrieval
│   └── document_service.py       # DOCX / PDF / XLSX / SQL / ZIP deliverables
├── knowledge_base/             # one YAML per ERP (sap/, jde/, oracle/, syspro/, iscala/, blue_yonder/, infor/, microsoft/, manhattan/, koerber/, gt_nexus/)
├── config/
│   ├── settings.py             # confidence vocabulary, LLM + SQL defaults
│   └── canonical_model.yaml    # ERP-neutral logistics concepts (≈70) with synonyms + target Snowflake columns
├── utils/                      # shared UI state + components
├── data/                       # runtime: uploaded metadata, RAG index, custom ERPs
├── tests/                      # pytest: services + Streamlit AppTest end-to-end
├── requirements.txt  packages.txt  .streamlit/config.toml  .streamlit/secrets.toml.example
```

## 3. Run locally

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# optional, for PNG/SVG diagram export and diagrams inside DOCX/PDF:
#   macOS: brew install graphviz   ·   Ubuntu: sudo apt-get install graphviz   ·   Windows: choco install graphviz
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # optional: add an API key
streamlit run app.py
python -m pytest tests -q                                   # 8 tests
```

Without an API key the app runs in **offline knowledge-base mode**. Mapping, SQL, diagrams and documents all work. The
LLM only adds free-form reasoning and AI suggestions.

## 4. Deploy to Streamlit Community Cloud (Phase 7)

1. Push this folder to a GitHub repository (the `.gitignore` already excludes secrets and runtime data).
2. Go to <https://share.streamlit.io> → **Create app** → pick the repository and branch → **Main file path: `app.py`**.
3. **Advanced settings → Python version:** 3.11 or 3.12.
4. **Advanced settings → Secrets:** paste the contents of `.streamlit/secrets.toml.example` with your key, for example
   ```toml
   ANTHROPIC_API_KEY = "sk-ant-..."
   ANTHROPIC_MODEL = "claude-sonnet-5-5"
   ```
5. Deploy. `packages.txt` installs the Graphviz binary (needed for PNG/SVG and diagrams inside Word/PDF). In-app
   diagrams render in the browser even without it.
6. Optional: restrict access under **Settings → Sharing**. The app can hold proprietary ERP metadata.

**Persistence on Streamlit Cloud:** the container filesystem is *ephemeral*. Uploads, the RAG index and custom ERPs
reset when the app restarts. To keep knowledge permanently:

- Export the ERP YAML (Metadata Repository → Import/Export) and commit it to `knowledge_base/<erp>/`.
- Commit data dictionaries as CSV and re-upload them, or extend `rag_service.VectorStore` to persist to Snowflake,
  S3 or a managed vector DB (the `add / search / delete_source` interface is isolated for this).

## 5. Add an ERP or extend knowledge (no code changes)

**From the UI:** Metadata Repository → *Add new ERP*, then *Extend ERP* (tables, relationships, concept mappings,
T-codes), or *Upload table/column metadata*.

**As a file:** add `knowledge_base/<folder>/<erp_id>.yaml`:

```yaml
erp:
  id: epicor_kinetic
  name: Epicor Kinetic
  family: Epicor
  storage: relational            # relational | api | mixed | customer_specific
  versions: ["2024"]
  inherits: null                 # e.g. sap_common to reuse another ERP's knowledge
  physical_name_template: null   # e.g. "{table}{company}{year}" for iScala
tables:
  OrderHed:
    description: Sales order header
    primary_key: [Company, OrderNum]
    confidence: standard         # standard | uploaded | inferred | validate
    role: Why this table matters
    fields: {OrderNum: {description: Order number, type: INT}}
relationships:
  - {from: OrderHed, to: OrderDtl, on: [[Company, Company], [OrderNum, OrderNum]], cardinality: "1:N", confidence: standard}
concepts:                        # canonical concept id -> source (see config/canonical_model.yaml)
  sales_order_number: {table: OrderHed, field: OrderNum, confidence: standard}
  open_quantity: {expr: "{OrderDtl.OrderQty} - {OrderDtl.ShippedQty}", confidence: inferred}
business_objects:
  sales_order: {anchor: OrderDtl, header: OrderHed, grain: One row per order line, model_type: Header/item fact,
                business_key: [Company, OrderNum, OrderLine], duplicate_risks: []}
transactions:
  "Order Entry": {description: Sales order entry, type: standard, business_object: sales_order, tables: [OrderHed, OrderDtl]}
```

`virtual_tables` (pre-aggregated CTEs), `field_overrides` per business object, `base_filter` and `history_union` are also
supported. See `knowledge_base/sap/sap_common.yaml` and `knowledge_base/jde/jde_e1.yaml` for examples.

New business concepts go into `config/canonical_model.yaml`. Synonyms drive the fuzzy matching of free-text column names.

## 6. Knowledge-base coverage and limitations

- **Deepest coverage:** SAP S/4HANA and ECC (SD, LE-SHP, LE-TRA, MM-IM, MM-PUR, PP, serial numbers), Oracle EBS,
  JD Edwards and D365. Good coverage: SYSPRO, Blue Yonder WMS and Infor M3/LN. Starter coverage only: Manhattan,
  Körber, Oracle Fusion and Infor Nexus.
- **iScala** uses positional column names (`OR01001`…) that differ by release. Its KB is table-level only, and field
  mappings show as "Needs validation" until you upload your iScala dictionary. Shipment structures are not pre-populated.
- The KB is curated starter content, not a full vendor data dictionary. Upload your own data dictionary to confirm
  fields: SAP DD02L/DD03L/DD03T, JDE F9860/F9210 and so on. Physical names also depend on your replication tool, for
  example lower-case columns from Synapse Link, or VARIANT JSON for API sources.
- SAP TM freight orders, EWM tables and S/4HANA CDS views are mentioned in the notes but not modelled as tables yet.
- RAG uses TF-IDF, so it is lightweight and runs on the free tier. The store can be swapped for embeddings or a vector DB.

## 7. Development phases

| Phase | Status |
|---|---|
| 1 · Streamlit UI, ERP selector, assistant, manual T-code / object / column input | ✅ |
| 2 · ERP knowledge retrieval + metadata upload (KB + RAG) | ✅ |
| 3 · Source-to-target mapping, grain, joins, logical modelling | ✅ |
| 4 · Snowflake SQL generation + validation | ✅ |
| 5 · DOCX, PDF, Excel, SQL downloads (preview-before-download) | ✅ |
| 6 · Cross-ERP comparison + canonical model | ✅ |
| 7 · Streamlit Cloud deployment guide | ✅ (section 4) |

Suggested next steps: a Snowflake connector to run the DQ queries live, persistent storage for uploads, embedding-based
RAG, SAP TM / EWM / CDS-view knowledge packs, and returns/STO business objects for non-SAP ERPs.
