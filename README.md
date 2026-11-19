# ERP → SAP Data Mapper (Streamlit)

Maps **serial numbers (SAP IQ09)** and **outbound deliveries (SAP LIKP/LIPS)** from many ERPs
to one SAP-style model and generates **Snowflake** views. You can union the views for
cross-ERP reporting.

| ERP | Serial numbers | Deliveries | Confidence |
|---|---|---|---|
| SAP ECC / S/4HANA | ✅ | ✅ | high |
| Epicor iScala | ✅ | ✅ | high |
| Oracle E-Business Suite | ✅ | ✅ | high |
| JD Edwards EnterpriseOne | ✅ | ✅ | medium |
| Microsoft Dynamics 365 F&O | ✅ | ✅ | medium |
| SYSPRO 8 | ✅ | ✅ | medium |
| Oracle NetSuite | ✅ | ✅ | medium |
| Blue Yonder WMS (JDA/RedPrairie) | ✅ | ✅ | medium |
| Infor Nexus (GT Nexus) | — | ✅ (shipments, JSON) | low / illustrative |

*Confidence*: **high** = column names checked against a published data dictionary;
**medium** = standard tables, but confirm the columns against your version or replication tool;
**low** = depends on how the API payload is landed.

## What the app does
- **Field mapping**: side-by-side matrix of every SAP field and its source in each ERP. Download as CSV.
- **SQL**: one `CREATE OR REPLACE VIEW` per ERP (same columns and types) plus a combined `UNION ALL` view.
  Download as a ZIP or a single deploy script.
- **Customise mapping**: override any field's SQL expression in the browser.
- **Source tables & notes**: tables used and caveats for each ERP.
- **Run in Snowflake** (optional): preview rows or create the views, once secrets are configured.

Settings per ERP (sidebar): schema name, language, client/company, iScala company code,
JDE decimal divisors, SAP ECC vs S/4HANA, status-profile source (T370T/JSTO), and others.

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Community Cloud
1. Push this folder to a GitHub repository (`app.py` at the root).
2. Go to https://share.streamlit.io → **Create app** → pick the repo, branch and `app.py`.
3. Optional: under **Advanced settings → Secrets**, paste the content of
   `.streamlit/secrets.toml.example` with your Snowflake details to enable the *Run in Snowflake* tab.
4. Deploy.

## Adding another ERP
Add a dictionary to `erp_catalog.py` (copy an existing one) and register it in `ERPS`.
List only the canonical fields you can map; the rest are returned as `NULL` automatically.
Use `{{param}}` tokens for anything configurable.
