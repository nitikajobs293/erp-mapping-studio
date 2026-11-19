"""Builds Snowflake SQL from the ERP catalogue."""
import re

from erp_catalog import CANONICAL, ERPS

TYPE_SQL = {"VARCHAR": "VARCHAR", "NUMBER": "NUMBER(38,6)", "DATE": "DATE"}
TOKEN = re.compile(r"\{\{(\w+)\}\}")


def default_params(erp_key):
    return {k: v[0] for k, v in ERPS[erp_key]["params"].items()}


def supported(erp_key, obj_key):
    return obj_key in ERPS[erp_key]["objects"]


def _val(x, params):
    return x(params) if callable(x) else (x or "")


def resolve(text, params):
    """Replace {{token}} with parameter values; fail loudly on unknown tokens."""
    def sub(m):
        name = m.group(1)
        if name not in params:
            raise KeyError(f"Missing parameter '{name}'")
        return str(params[name])
    return TOKEN.sub(sub, text)


def field_rows(erp_key, obj_key, params, overrides=None):
    """Canonical field list with the expression and source used for one ERP."""
    overrides = overrides or {}
    obj = ERPS[erp_key]["objects"][obj_key]
    dyn = obj.get("dynamic_fields", {})
    rows = []
    for name, ftype, label, sap_ref in CANONICAL[obj_key]["fields"]:
        expr, source = obj["fields"].get(name, (None, ""))
        if name in dyn:
            res = dyn[name](params)
            if isinstance(res, tuple):
                expr, source = res
            else:
                expr = res
            if expr is None:
                source = ""
        custom = False
        if name in overrides and overrides[name].get("expr") is not None:
            o = overrides[name]
            expr = o["expr"].strip() or None
            source = o.get("source") or ("custom" if expr else "")
            custom = True
        rows.append({
            "field": name, "type": ftype, "label": label, "sap_ref": sap_ref,
            "expr": expr, "source": source if expr else "", "mapped": bool(expr), "custom": custom,
        })
    return rows


def coverage(erp_key, obj_key, params, overrides=None):
    rows = field_rows(erp_key, obj_key, params, overrides)
    return sum(r["mapped"] for r in rows), len(rows)


def build_select(erp_key, obj_key, params, overrides=None):
    obj = ERPS[erp_key]["objects"][obj_key]
    rows = field_rows(erp_key, obj_key, params, overrides)

    cols = [f"    '{erp_key}'::VARCHAR AS SOURCE_SYSTEM"]
    for r in rows:
        typ = TYPE_SQL[r["type"]]
        if r["mapped"]:
            col = f"    CAST({r['expr']} AS {typ}) AS {r['field']}"
            if r["source"]:
                col += f"  -- {r['source']}"
        else:
            col = f"    CAST(NULL AS {typ}) AS {r['field']}"
        cols.append(col)
    # commas go before the trailing comment
    out = []
    for i, c in enumerate(cols):
        last = i == len(cols) - 1
        if "  -- " in c:
            body, comment = c.split("  -- ", 1)
            out.append(body + ("" if last else ",") + "  -- " + comment.replace("\n", " "))
        else:
            out.append(c + ("" if last else ","))

    ctes = _val(obj.get("ctes"), params).strip()
    frm = _val(obj.get("from"), params).strip()
    where = _val(obj.get("where"), params).strip()

    sql = ""
    if ctes:
        sql += "WITH\n" + ctes + "\n"
    sql += "SELECT\n" + "\n".join(out) + "\nFROM " + frm
    if where:
        sql += "\nWHERE " + where
    return resolve(sql, params)


def view_name(prefix, erp_key, obj_key):
    return f"{prefix}{CANONICAL[obj_key]['short']}_{erp_key}"


def build_view(erp_key, obj_key, params, name, overrides=None):
    erp = ERPS[erp_key]
    header = (f"/* {CANONICAL[obj_key]['label']}\n"
              f"   Source : {erp['label']}\n"
              f"   Target : Snowflake\n"
              f"   Params : " + ", ".join(f"{k}={v!r}" for k, v in params.items()) + " */\n")
    return header + f"CREATE OR REPLACE VIEW {name} AS\n" + build_select(erp_key, obj_key, params, overrides) + ";\n"


def build_union(view_names, name, obj_key):
    if not view_names:
        return ""
    body = "\nUNION ALL\n".join(f"SELECT * FROM {v}" for v in view_names)
    return (f"/* Combined {CANONICAL[obj_key]['label']} across "
            f"{len(view_names)} source(s) */\nCREATE OR REPLACE VIEW {name} AS\n{body};\n")
