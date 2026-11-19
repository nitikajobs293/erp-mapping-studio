import sqlglot
import pytest

from erpdm.constants import SQL_DIALECTS
from erpdm.schemas import SelectedColumn
from erpdm.services.relationships import PlanningError, discover_candidates, plan_joins
from erpdm.services.sqlgen import SQLGenError, generate_sql


def cols(*specs):
    return [SelectedColumn(table=s.split(".")[0], column=s.split(".")[1]) for s in specs]


def test_iq09_plan_uses_bridge_and_language(session, ecc):
    plan = plan_joins(session, ecc, ["EQUI", "MAKT", "EQKT", "T370T"], root="EQUI")
    assert plan.tables[0] == "EQUI" and "MARA" in plan.tables  # bridge to MAKT
    eqkt = next(e for e in plan.edges if e.right_table == "EQKT")
    assert "SPRAS = 'E'" in eqkt.condition_sql and not eqkt.fans_out
    assert any("Bridge table MARA" in w for w in plan.warnings)


def test_planner_does_not_bridge_transactions_through_master(session, ecc):
    plan = plan_joins(session, ecc, ["VBAK", "VBAP", "LIKP"], root="VBAK")
    assert "KNA1" not in plan.tables and "LIPS" in plan.tables


def test_fan_out_detected_for_time_dependent(session, ecc):
    plan = plan_joins(session, ecc, ["EQUI", "EQUZ"], root="EQUI")
    e = plan.edges[0]
    assert e.fans_out and e.uncovered_keys == ["EQLFN"]


def test_unknown_table_raises(session, ecc):
    with pytest.raises(PlanningError):
        plan_joins(session, ecc, ["EQUI", "ZNOT_THERE"])


def test_rejected_relationships_are_not_used(session, ecc):
    from erpdm.services.metadata import relationships_among, set_relationship_status
    rel = next(r for r in relationships_among(session, ecc.id) if r.to_table.name == "EQKT")
    set_relationship_status(session, rel.id, "rejected", "t")
    plan = plan_joins(session, ecc, ["EQUI", "EQKT"], root="EQUI")
    assert plan.unreachable == ["EQKT"]


@pytest.mark.parametrize("dialect", list(SQL_DIALECTS))
def test_sql_generation_parses_in_every_dialect(session, ecc, dialect):
    selected = cols("EQUI.EQUNR", "EQUI.SERNR", "MAKT.MAKTX", "EQUZ.IWERK", "T001W.NAME1")
    plan = plan_joins(session, ecc, [c.table for c in selected], root="EQUI")
    gen = generate_sql(session, ecc, plan, selected, dialect, raw_database="RAW", raw_schema="SAP_ECC")
    sqlglot.parse(gen.sql, read=SQL_DIALECTS[dialect])
    assert "RAW.SAP_ECC.EQUI" in gen.sql or "RAW.SAP_ECC" in gen.sql.replace('"', "")
    assert gen.join_explanations and gen.validation_queries
    if dialect in ("Snowflake", "Databricks SQL"):
        assert "QUALIFY" in gen.sql.upper()  # EQUZ fan-out de-duplicated
    else:
        assert "_rn = 1" in gen.sql.replace('"', "").lower() or "_RN = 1" in gen.sql.upper()


def test_sql_rejects_injection_in_filters(session, ecc):
    selected = cols("EQUI.EQUNR")
    plan = plan_joins(session, ecc, ["EQUI"], root="EQUI")
    with pytest.raises(SQLGenError):
        generate_sql(session, ecc, plan, selected, "Snowflake", filters=["1=1; DROP TABLE x"])


def test_sql_rejects_unknown_column(session, ecc):
    plan = plan_joins(session, ecc, ["EQUI"], root="EQUI")
    with pytest.raises(SQLGenError):
        generate_sql(session, ecc, plan, cols("EQUI.NOT_A_COLUMN"), "Snowflake")


def test_candidate_discovery_is_unverified(session, ecc):
    cands = discover_candidates(session, ecc)
    assert all(c.confidence <= 0.3 for c in cands)
