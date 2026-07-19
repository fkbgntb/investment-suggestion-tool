"""Validated storage paths rooted under the configured data directory."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.storage.database import sqlite_database_path


@dataclass(frozen=True)
class StoragePaths:
    data_dir: Path
    database_path: Path | None
    raw_documents_dir: Path
    backups_dir: Path


def _assert_within(root: Path, candidate: Path) -> None:
    if not candidate.is_relative_to(root):
        raise ValueError(f"storage path must remain inside configured data directory: {candidate}")


def apply_private_permissions(path: Path, *, directory: bool) -> None:
    """Apply owner-only POSIX bits; Windows keeps its existing ACL inheritance."""
    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError:
        if os.name != "nt":
            raise


def prepare_storage_paths(settings: Settings) -> StoragePaths:
    data_dir = settings.data_dir.expanduser().resolve()
    raw_documents_dir = (data_dir / "raw-documents").resolve()
    backups_dir = (data_dir / "backups").resolve()
    database_path = sqlite_database_path(settings.database_url)

    for candidate in (raw_documents_dir, backups_dir):
        _assert_within(data_dir, candidate)
    if database_path is not None:
        _assert_within(data_dir, database_path)

    for directory in (data_dir, raw_documents_dir, backups_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        apply_private_permissions(directory, directory=True)

    return StoragePaths(
        data_dir=data_dir,
        database_path=database_path,
        raw_documents_dir=raw_documents_dir,
        backups_dir=backups_dir,
    )
