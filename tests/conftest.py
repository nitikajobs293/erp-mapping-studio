"""Test fixtures: a fresh, seeded database per session (SQLite by default; PostgreSQL if TEST_DATABASE_URL is set)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def db_url(tmp_path_factory) -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        from sqlalchemy import create_engine, text
        eng = create_engine(url)
        with eng.begin() as c:
            c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        eng.dispose()
        use_migrations = True
    else:
        url = f"sqlite:///{tmp_path_factory.mktemp('db') / 'test.db'}"
        use_migrations = False
    os.environ["DATABASE_URL"] = url
    from erpdm.config import get_settings
    get_settings.cache_clear()
    from erpdm.db.init_db import init_database
    init_database(url, use_migrations=use_migrations, seed=True)
    return url


@pytest.fixture()
def session(db_url):
    """Each test runs in a transaction that is rolled back afterwards."""
    from erpdm.db.base import get_session_factory
    s = get_session_factory(db_url)()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture()
def ecc(session):
    from erpdm.services.metadata import get_erp
    return get_erp(session, "SAP_ECC")


@pytest.fixture()
def nsap(session):
    from erpdm.services.metadata import get_erp
    return get_erp(session, "SAP_NSAP")
