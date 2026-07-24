"""Register the small, reviewed official-source set in the local database."""

from __future__ import annotations

import json
from pathlib import Path

from app.collectors.registry import build_default_adapter_registry
from app.config import Settings
from app.domain.taxonomy import Source
from app.services.sources import SourceService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.paths import prepare_storage_paths

_FILES = (
    "csi-h30184-factsheet.json",
    "csi-h30184-methodology.json",
    "sse-512480-product.json",
    "sse-512480-split.json",
    "cninfo-007300-product.json",
    "micron-ir-news.json",
)


def load_sources() -> tuple[Source, ...]:
    root = Path(__file__).resolve().parents[1] / "config_data" / "sources"
    return tuple(
        Source.model_validate(json.loads((root / name).read_text(encoding="utf-8")))
        for name in _FILES
    )


def main() -> int:
    settings = Settings()
    paths = prepare_storage_paths(settings)
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    created = updated = unchanged = 0
    try:
        with database.session() as session:
            service = SourceService(
                session,
                settings.portfolio_workspace_id,
                build_default_adapter_registry(),
            )
            for source in load_sources():
                current = service.repository.get(source.source_id)
                if current is None:
                    service.create(source)
                    created += 1
                elif current == source:
                    unchanged += 1
                else:
                    service.update(source.source_id, source)
                    updated += 1
    finally:
        database.dispose()
    print(f"source data directory: {paths.data_dir}")
    print(f"official sources created: {created}")
    print(f"official sources updated: {updated}")
    print(f"official sources unchanged: {unchanged}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
