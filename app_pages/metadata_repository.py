import io

import pandas as pd
import streamlit as st
import yaml

from config.settings import CONFIDENCE
from utils.ui import repo, setup_page, sidebar_context, store

setup_page()
erp = sidebar_context()
r = repo()
st.title("🗄️ ERP Metadata Repository")
st.caption("Browse the knowledge base, upload data dictionaries and documentation (RAG), and add or extend ERP systems "
           "— no code changes needed.")

tabs = st.tabs(["📚 Browse knowledge", "⬆️ Upload table/column metadata", "📄 Documents for RAG", "➕ Add new ERP",
                "🛠️ Extend ERP", "🔁 Import / export YAML"])

# ------------------------------------------------------------------ browse
with tabs[0]:
    if erp.meta.get("notes"):
        st.info(erp.meta["notes"])
    sub = st.radio("View", ["Tables", "Relationships", "Concept mappings", "Business objects", "T-codes / screens"], horizontal=True)
    if sub == "Tables":
        tdf = pd.DataFrame([{"Table": t, "Description": d.get("description", ""), "Module": d.get("module", ""),
                             "PK": ", ".join(d.get("primary_key", []) or []), "Fields": len(d.get("fields") or {}),
                             "Confidence": CONFIDENCE.get(d.get("confidence", "validate"), {}).get("label", ""),
                             "Virtual": "yes" if d.get("virtual") else ""} for t, d in erp.all_tables().items()])
        q = st.text_input("Filter tables / descriptions")
        if q:
            tdf = tdf[tdf.apply(lambda x: q.lower() in " ".join(map(str, x)).lower(), axis=1)]
        st.dataframe(tdf, width="stretch", hide_index=True)
        t = st.selectbox("Table details", list(erp.all_tables()))
        if t:
            d = erp.table(t)
            st.markdown(f"**{t}** — {d.get('description', '')}  \n{d.get('role', '')}  \n{d.get('notes', '')}")
            st.dataframe(pd.DataFrame([{"Field": f, "Description": v.get("description", ""), "Type": v.get("type", ""),
                                        "PK": "✔" if f in (d.get("primary_key") or []) else "",
                                        "Source": v.get("source", "knowledge base")} for f, v in (d.get("fields") or {}).items()]),
                         width="stretch", hide_index=True)
            if d.get("sql"):
                st.code(d["sql"], language="sql")
    elif sub == "Relationships":
        st.dataframe(pd.DataFrame([{"From": x["from"], "To": x["to"], "On": " AND ".join(f"{a}={b}" for a, b in x.get("on", [])),
                                    "Cardinality": x.get("cardinality", ""), "Filter": x.get("filter", ""),
                                    "Confidence": x.get("confidence", ""), "Label": x.get("label", "")} for x in erp.relationships]),
                     width="stretch", hide_index=True)
    elif sub == "Concept mappings":
        cat = r.concept_catalog()
        st.dataframe(pd.DataFrame([{"Concept": cat.get(c, {}).get("label", c), "Id": c,
                                    "Source": v.get("expr") or f"{v.get('table')}.{v.get('field')}",
                                    "Confidence": v.get("confidence"), "Note": v.get("note", "")} for c, v in erp.concepts.items()]),
                     width="stretch", hide_index=True)
    elif sub == "Business objects":
        for k, v in erp.business_objects.items():
            with st.expander(k):
                st.json(v)
    else:
        st.dataframe(pd.DataFrame([{"Code": k, "Description": v.get("description", ""), "Type": v.get("type", ""),
                                    "Object": v.get("business_object", ""), "Tables": ", ".join(v.get("tables", []))}
                                   for k, v in erp.transactions.items()]), width="stretch", hide_index=True)

# ------------------------------------------------------------------ metadata upload
with tabs[1]:
    st.markdown(f"Upload a **table/column inventory** for **{erp.name}** (CSV or Excel). Recognised columns: "
                "`table`, `field`, `description`, `data_type`, `key` (PK/Y/X), `table_description`, `module`, "
                "`business_concept` (optional canonical concept id to map directly). Common alternatives such as "
                "SAP DD03L `TABNAME/FIELDNAME/KEYFLAG/ROLLNAME` + DD03T `DDTEXT` are accepted.")
    up = st.file_uploader("Metadata file", type=["csv", "xlsx", "xls"], key="meta_up")
    if up:
        df = pd.read_csv(up) if up.name.lower().endswith(".csv") else pd.read_excel(up)
        st.dataframe(df.head(50), width="stretch")
        replace = st.checkbox("Replace previously uploaded metadata for this ERP")
        also_rag = st.checkbox("Also index as a RAG document", value=True)
        if st.button("💾 Save metadata", type="primary"):
            try:
                n = r.save_uploaded_metadata(erp.id, df, replace=replace)
                if also_rag:
                    store().add_document(erp.id, up.name, up.getvalue(), st.session_state.erp_version)
                st.success(f"Saved {n} rows. Fields now show as '🔵 Confirmed - uploaded metadata' in mappings.")
            except ValueError as exc:
                st.error(str(exc))
    if erp.uploaded_fields:
        st.markdown(f"**Uploaded tables:** {', '.join(sorted(erp.uploaded_fields))}")
        if st.button("🗑️ Clear uploaded metadata for this ERP"):
            r.clear_uploaded_metadata(erp.id)
            st.rerun()
    tmpl = pd.DataFrame([{"table": "LIKP", "field": "ZZCARRIER_REF", "description": "Custom carrier reference",
                          "data_type": "CHAR(20)", "key": "", "business_concept": "tracking_number"}])
    st.download_button("⬇️ Template CSV", tmpl.to_csv(index=False), file_name="metadata_template.csv")

# ------------------------------------------------------------------ RAG documents
with tabs[2]:
    st.markdown("Upload ERP documentation, vendor PDFs, API specifications or notes. They are chunked and indexed per ERP "
                "(TF-IDF) and retrieved to ground assistant answers, with source references.")
    target = st.selectbox("Collection", ["(this ERP) " + erp.name, "(global - all ERPs)"])
    erp_target = erp.id if target.startswith("(this") else "_global"
    docs = st.file_uploader("Documents", type=["pdf", "txt", "md", "json", "yaml", "yml", "docx", "csv", "xlsx"],
                            accept_multiple_files=True, key="rag_up")
    if docs and st.button("📥 Index documents", type="primary"):
        total = 0
        for d in docs:
            total += store().add_document(erp_target, d.name, d.getvalue(), st.session_state.erp_version)
        st.success(f"Indexed {total} chunks from {len(docs)} document(s).")
    st.dataframe(store().sources(), width="stretch", hide_index=True)
    q = st.text_input("🔍 Test retrieval", placeholder="e.g. shipment status field")
    if q:
        for h in store().search(q, erp.id, k=5):
            st.markdown(f"**{h['source']}** · {h['location']} · score {h['score']:.2f}")
            st.caption(h["text"][:600])
    rm = st.selectbox("Remove a document", [""] + sorted({c.source for c in store().chunks}))
    if rm and st.button("Remove"):
        for c in list({(c.erp_id, c.source) for c in store().chunks if c.source == rm}):
            store().delete_source(c[0] if c[0] != "_global" else "", c[1])
        st.rerun()

# ------------------------------------------------------------------ add ERP
with tabs[3]:
    with st.form("add_erp"):
        c = st.columns(3)
        name = c[0].text_input("ERP / WMS / TMS name", placeholder="e.g. Epicor Kinetic")
        erp_id = c[1].text_input("Id (lowercase)", placeholder="epicor_kinetic")
        family = c[2].text_input("Family / vendor", placeholder="Epicor")
        c = st.columns(3)
        storage = c[0].selectbox("Data access", ["relational", "api", "mixed", "customer_specific"])
        versions = c[1].text_input("Versions (comma separated)", placeholder="2023, 2024")
        inherits = c[2].selectbox("Inherit from (optional)", [""] + sorted(r.erps))
        notes = st.text_area("Notes (naming conventions, extraction method, …)")
        if st.form_submit_button("➕ Create ERP", type="primary"):
            if not name:
                st.error("Name is required")
            else:
                path = r.add_custom_erp(erp_id or name, name, family, storage,
                                        [v.strip() for v in versions.split(",") if v.strip()], notes, inherits or None)
                st.success(f"Created {path.name}. Select it in the sidebar, then upload metadata or extend it.")
    st.caption("Custom ERPs are saved under data/custom_erps/. On Streamlit Community Cloud the filesystem is ephemeral — "
               "export YAML (last tab) and commit it to knowledge_base/ in your repo to make it permanent.")

# ------------------------------------------------------------------ extend ERP
with tabs[4]:
    st.markdown(f"Add knowledge to **{erp.name}** (saved as a custom overlay).")
    kind = st.radio("Add", ["Table", "Relationship", "Concept mapping", "Transaction / screen"], horizontal=True)
    lvls = ["standard", "uploaded", "inferred", "validate"]
    if kind == "Table":
        with st.form("f_table"):
            c = st.columns(3)
            tname = c[0].text_input("Table name")
            desc = c[1].text_input("Description")
            pk = c[2].text_input("Primary key (comma separated)")
            fields = st.text_area("Fields: one per line `FIELD | description | type`")
            conf = st.selectbox("Confidence", lvls, index=1)
            if st.form_submit_button("Save table") and tname:
                fdict = {}
                for line in fields.splitlines():
                    p = [x.strip() for x in line.split("|")]
                    if p and p[0]:
                        fdict[p[0]] = {"description": p[1] if len(p) > 1 else "", "type": p[2] if len(p) > 2 else ""}
                r.add_custom_entry(erp.id, "tables", tname, {"description": desc, "primary_key": [x.strip() for x in pk.split(",") if x.strip()],
                                                              "confidence": conf, "role": desc, "fields": fdict})
                st.success("Saved.")
    elif kind == "Relationship":
        with st.form("f_rel"):
            tbls = list(erp.all_tables())
            c = st.columns(3)
            frm = c[0].selectbox("From table", tbls)
            to = c[1].selectbox("To table", tbls)
            card = c[2].selectbox("Cardinality (from:to)", ["1:N", "N:1", "1:1", "N:N"])
            on = st.text_input("Join pairs `FROM_FIELD=TO_FIELD, ...`")
            flt = st.text_input("Optional filter, e.g. {VBPA}.PARVW = 'WE'")
            conf = st.selectbox("Confidence", lvls, index=2)
            if st.form_submit_button("Save relationship"):
                pairs = [[p.split("=")[0].strip(), p.split("=")[1].strip()] for p in on.split(",") if "=" in p]
                r.add_custom_entry(erp.id, "relationships", "", {"from": frm, "to": to, "on": pairs, "cardinality": card,
                                                                  "confidence": conf, "filter": flt})
                st.success("Saved.")
    elif kind == "Concept mapping":
        with st.form("f_con"):
            cat = r.concept_catalog()
            c = st.columns(3)
            cid = c[0].selectbox("Canonical concept", list(cat), format_func=lambda x: cat[x]["label"])
            tbl = c[1].selectbox("Table", list(erp.all_tables()))
            fld = c[2].text_input("Field")
            note = st.text_input("Note")
            conf = st.selectbox("Confidence", lvls, index=1)
            if st.form_submit_button("Save mapping") and fld:
                r.add_custom_entry(erp.id, "concepts", cid, {"table": tbl, "field": fld, "confidence": conf, "note": note})
                st.success("Saved.")
    else:
        with st.form("f_tx"):
            c = st.columns(3)
            code = c[0].text_input("Code / screen / endpoint")
            desc = c[1].text_input("Description")
            bo = c[2].selectbox("Business object", list(r.business_object_catalog()))
            tbls = st.multiselect("Tables", list(erp.all_tables()))
            notes = st.text_input("Notes")
            ttype = st.selectbox("Type", ["standard", "custom"])
            if st.form_submit_button("Save transaction") and code:
                r.add_custom_entry(erp.id, "transactions", code, {"description": desc, "type": ttype, "business_object": bo,
                                                                   "tables": tbls, "notes": notes})
                st.success("Saved.")

# ------------------------------------------------------------------ import / export
with tabs[5]:
    st.download_button(f"⬇️ Export resolved knowledge for {erp.name} (YAML)", r.export_erp_yaml(erp.id),
                       file_name=f"{erp.id}.yaml")
    yup = st.file_uploader("Import an ERP knowledge YAML (same schema as knowledge_base/*.yaml)", type=["yaml", "yml"])
    if yup and st.button("Import YAML"):
        try:
            eid = r.save_custom_yaml(yup.getvalue().decode("utf-8"))
            st.success(f"Imported {eid}.")
        except (ValueError, yaml.YAMLError) as exc:
            st.error(str(exc))
    if st.button("🔄 Reload knowledge base from disk"):
        r.reload()
        st.success("Reloaded.")
