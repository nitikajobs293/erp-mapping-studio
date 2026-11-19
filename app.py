"""
ERP → SAP Data Mapper
Maps serial-number (SAP IQ09) and outbound-delivery (SAP LIKP/LIPS) data from many ERPs
to one SAP-style model and generates Snowflake SQL views.

Run locally:  streamlit run app.py
"""
import io
import zipfile

import pandas as pd
import streamlit as st

from erp_catalog import CANONICAL, CONFIDENCE, ERPS
from sql_builder import (build_select, build_union, build_view, coverage,
                         default_params, field_rows, supported, view_name)

st.set_page_config(page_title="ERP → SAP Data Mapper", page_icon="🔀", layout="wide")

if "overrides" not in st.session_state:
    st.session_state.overrides = {}  # {(erp, obj): {field: {"expr":..., "source":...}}}


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("🔀 ERP → SAP Mapper")
    obj_key = st.radio(
        "Business object",
        list(CANONICAL.keys()),
        format_func=lambda k: CANONICAL[k]["label"],
    )

    available = [k for k in ERPS if supported(k, obj_key)]
    missing = [ERPS[k]["label"] for k in ERPS if not supported(k, obj_key)]
    default_sel = [k for k in ("SAP", "ISCALA") if k in available]
    erps = st.multiselect(
        "Source ERPs",
        available,
        default=default_sel,
        format_func=lambda k: ERPS[k]["label"],
    )
    if missing:
        st.caption("Not available for this object: " + ", ".join(missing))

    st.divider()
    prefix = st.text_input("View name prefix", "V_")
    union_name = st.text_input("Combined view name", f"{prefix}{CANONICAL[obj_key]['short']}_ALL")

    st.divider()
    st.subheader("Source settings")
    params = {}
    for e in erps:
        with st.expander(ERPS[e]["label"]):
            p = {}
            for name, (default, help_text, choices) in ERPS[e]["params"].items():
                key = f"param_{e}_{name}"
                if choices:
                    p[name] = st.selectbox(name, choices, index=choices.index(default),
                                           help=help_text, key=key)
                else:
                    p[name] = st.text_input(name, default, help=help_text, key=key)
            params[e] = p

st.title("ERP → SAP Data Mapper")
st.caption(f"**{CANONICAL[obj_key]['label']}**: map each ERP to one SAP-style model and "
           "generate Snowflake views you can union for reporting.")

if not erps:
    st.info("Pick at least one source ERP in the sidebar.")
    st.stop()


def ov(e):
    return st.session_state.overrides.get((e, obj_key), {})


# ---------------------------------------------------------------------------
# Coverage strip
# ---------------------------------------------------------------------------
cols = st.columns(min(len(erps), 5))
for i, e in enumerate(erps):
    m, total = coverage(e, obj_key, params[e], ov(e))
    with cols[i % len(cols)]:
        st.metric(ERPS[e]["label"], f"{m}/{total} fields")
        st.progress(m / total, text=f"{round(100 * m / total)}% mapped")

tab_map, tab_sql, tab_edit, tab_src, tab_run = st.tabs(
    ["📋 Field mapping", "🧾 SQL", "✏️ Customise mapping", "🗂️ Source tables & notes", "❄️ Run in Snowflake"]
)

# ---------------------------------------------------------------------------
# Field mapping matrix
# ---------------------------------------------------------------------------
with tab_map:
    show = st.radio("Show", ["Source fields", "SQL expressions"], horizontal=True)
    base = [(f, lbl, ref, typ) for f, typ, lbl, ref in CANONICAL[obj_key]["fields"]]
    df = pd.DataFrame(base, columns=["Field", "Label", "SAP reference", "Type"])
    for e in erps:
        rows = field_rows(e, obj_key, params[e], ov(e))
        if show == "Source fields":
            df[ERPS[e]["label"]] = [(("✏️ " if r["custom"] else "") + (r["source"] or "mapped"))
                                    if r["mapped"] else "—" for r in rows]
        else:
            df[ERPS[e]["label"]] = [r["expr"] if r["mapped"] else "NULL" for r in rows]

    st.dataframe(df, width="stretch", hide_index=True, height=min(38 * (len(df) + 1), 1000))
    st.download_button("⬇️ Download mapping (CSV)", df.to_csv(index=False).encode("utf-8"),
                       file_name=f"mapping_{obj_key}.csv", mime="text/csv")
    st.caption("— = no equivalent in that ERP (the column is returned as NULL so all views can be unioned). "
               "✏️ = customised mapping.")

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------
sql_files = {}
view_names = []
for e in erps:
    name = view_name(prefix, e, obj_key)
    view_names.append(name)
    sql_files[f"{name}.sql"] = build_view(e, obj_key, params[e], name, ov(e))
sql_files[f"{union_name}.sql"] = build_union(view_names, union_name, obj_key)

with tab_sql:
    st.write("One view per ERP, all with the same columns, plus a combined `UNION ALL` view.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fname, sql in sql_files.items():
            z.writestr(fname, sql)
        z.writestr(f"mapping_{obj_key}.csv", df.to_csv(index=False))
        z.writestr("deploy_all.sql", "\n\n".join(sql_files.values()))
    c1, c2 = st.columns(2)
    c1.download_button("⬇️ Download everything (ZIP)", buf.getvalue(),
                       file_name=f"erp_sap_{obj_key}_views.zip", mime="application/zip")
    c2.download_button("⬇️ Download one deploy script", "\n\n".join(sql_files.values()),
                       file_name=f"deploy_{obj_key}.sql", mime="text/plain")

    for e, name in zip(erps, view_names):
        conf = ERPS[e]["confidence"]
        with st.expander(f"{ERPS[e]['label']}  →  {name}", expanded=len(erps) == 1):
            st.caption(CONFIDENCE[conf])
            st.code(sql_files[f"{name}.sql"], language="sql")
            st.download_button("Download", sql_files[f"{name}.sql"], file_name=f"{name}.sql",
                               key=f"dl_{e}")
    with st.expander(f"Combined view → {union_name}", expanded=True):
        st.code(sql_files[f"{union_name}.sql"], language="sql")

# ---------------------------------------------------------------------------
# Customise mapping
# ---------------------------------------------------------------------------
with tab_edit:
    st.write("Change or add a Snowflake SQL expression for any field. The table aliases are the ones "
             "used in the generated SQL (see the SQL tab). Leave the expression empty to return NULL.")
    e = st.selectbox("ERP", erps, format_func=lambda k: ERPS[k]["label"], key="edit_erp")
    rows = field_rows(e, obj_key, params[e], ov(e))
    edit_df = pd.DataFrame([{
        "Field": r["field"], "Label": r["label"], "Type": r["type"],
        "Expression": r["expr"] or "", "Source note": r["source"],
    } for r in rows])
    edited = st.data_editor(
        edit_df, hide_index=True, width="stretch", key=f"editor_{e}_{obj_key}",
        disabled=["Field", "Label", "Type"],
        column_config={"Expression": st.column_config.TextColumn(width="large")},
    )
    c1, c2 = st.columns(2)
    if c1.button("💾 Apply changes", type="primary"):
        changes = {}
        for orig, new in zip(edit_df.to_dict("records"), edited.to_dict("records")):
            if (orig["Expression"] != new["Expression"]) or (orig["Source note"] != new["Source note"]):
                changes[new["Field"]] = {"expr": new["Expression"] or "", "source": new["Source note"]}
        cur = dict(ov(e))
        cur.update(changes)
        st.session_state.overrides[(e, obj_key)] = cur
        st.success(f"Applied {len(changes)} change(s).")
        st.rerun()
    if c2.button("↩️ Reset to defaults"):
        st.session_state.overrides.pop((e, obj_key), None)
        st.rerun()
    if ov(e):
        st.caption("Customised fields: " + ", ".join(ov(e).keys()))

# ---------------------------------------------------------------------------
# Source tables & notes
# ---------------------------------------------------------------------------
with tab_src:
    for e in erps:
        erp = ERPS[e]
        obj = erp["objects"][obj_key]
        st.subheader(erp["label"])
        st.caption(f"Vendor: {erp['vendor']} · {CONFIDENCE[erp['confidence']]}")
        st.table(pd.DataFrame(obj["tables"], columns=["Table", "Purpose"]))
        for n in obj.get("notes", []):
            st.markdown(f"- {n}")
        st.divider()

# ---------------------------------------------------------------------------
# Run in Snowflake (optional)
# ---------------------------------------------------------------------------
with tab_run:
    has_secrets = False
    try:
        has_secrets = "snowflake" in st.secrets.get("connections", {})
    except Exception:
        has_secrets = False

    if not has_secrets:
        st.info(
            "To preview data or create the views directly, add a Snowflake connection in "
            "`.streamlit/secrets.toml` (or in Streamlit Community Cloud → App settings → Secrets):\n\n"
            "```toml\n[connections.snowflake]\naccount = \"xy12345.eu-west-1\"\nuser = \"...\"\n"
            "password = \"...\"\nrole = \"...\"\nwarehouse = \"...\"\ndatabase = \"...\"\nschema = \"...\"\n```"
        )
    else:
        try:
            conn = st.connection("snowflake")
        except Exception as ex:  # noqa: BLE001
            conn = None
            st.error(f"Could not connect to Snowflake: {ex}")

        if conn is not None:
            e = st.selectbox("ERP to preview", erps, format_func=lambda k: ERPS[k]["label"], key="run_erp")
            limit = st.number_input("Rows", 10, 1000, 100, step=10)
            if st.button("🔍 Preview data"):
                sql = build_select(e, obj_key, params[e], ov(e)) + f"\nLIMIT {int(limit)}"
                try:
                    st.dataframe(conn.query(sql, ttl=0), width="stretch")
                except Exception as ex:  # noqa: BLE001
                    st.error(f"Query failed: {ex}")
                    st.code(sql, language="sql")

            st.divider()
            st.write("Create (or replace) all views shown in the SQL tab in the connection's current "
                     "database and schema.")
            confirm = st.checkbox("I understand this runs CREATE OR REPLACE VIEW in Snowflake")
            if st.button("🚀 Create views", disabled=not confirm):
                cur = conn.cursor()
                for fname, sql in sql_files.items():
                    try:
                        cur.execute(sql)
                        st.success(f"Created {fname[:-4]}")
                    except Exception as ex:  # noqa: BLE001
                        st.error(f"{fname[:-4]}: {ex}")
