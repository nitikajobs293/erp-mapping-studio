import pytest

from erpdm.schemas import JoinPair, RelationshipIn
from erpdm.seed.loader import PACKAGE_DIR, _raw_packages, resolve_package
from erpdm.services.metadata import (MetadataError, check_relationship, export_package, get_table,
                                     set_relationship_status, upsert_relationship)
from erpdm.services.search import search


def test_seed_systems_are_isolated(session, ecc, nsap):
    e, n = get_table(session, ecc.id, "EQUI"), get_table(session, nsap.id, "EQUI")
    assert e.id != n.id, "each ERP has its own catalogue"
    assert n.confidence <= 0.6 and n.validation_status == "requires_validation"
    assert "ASSUMPTION" in nsap.landscape_notes


def test_s4_derivation_differs_from_ecc():
    cat = _raw_packages(PACKAGE_DIR)
    s4 = resolve_package(cat["SAP_S4HANA"], cat)
    names = {t["name"] for t in s4["tables"]}
    assert "MATDOC" in names and "MSEG" not in names and "MKPF" not in names
    mara = next(t for t in s4["tables"] if t["name"] == "MARA")
    assert next(c for c in mara["columns"] if c["name"] == "MATNR")["length"] == 40


def test_unknown_erps_have_no_tables(session):
    from erpdm.services.metadata import get_erp, list_tables
    jde = get_erp(session, "JDE")
    rows, total = list_tables(session, jde.id)
    assert total == 0


def test_relationship_validation_rejects_unknown_columns(session, ecc):
    errors, _ = check_relationship(session, ecc, RelationshipIn(
        from_table="EQUI", to_table="EQKT", join_columns=[JoinPair(**{"from": "NOPE", "to": "EQUNR"})]))
    assert any("NOPE" in e for e in errors)
    with pytest.raises(MetadataError):
        upsert_relationship(session, ecc, RelationshipIn(
            from_table="EQUI", to_table="ZZZ", join_columns=[JoinPair(**{"from": "EQUNR", "to": "EQUNR"})]), "t")


def test_cardinality_warning_for_partial_key(session):
    from erpdm.services.metadata import get_erp
    ebs = get_erp(session, "ORACLE_EBS")
    _, warnings = check_relationship(session, ebs, RelationshipIn(
        from_table="PO_LINES_ALL", to_table="MTL_SYSTEM_ITEMS_B", cardinality="N:1",
        join_columns=[JoinPair(**{"from": "ITEM_ID", "to": "INVENTORY_ITEM_ID"})]))
    assert any("ORGANIZATION_ID" in w for w in warnings)


def test_approve_relationship_promotes_source(session, ecc):
    from erpdm.services.metadata import relationships_among
    rel = next(r for r in relationships_among(session, ecc.id) if r.to_table.name == "EQKT")
    set_relationship_status(session, rel.id, "verified", "tester", "checked SE11")
    assert rel.validation_status == "verified" and rel.source_type == "user_verified" and rel.confidence >= 0.95
    assert "checked SE11" in rel.evidence


def test_search_finds_tables_and_tcodes(session, ecc):
    hits = search(session, "serial number", erp_id=ecc.id)
    assert any(h.title.startswith("EQUI") for h in hits)
    hits = search(session, "IQ09", erp_id=ecc.id)
    assert hits[0].entity_type == "transaction"


def test_export_package_roundtrip(session, ecc):
    pkg = export_package(session, ecc)
    assert pkg.erp.code == "SAP_ECC"
    assert len(pkg.tables) >= 20 and len(pkg.relationships) >= 30
    assert any(t.code == "IQ09" for t in pkg.transactions)
