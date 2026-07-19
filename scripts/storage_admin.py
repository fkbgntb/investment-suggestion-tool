"""Safe local administration commands for database storage and backups."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select

from app.config import Settings
from app.storage.backup import create_encrypted_backup, restore_encrypted_backup
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.models import WorkspaceRow, utc_now
from app.storage.paths import StoragePaths, prepare_storage_paths
from app.storage.repositories import AuditRepository
from app.storage.retention import purge_expired_raw_bodies


def _settings_and_paths() -> tuple[Settings, StoragePaths]:
    settings = Settings()
    return settings, prepare_storage_paths(settings)


def _require_sqlite_path(paths: StoragePaths) -> Path:
    if paths.database_path is None:
        raise RuntimeError("this command currently supports file-based SQLite databases only")
    return paths.database_path


def _require_backup_passphrase(settings: Settings) -> str:
    if settings.backup_passphrase is None:
        raise RuntimeError("set INVEST_BACKUP_PASSPHRASE locally before using backups")
    return settings.backup_passphrase.get_secret_value()


def _safe_backup_path(paths: StoragePaths, filename: str) -> Path:
    if Path(filename).name != filename:
        raise ValueError("backup must be a filename from the configured backups directory")
    candidate = (paths.backups_dir / filename).resolve()
    if not candidate.is_relative_to(paths.backups_dir):
        raise ValueError("backup path escapes the configured backups directory")
    return candidate


def _migrate() -> None:
    settings, paths = _settings_and_paths()
    upgrade_database(settings.database_url)
    location = paths.database_path or Path("managed database")
    print(f"database is at the latest schema revision: {location}")


def _backup() -> None:
    settings, paths = _settings_and_paths()
    backup_path = create_encrypted_backup(
        database_path=_require_sqlite_path(paths),
        backups_dir=paths.backups_dir,
        passphrase=_require_backup_passphrase(settings),
    )
    print(f"encrypted backup created: {backup_path}")


def _restore(filename: str, *, overwrite: bool) -> None:
    settings, paths = _settings_and_paths()
    restored_path = restore_encrypted_backup(
        backup_path=_safe_backup_path(paths, filename),
        destination_path=_require_sqlite_path(paths),
        passphrase=_require_backup_passphrase(settings),
        allow_overwrite=overwrite,
    )
    print(f"database restored and integrity checked: {restored_path}")


def _purge() -> None:
    settings, _ = _settings_and_paths()
    database = Database(settings.database_url)
    total = 0
    try:
        with database.session() as session:
            workspaces = session.scalars(select(WorkspaceRow)).all()
            for workspace in workspaces:
                count = purge_expired_raw_bodies(
                    session,
                    workspace_id=workspace.workspace_id,
                    now=utc_now(),
                    retention_days=workspace.raw_document_retention_days,
                )
                if count:
                    AuditRepository(session, workspace.workspace_id).record(
                        event_type="raw_document_retention",
                        actor="storage_admin",
                        target_type="workspace",
                        target_id=workspace.workspace_id,
                        outcome="completed",
                        details={"purged_document_count": count},
                    )
                total += count
    finally:
        database.dispose()
    print(f"raw document bodies purged: {total}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("migrate", help="upgrade the configured database to the latest schema")
    subcommands.add_parser("backup", help="create an authenticated encrypted SQLite backup")
    restore = subcommands.add_parser("restore", help="restore a backup from the backups directory")
    restore.add_argument("filename", help="backup filename, without a directory")
    restore.add_argument(
        "--overwrite",
        action="store_true",
        help="explicitly replace the configured database after validation",
    )
    subcommands.add_parser("purge", help="purge expired raw bodies while keeping metadata")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        _migrate()
    elif args.command == "backup":
        _backup()
    elif args.command == "restore":
        _restore(args.filename, overwrite=args.overwrite)
    elif args.command == "purge":
        _purge()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
