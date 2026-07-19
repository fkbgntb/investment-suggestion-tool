"""Database engine and transaction/session lifecycle."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def sqlite_database_path(database_url: str) -> Path | None:
    """Return the SQLite file path, or None for memory/non-SQLite databases."""
    url = make_url(database_url)
    if url.drivername != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database).expanduser().resolve()


def _configure_sqlite(dbapi_connection: sqlite3.Connection, _: object) -> None:
    previous_autocommit: bool | None = None
    if hasattr(dbapi_connection, "autocommit"):
        previous_autocommit = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=FULL")
    finally:
        cursor.close()
        if previous_autocommit is not None:
            dbapi_connection.autocommit = previous_autocommit


def create_database_engine(database_url: str) -> Engine:
    """Create a portable SQLAlchemy engine with secure SQLite defaults."""
    url = make_url(database_url)
    options: dict[str, object] = {"pool_pre_ping": True}

    if url.drivername == "sqlite":
        database_path = sqlite_database_path(database_url)
        if database_path is not None:
            database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        connect_args: dict[str, object] = {"check_same_thread": False}
        if sys.version_info >= (3, 12):
            connect_args["autocommit"] = False
        options["connect_args"] = connect_args
        if url.database == ":memory:":
            options["poolclass"] = StaticPool

    engine = create_engine(database_url, **options)
    if url.drivername == "sqlite":
        event.listen(engine, "connect", _configure_sqlite)
    return engine


class Database:
    """Own the engine and expose short-lived transactional sessions."""

    def __init__(self, database_url: str) -> None:
        self.engine = create_database_engine(database_url)
        self._session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            with session.begin():
                yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
