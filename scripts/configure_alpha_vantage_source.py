"""Enable Alpha Vantage news and disable unreliable GDELT collection locally."""

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


def load_alpha_vantage_source() -> Source:
    path = (
        Path(__file__).resolve().parents[1] / "config_data" / "sources" / "alpha-vantage-news.json"
    )
    return Source.model_validate(json.loads(path.read_text(encoding="utf-8")))


def main() -> int:
    settings = Settings()
    if settings.alpha_vantage_api_key is None:
        print("Alpha Vantage source not configured: set INVEST_ALPHA_VANTAGE_API_KEY in .env")
        return 1
    paths = prepare_storage_paths(settings)
    upgrade_database(settings.database_url)
    source = load_alpha_vantage_source()
    database = Database(settings.database_url)
    disabled_gdelt = False
    try:
        with database.session() as session:
            service = SourceService(
                session,
                settings.portfolio_workspace_id,
                build_default_adapter_registry(),
            )
            existed = service.repository.get(source.source_id) is not None
            service.create(source)
            if service.repository.get("gdelt-global-news") is not None:
                disabled_gdelt = service.disable("gdelt-global-news").enabled is False
    finally:
        database.dispose()
    print(f"source data directory: {paths.data_dir}")
    print(f"source: {source.source_id}")
    print("source was already present" if existed else "source was registered")
    print("GDELT disabled" if disabled_gdelt else "GDELT was not registered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
