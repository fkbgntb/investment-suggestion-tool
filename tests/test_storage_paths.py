from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.config import Settings
from app.storage.database import create_database_engine
from app.storage.paths import prepare_storage_paths
from scripts.storage_admin import _safe_backup_path


def test_storage_paths_are_created_inside_configured_data_root(tmp_path: Path) -> None:
    data_dir = tmp_path / "external-data"
    settings = Settings(
        _env_file=None,
        data_dir=data_dir,
        database_url=f"sqlite:///{(data_dir / 'tool.sqlite3').as_posix()}",
    )

    paths = prepare_storage_paths(settings)

    assert paths.data_dir == data_dir.resolve()
    assert paths.raw_documents_dir.is_dir()
    assert paths.backups_dir.is_dir()
    assert paths.database_path == (data_dir / "tool.sqlite3").resolve()


def test_sqlite_database_cannot_escape_data_root(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="inside INVEST_DATA_DIR"):
        Settings(
            _env_file=None,
            data_dir=tmp_path / "allowed",
            database_url=f"sqlite:///{(tmp_path / 'outside.sqlite3').as_posix()}",
        )

    with pytest.raises(ValidationError, match="inside INVEST_DATA_DIR"):
        Settings(
            _env_file=None,
            data_dir=tmp_path / "allowed",
            database_url=f"sqlite+pysqlite:///{(tmp_path / 'outside.sqlite3').as_posix()}",
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-letter policy")
def test_external_data_policy_rejects_system_drive(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="system drive"):
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{(tmp_path / 'tool.sqlite3').as_posix()}",
            require_external_data_dir=True,
        )


def test_sqlite_engine_enables_foreign_keys(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'engine.sqlite3').as_posix()}")
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
            assert connection.scalar(text("PRAGMA synchronous")) == 2
    finally:
        engine.dispose()


def test_restore_backup_filename_cannot_escape_backup_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    settings = Settings(
        _env_file=None,
        data_dir=data_dir,
        database_url=f"sqlite:///{(data_dir / 'tool.sqlite3').as_posix()}",
    )
    paths = prepare_storage_paths(settings)

    with pytest.raises(ValueError, match="filename"):
        _safe_backup_path(paths, "../outside.istbackup")
