from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.domain.enums import AssetType
from app.domain.portfolio import Asset
from app.services.portfolio import PortfolioService
from app.services.taxonomy import TaxonomyConflict, TaxonomyService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.models import (
    AuditEventRow,
    ImmutableTaxonomyConfigurationError,
    TaxonomyConfigurationRow,
)
from app.storage.repositories import TaxonomyRepository
from tests.taxonomy_factories import taxonomy_configuration


def database(tmp_path: Path) -> Database:
    url = f"sqlite:///{(tmp_path / 'taxonomy.sqlite3').as_posix()}"
    upgrade_database(url)
    return Database(url)


def add_demo_asset(session: object, workspace_id: str = "personal") -> None:
    PortfolioService(session, workspace_id).create_asset(
        Asset(
            asset_id="asset-007300",
            fund_code="007300",
            name="Semiconductor ETF Link A",
            asset_type=AssetType.ETF_LINK,
            market="CN",
            tracking_asset_code="512480",
        )
    )


def test_publish_version_activate_rollback_and_audit(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            add_demo_asset(session)
            service = TaxonomyService(session, "personal")
            first = taxonomy_configuration()
            assert service.publish(first) == first
            assert service.publish(first) == first

            second = taxonomy_configuration(
                "test-1.1.0",
                based_on_version="test-1.0.0",
                topic_enabled=False,
            )
            service.publish(second)
            assert service.get_active().config_version == "test-1.1.0"
            assert service.get_active().topics[0].enabled is False
            assert len(service.list_configurations()) == 2

            service.activate("test-1.0.0")
            assert service.get_active().config_version == "test-1.0.0"
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEventRow)
                    .where(AuditEventRow.target_type == "taxonomy_configuration")
                )
                == 3
            )
    finally:
        db.dispose()


def test_publication_rejects_stale_base_unknown_asset_and_conflicting_version(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            add_demo_asset(session)
            service = TaxonomyService(session, "personal")
            service.publish(taxonomy_configuration())

            with pytest.raises(TaxonomyConflict, match="currently active"):
                service.publish(taxonomy_configuration("test-1.1.0"))
            with pytest.raises(TaxonomyConflict, match="unknown asset"):
                service.publish(
                    taxonomy_configuration(
                        "test-1.1.0",
                        based_on_version="test-1.0.0",
                        asset_id="missing-asset",
                    )
                )

            conflicting = taxonomy_configuration().model_copy(update={"name": "Different payload"})
            with pytest.raises(TaxonomyConflict, match="different data"):
                service.publish(conflicting)
    finally:
        db.dispose()


def test_published_configuration_is_immutable_and_workspace_scoped(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            add_demo_asset(session, "workspace-a")
            TaxonomyService(session, "workspace-a").publish(taxonomy_configuration())
            assert TaxonomyRepository(session, "workspace-b").get_active() is None

        with pytest.raises(ImmutableTaxonomyConfigurationError), db.session() as session:
            row = session.scalar(select(TaxonomyConfigurationRow))
            assert row is not None
            row.payload = {"tampered": True}

        with pytest.raises(ImmutableTaxonomyConfigurationError), db.session() as session:
            row = session.scalar(select(TaxonomyConfigurationRow))
            assert row is not None
            session.delete(row)
    finally:
        db.dispose()
