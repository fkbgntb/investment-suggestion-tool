"""Validate and publish the data-only semiconductor taxonomy configuration."""

from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings
from app.domain.taxonomy import TaxonomyConfiguration
from app.services.taxonomy import TaxonomyService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.paths import prepare_storage_paths


def load_demo_configuration() -> TaxonomyConfiguration:
    path = (
        Path(__file__).resolve().parents[1]
        / "config_data"
        / "taxonomy"
        / "semiconductor-1.0.0.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    return TaxonomyConfiguration.model_validate(payload)


def main() -> int:
    settings = Settings()
    paths = prepare_storage_paths(settings)
    upgrade_database(settings.database_url)
    configuration = load_demo_configuration()
    database = Database(settings.database_url)
    try:
        with database.session() as session:
            service = TaxonomyService(session, settings.portfolio_workspace_id)
            existed = service.repository.get_by_version(configuration.config_version) is not None
            service.publish(configuration)
    finally:
        database.dispose()

    print(f"taxonomy data directory: {paths.data_dir}")
    print(f"active taxonomy version: {configuration.config_version}")
    print("configuration was already present" if existed else "configuration was published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
