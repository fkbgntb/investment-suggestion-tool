"""Authenticated encrypted SQLite backup and integrity-checked restore."""

from __future__ import annotations

import os
import sqlite3
import zlib
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_bytes, token_hex
from tempfile import NamedTemporaryFile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from app.storage.paths import apply_private_permissions

MAGIC = b"ISTBACKUP1\x00"
SALT_SIZE = 16
NONCE_SIZE = 12
MAX_BACKUP_BYTES = 512 * 1024 * 1024
MAX_DATABASE_BYTES = 1024 * 1024 * 1024


class BackupError(RuntimeError):
    pass


class BackupDecryptionError(BackupError):
    pass


def _validate_passphrase(passphrase: str) -> bytes:
    if len(passphrase) < 16:
        raise ValueError("backup passphrase must contain at least 16 characters")
    return passphrase.encode("utf-8")


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(_validate_passphrase(passphrase))


def _safe_decompress(payload: bytes) -> bytes:
    decompressor = zlib.decompressobj()
    result = decompressor.decompress(payload, MAX_DATABASE_BYTES + 1)
    if len(result) > MAX_DATABASE_BYTES or decompressor.unconsumed_tail:
        raise BackupError("restored database exceeds the configured size limit")
    result += decompressor.flush()
    if len(result) > MAX_DATABASE_BYTES or not decompressor.eof:
        raise BackupError("invalid or oversized compressed backup payload")
    return result


def _verify_sqlite_integrity(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if result is None or result[0] != "ok":
        raise BackupError("restored SQLite database failed integrity check")


def create_encrypted_backup(
    *,
    database_path: Path,
    backups_dir: Path,
    passphrase: str,
    now: datetime | None = None,
) -> Path:
    """Use SQLite online backup, compress it, then encrypt with Scrypt + AES-GCM."""
    source_path = database_path.resolve()
    destination_root = backups_dir.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {source_path}")
    destination_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    final_path = destination_root / f"investment-tool-{timestamp}-{token_hex(4)}.istbackup"
    temporary_database: Path | None = None
    temporary_backup: Path | None = None

    try:
        with NamedTemporaryFile(
            prefix="backup-source-", suffix=".sqlite3", dir=destination_root, delete=False
        ) as handle:
            temporary_database = Path(handle.name)

        source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
        destination = sqlite3.connect(temporary_database)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

        _verify_sqlite_integrity(temporary_database)
        if temporary_database.stat().st_size > MAX_DATABASE_BYTES:
            raise BackupError("SQLite database exceeds the configured backup size limit")
        compressed = zlib.compress(temporary_database.read_bytes(), level=9)
        salt = token_bytes(SALT_SIZE)
        nonce = token_bytes(NONCE_SIZE)
        aad = MAGIC + salt
        encrypted = AESGCM(_derive_key(passphrase, salt)).encrypt(nonce, compressed, aad)
        if len(MAGIC) + len(salt) + len(nonce) + len(encrypted) > MAX_BACKUP_BYTES:
            raise BackupError("encrypted backup exceeds the configured size limit")

        with NamedTemporaryFile(
            prefix="backup-write-", suffix=".tmp", dir=destination_root, delete=False
        ) as handle:
            temporary_backup = Path(handle.name)
            handle.write(MAGIC + salt + nonce + encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_backup, final_path)
        temporary_backup = None
        apply_private_permissions(final_path, directory=False)
        return final_path
    finally:
        for temporary_path in (temporary_database, temporary_backup):
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def restore_encrypted_backup(
    *,
    backup_path: Path,
    destination_path: Path,
    passphrase: str,
    allow_overwrite: bool = False,
) -> Path:
    source_path = backup_path.resolve()
    final_path = destination_path.resolve()
    if final_path.exists() and not allow_overwrite:
        raise FileExistsError("restore destination already exists; explicit overwrite is required")
    if source_path.stat().st_size > MAX_BACKUP_BYTES:
        raise BackupError("encrypted backup exceeds the configured size limit")

    payload = source_path.read_bytes()
    header_size = len(MAGIC) + SALT_SIZE + NONCE_SIZE
    if len(payload) <= header_size or not payload.startswith(MAGIC):
        raise BackupError("not a supported investment tool backup")
    salt_start = len(MAGIC)
    nonce_start = salt_start + SALT_SIZE
    encrypted_start = nonce_start + NONCE_SIZE
    salt = payload[salt_start:nonce_start]
    nonce = payload[nonce_start:encrypted_start]
    encrypted = payload[encrypted_start:]

    try:
        compressed = AESGCM(_derive_key(passphrase, salt)).decrypt(nonce, encrypted, MAGIC + salt)
    except InvalidTag as error:
        raise BackupDecryptionError("backup authentication failed") from error
    database_bytes = _safe_decompress(compressed)

    final_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            prefix="restore-", suffix=".sqlite3", dir=final_path.parent, delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(database_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        _verify_sqlite_integrity(temporary_path)
        os.replace(temporary_path, final_path)
        temporary_path = None
        apply_private_permissions(final_path, directory=False)
        return final_path
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
