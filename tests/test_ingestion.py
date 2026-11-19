import io

import pandas as pd

from erpdm.services.ingestion import commit_upload, detect_kind, parse_upload
from erpdm.services.metadata import get_erp, get_table, relationships_among

DDL = b"""
CREATE TABLE ZWARRANTY (
  MANDT VARCHAR(3) NOT NULL,
  CLAIM_ID VARCHAR(12) NOT NULL,
  EQUNR VARCHAR(18),
  CLAIM_DATE DATE,
  AMOUNT DECIMAL(13,2),
  PRIMARY KEY (MANDT, CLAIM_ID)
);
ALTER TABLE ZWARRANTY ADD CONSTRAINT FK_EQ FOREIGN KEY (MANDT, EQUNR) REFERENCES EQUI (MANDT, EQUNR);
"""

SQL = b"""
SELECT a.EQUNR, b.EQKTX FROM EQUI a LEFT JOIN EQKT b ON a.MANDT = b.MANDT AND a.EQUNR = b.EQUNR AND b.SPRAS = 'E';
"""


def test_detect_kinds():
    assert detect_kind("x.sql", DDL) == "ddl"
    assert detect_kind("q.sql", SQL) == "sql_queries"
    assert detect_kind("p.yaml", b"erp: {code: X, name: Y}\ntables: []") == "package"


def test_ddl_creates_tables_and_verified_fk(session, ecc):
    out = parse_upload("warranty.sql", DDL)
    t = out.package.tables[0]
    assert t.name == "ZWARRANTY" and {c.name for c in t.columns if c.is_pk} == {"MANDT", "CLAIM_ID"}
    commit_upload(session, out, "warranty.sql", DDL, "SAP_ECC", ecc.id, "tester")
    tbl = get_table(session, ecc.id, "ZWARRANTY")
    assert tbl is not None and tbl.primary_key == ["MANDT", "CLAIM_ID"]
    rel = next(r for r in relationships_among(session, ecc.id, [tbl.id]) if r.to_table.name == "EQUI")
    assert rel.source_type == "ddl_constraint" and rel.validation_status == "verified"


def test_existing_sql_joins_are_pending(session, ecc):
    out = parse_upload("q.sql", SQL)
    r = out.package.relationships[0]
    assert r.source_type == "existing_sql" and r.validation_status == "pending"
    assert {(j.from_, j.to) for j in r.join_columns} == {("MANDT", "MANDT"), ("EQUNR", "EQUNR")}


def test_sap_dd03l_inventory_and_dd05s_fk(session):
    dd03l = pd.DataFrame({"TABNAME": ["ZT1", "ZT1", "ZT1", "ZT2", "ZT2"],
                          "FIELDNAME": ["MANDT", "ID", "PARENT", "MANDT", "PID"],
                          "KEYFLAG": ["X", "X", "", "X", "X"], "DATATYPE": ["CLNT", "CHAR", "CHAR", "CLNT", "CHAR"],
                          "LENG": ["3", "10", "10", "3", "10"], "POSITION": ["1", "2", "3", "1", "2"],
                          "DDTEXT": ["Client", "Id", "Parent", "Client", "Parent id"]})
    buf = io.BytesIO()
    dd03l.to_csv(buf, index=False)
    out = parse_upload("dd03l.csv", buf.getvalue(), trusted=True)
    assert out.kind == "inventory" and len(out.package.tables) == 2
    assert out.package.tables[0].source_type == "ddl_constraint"
    erp = get_erp(session, "SAP_ECC")
    commit_upload(session, out, "dd03l.csv", buf.getvalue(), "SAP_ECC", erp.id, "tester", trusted=True)
    dd05s = pd.DataFrame({"TABNAME": ["ZT1", "ZT1"], "FIELDNAME": ["PARENT", "PARENT"],
                          "FORTABLE": ["ZT1", "ZT1"], "FORKEY": ["MANDT", "PARENT"],
                          "CHECKTABLE": ["ZT2", "ZT2"], "CHECKFIELD": ["MANDT", "PID"]})
    buf2 = io.BytesIO()
    dd05s.to_csv(buf2, index=False)
    fk = parse_upload("dd05s.csv", buf2.getvalue())
    assert fk.kind == "foreign_keys"
    rel = fk.package.relationships[0]
    assert rel.from_table == "ZT1" and rel.to_table == "ZT2" and len(rel.join_columns) == 2
    commit_upload(session, fk, "dd05s.csv", buf2.getvalue(), "SAP_ECC", erp.id, "tester")


def test_document_indexing_and_retrieval(session, ecc):
    from erpdm.services.search import search_documents
    text = b"Warranty claims are stored in table ZWARRANTY and linked to equipment via EQUNR.\n\nOther text."
    out = parse_upload("notes.txt", text)
    _, doc_id = commit_upload(session, out, "notes.txt", text, "SAP_ECC", ecc.id, "tester")
    assert doc_id
    hits = search_documents(session, "warranty claims", ecc.id)
    assert hits and "ZWARRANTY" in hits[0][0].content


def test_package_upload_creates_new_erp(session):
    pkg = b"""
erp: {code: MINI_WMS, name: Mini WMS, category: WMS}
tables:
  - name: ORDERS
    columns: [{name: ORDER_ID, is_pk: true, data_type: NUMBER}, {name: SITE_ID, data_type: NUMBER}]
  - name: SITES
    columns: [{name: SITE_ID, is_pk: true, data_type: NUMBER}, {name: SITE_NAME, data_type: VARCHAR2, business_name: Site Name}]
relationships:
  - {from_table: ORDERS, to_table: SITES, join_columns: [{from: SITE_ID, to: SITE_ID}], cardinality: "N:1", relationship_type: lookup}
"""
    out = parse_upload("mini.yaml", pkg)
    result, _ = commit_upload(session, out, "mini.yaml", pkg, None, None, "tester")
    assert result.created["tables"] == 2 and result.created["relationships"] == 1
    erp = get_erp(session, "MINI_WMS")
    from erpdm.services.relationships import plan_joins
    plan = plan_joins(session, erp, ["ORDERS", "SITES"])
    assert plan.edges and plan.edges[0].condition_sql == "orders.SITE_ID = sites.SITE_ID"
