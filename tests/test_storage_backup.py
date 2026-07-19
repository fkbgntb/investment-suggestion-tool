from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.storage.backup import (
    BackupDecryptionError,
    create_encrypted_backup,
    restore_encrypted_backup,
)

PASSPHRASE = "test-only-passphrase-32-characters"  # noqa: S105


def create_sample_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES ('preserved')")
        connection.commit()
    finally:
        connection.close()


def test_encrypted_backup_round_trip_and_overwrite_guard(tmp_path: Path) -> None:
    database_path = tmp_path / "source.sqlite3"
    backups_dir = tmp_path / "backups"
    restored_path = tmp_path / "restored.sqlite3"
    create_sample_database(database_path)

    backup_path = create_encrypted_backup(
        database_path=database_path,
        backups_dir=backups_dir,
        passphrase=PASSPHRASE,
    )
    assert b"preserved" not in backup_path.read_bytes()

    restore_encrypted_backup(
        backup_path=backup_path,
        destination_path=restored_path,
        passphrase=PASSPHRASE,
    )
    connection = sqlite3.connect(restored_path)
    try:
        assert connection.execute("SELECT value FROM sample").fetchone() == ("preserved",)
    finally:
        connection.close()

    with pytest.raises(FileExistsError, match="overwrite"):
        restore_encrypted_backup(
            backup_path=backup_path,
            destination_path=restored_path,
            passphrase=PASSPHRASE,
        )


def test_wrong_passphrase_and_tampering_are_rejected(tmp_path: Path) -> None:
    database_path = tmp_path / "source.sqlite3"
    backups_dir = tmp_path / "backups"
    create_sample_database(database_path)
    backup_path = create_encrypted_backup(
        database_path=database_path,
        backups_dir=backups_dir,
        passphrase=PASSPHRASE,
    )

    with pytest.raises(BackupDecryptionError, match="authentication"):
        restore_encrypted_backup(
            backup_path=backup_path,
            destination_path=tmp_path / "wrong.sqlite3",
            passphrase="a-different-test-passphrase",  # noqa: S106
        )

    tampered = bytearray(backup_path.read_bytes())
    tampered[-1] ^= 1
    tampered_path = backups_dir / "tampered.istbackup"
    tampered_path.write_bytes(tampered)
    with pytest.raises(BackupDecryptionError, match="authentication"):
        restore_encrypted_backup(
            backup_path=tampered_path,
            destination_path=tmp_path / "tampered.sqlite3",
            passphrase=PASSPHRASE,
        )
