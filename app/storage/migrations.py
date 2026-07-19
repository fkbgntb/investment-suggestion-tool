"""Programmatic Alembic entrypoints used by scripts and tests."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from app.storage.database import sqlite_database_path


def build_alembic_config(database_url: str) -> Config:
    project_root = Path(__file__).resolve().parents[2]
    config = Config(project_root / "alembic.ini")
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade_database(database_url: str, revision: str = "head") -> None:
    database_path = sqlite_database_path(database_url)
    if database_path is not None:
        database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    command.upgrade(build_alembic_config(database_url), revision)


def downgrade_database(database_url: str, revision: str) -> None:
    command.downgrade(build_alembic_config(database_url), revision)
