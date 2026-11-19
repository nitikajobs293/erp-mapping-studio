"""Engine / session factory. Works with PostgreSQL (production) and SQLite (tests, demo)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from erpdm.config import get_settings


class Base(DeclarativeBase):
    pass


_engines: dict[str, Engine] = {}
_factories: dict[str, sessionmaker] = {}


def get_engine(url: str | None = None) -> Engine:
    settings = get_settings()
    url = url or settings.database_url
    if url not in _engines:
        kwargs: dict = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs["pool_size"] = settings.db_pool_size
            kwargs["max_overflow"] = settings.db_pool_size * 2
        engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(engine, "connect")
            def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        _engines[url] = engine
    return _engines[url]


def get_session_factory(url: str | None = None) -> sessionmaker:
    url = url or get_settings().database_url
    if url not in _factories:
        _factories[url] = sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)
    return _factories[url]


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    session = get_session_factory(url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def is_postgres(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"
