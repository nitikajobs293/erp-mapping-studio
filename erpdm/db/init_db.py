"""Database initialisation: migrations (or create_all for tests), FTS indexes, bootstrap admin, seed."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from erpdm.config import get_settings
from erpdm.db import models  # noqa: F401  (register models)
from erpdm.db.base import Base, get_engine, session_scope
from erpdm.logging_setup import get_logger

log = get_logger(__name__)
ROOT = Path(__file__).resolve().parents[2]


def run_migrations(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    insp = inspect(get_engine(url))
    if insp.has_table("erp_systems") and not insp.has_table("alembic_version"):
        # Schema was created outside Alembic (e.g. create_all in tests/demo): adopt it at head.
        log.warning("Existing unversioned schema found; stamping Alembic head")
        command.stamp(cfg, "head")
        return
    command.upgrade(cfg, "head")


def init_database(url: str | None = None, use_migrations: bool = True, seed: bool | None = None) -> list[str]:
    """Idempotent. Returns a list of human-readable notices (e.g. missing admin config)."""
    settings = get_settings()
    url = url or settings.database_url
    notices: list[str] = []
    if use_migrations and (ROOT / "migrations").exists():
        run_migrations(url)
    else:
        Base.metadata.create_all(get_engine(url))
    from erpdm.seed.loader import load_seed
    from erpdm.services.auth import ensure_bootstrap_admin
    from erpdm.services.search import ensure_pg_indexes

    with session_scope(url) as s:
        ensure_pg_indexes(s)
        notice = ensure_bootstrap_admin(s)
        if notice:
            notices.append(notice)
        if seed if seed is not None else settings.seed_sample_metadata:
            results = load_seed(s)
            for code, r in results.items():
                log.info("seeded %s created=%s skipped=%d", code, r.created, len(r.skipped))
                for msg in r.skipped:
                    log.warning("seed %s: %s", code, msg)
    return notices


def schema_is_current(url: str | None = None) -> bool:
    insp = inspect(get_engine(url))
    return all(insp.has_table(t) for t in Base.metadata.tables)
