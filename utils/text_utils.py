"""Small text helpers: normalisation, tokenisation, similarity, SQL identifiers."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable

# Generic abbreviation expansion only. Deliberately contains NO ERP-specific
# table/column knowledge - matching must be driven by supplied metadata text.
ABBREVIATIONS = {
    "qty": "quantity", "quan": "quantity", "amt": "amount", "no": "number", "num": "number",
    "nbr": "number", "nr": "number", "id": "identifier", "desc": "description",
    "dt": "date", "dat": "date", "ts": "timestamp", "tm": "time", "mat": "material",
    "matl": "material", "org": "organization", "organisation": "organization",
    "cust": "customer", "vend": "vendor", "supp": "supplier", "loc": "location",
    "uom": "unit", "uoms": "unit", "curr": "currency", "cd": "code", "typ": "type",
    "shp": "shipment", "shpmt": "shipment", "dlv": "delivery", "deliv": "delivery",
    "itm": "item", "pos": "item", "line": "item", "hdr": "header", "whse": "warehouse",
    "wh": "warehouse", "mvt": "movement", "mvmt": "movement", "ref": "reference",
    "created": "create", "creation": "create", "changed": "change", "updated": "change",
    "modified": "change", "delivered": "delivery", "shipped": "shipment", "shipping": "shipment",
    "carrier": "carrier", "agent": "carrier", "forwarding": "carrier",
}

STOPWORDS = {"the", "of", "a", "an", "in", "on", "for", "to", "and", "or", "by", "at", "is",
             "which", "with", "from", "this", "that", "record", "field", "column", "value"}


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    s = str(value).strip().lower()
    return s in {"y", "yes", "true", "1", "x", "pk", "fk", "key", "t"}


def clean_str(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in {"nan", "none", "null"} else s


def split_list(value: Any, sep: str = ",") -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [clean_str(v) for v in value if clean_str(v)]
    return [p.strip() for p in str(value).replace(";", sep).split(sep) if p.strip() and p.strip().lower() != "nan"]


def tokenize(text: Any) -> list[str]:
    s = clean_str(text)
    if not s:
        return []
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)          # camelCase -> camel Case
    parts = re.split(r"[^A-Za-z0-9]+", s.lower())
    out = []
    for p in parts:
        if not p or p in STOPWORDS:
            continue
        p = ABBREVIATIONS.get(p, p)
        if p.endswith("s") and len(p) > 4 and not p.endswith("ss"):
            p = p[:-1]
        out.append(p)
    return out


def token_set(*texts: Any) -> set[str]:
    s: set[str] = set()
    for t in texts:
        s.update(tokenize(t))
    return s


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    a, b = set(a), set(b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def coverage(target_tokens: Iterable[str], source_tokens: Iterable[str]) -> float:
    """Share of target tokens found in source tokens (asymmetric)."""
    t, s = set(target_tokens), set(source_tokens)
    if not t:
        return 0.0
    return len(t & s) / len(t)


def string_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, clean_str(a).lower(), clean_str(b).lower()).ratio()


_SQL_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def sql_ident(name: str) -> str:
    """Return a Snowflake identifier, quoting only when required (e.g. '/BIC/FIELD')."""
    name = clean_str(name)
    if _SQL_SAFE.match(name):
        return name.upper() if name.isupper() or name.islower() else name
    return '"' + name.replace('"', '""') + '"'


def qualified(*parts: str) -> str:
    return ".".join(sql_ident(p) for p in parts if clean_str(p))


def make_alias(table: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9_]", "_", clean_str(table).lower()).strip("_") or "t"
    if base[0].isdigit():
        base = "t_" + base
    alias, i = base, 2
    while alias in used:
        alias = f"{base}_{i}"
        i += 1
    used.add(alias)
    return alias


def sql_literal(value: Any) -> str:
    return "'" + clean_str(value).replace("'", "''") + "'"
