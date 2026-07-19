from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.enums import AssetType, FeeDataStatus, PositionRiskBand
from app.domain.portfolio import (
    Asset,
    FundFeePolicy,
    Position,
    RedemptionFeeTier,
)
from app.services.portfolio import (
    PortfolioConflict,
    PortfolioNotFound,
    PortfolioService,
    assert_private_ai_summary,
    identify_asset_type,
    is_loopback_client,
)
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.models import ImmutableSnapshotError, PositionSnapshotRow
from tests.domain_factories import NOW, OPENED_ON, investment_profile, money


def database(tmp_path: Path) -> Database:
    url = f"sqlite:///{(tmp_path / 'portfolio.sqlite3').as_posix()}"
    upgrade_database(url)
    return Database(url)


def asset() -> Asset:
    return Asset(
        asset_id="asset-007300",
        fund_code="007300",
        name="Semiconductor ETF Link A",
        asset_type=AssetType.ETF_LINK,
        market="CN",
        tracking_asset_code="512480",
    )


def position(*, value: str = "653.59", snapshot_at: datetime = NOW) -> Position:
    return Position(
        position_id="position-007300",
        profile_id="profile-demo",
        asset_id="asset-007300",
        units=Decimal("157.89"),
        cost_basis=money("640"),
        current_value=money(value),
        average_cost_per_unit=Decimal("4.0535"),
        opened_on=OPENED_ON,
        latest_purchase_on=OPENED_ON.replace(month=7),
        recurring_contribution=money("50"),
        snapshot_at=snapshot_at,
    )


def test_product_classification_uses_explicit_facts() -> None:
    assert (
        identify_asset_type(exchange_traded=False, feeder_fund=True, index_tracking=True)
        is AssetType.ETF_LINK
    )
    assert (
        identify_asset_type(exchange_traded=True, feeder_fund=False, index_tracking=True)
        is AssetType.ETF
    )
    assert (
        identify_asset_type(exchange_traded=False, feeder_fund=False, index_tracking=True)
        is AssetType.INDEX_FUND
    )
    assert (
        identify_asset_type(exchange_traded=False, feeder_fund=False, index_tracking=False)
        is AssetType.UNKNOWN
    )
    with pytest.raises(ValueError, match="cannot also"):
        identify_asset_type(exchange_traded=True, feeder_fund=True, index_tracking=True)


def test_fee_policy_rejects_unverified_values_and_overlapping_tiers() -> None:
    with pytest.raises(ValueError, match="unverified"):
        FundFeePolicy(status=FeeDataStatus.UNKNOWN, purchase_fee_rate=Decimal("0.001"))

    with pytest.raises(ValueError, match="overlap"):
        FundFeePolicy(
            status=FeeDataStatus.USER_PROVIDED,
            source_name="manual test",
            effective_on=OPENED_ON,
            redemption_fee_tiers=(
                RedemptionFeeTier(
                    minimum_holding_days=0,
                    maximum_holding_days=7,
                    fee_rate=Decimal("0.015"),
                ),
                RedemptionFeeTier(
                    minimum_holding_days=7,
                    maximum_holding_days=30,
                    fee_rate=Decimal("0.005"),
                ),
            ),
        )


def test_crud_snapshot_immutability_and_private_ai_summary(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            service = PortfolioService(session, "personal-demo")
            service.create_profile(investment_profile())
            service.create_asset(asset())
            service.create_position(position())
            updated = service.update_position(position(value="600"))
            assert updated.current_value.amount == Decimal("600")
            assert service.list_positions() == (updated,)

            snapshot = service.create_analysis_snapshot(
                "position-007300",
                generated_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
            )
            summary = service.build_ai_risk_summary(snapshot.snapshot_id)
            payload = assert_private_ai_summary(summary)

            assert summary.risk_band is PositionRiskBand.WITHIN_PLAN
            assert summary.fee_data_status is FeeDataStatus.UNKNOWN
            serialized = str(payload)
            for forbidden in ("653.59", "600", "640", "157.89", "profile-demo", "个人训练账户"):
                assert forbidden not in serialized

        with (
            pytest.raises(ImmutableSnapshotError, match="cannot be updated"),
            db.session() as session,
        ):
            row = session.get(PositionSnapshotRow, snapshot.snapshot_id)
            assert row is not None
            row.purpose = "MUTATED"
            session.flush()

        with (
            pytest.raises(ImmutableSnapshotError, match="cannot be deleted"),
            db.session() as session,
        ):
            row = session.get(PositionSnapshotRow, snapshot.snapshot_id)
            assert row is not None
            session.delete(row)
            session.flush()

        with db.session() as session:
            service = PortfolioService(session, "personal-demo")
            service.delete_position("position-007300")
            with pytest.raises(PortfolioNotFound):
                service.get_position("position-007300")
            assert service.repository.get_position_snapshot(snapshot.snapshot_id) == snapshot
    finally:
        db.dispose()


def test_position_rejects_unknown_references_and_future_snapshots(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            service = PortfolioService(session, "personal-demo")
            with pytest.raises(PortfolioConflict, match="unknown investment profile"):
                service.create_position(position())
            service.create_profile(investment_profile())
            service.create_asset(asset())
            with pytest.raises(PortfolioConflict, match="future"):
                service.create_position(position(snapshot_at=datetime(2099, 1, 1, tzinfo=UTC)))
    finally:
        db.dispose()


def test_multiple_profiles_can_hold_same_asset_at_same_snapshot_time(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            service = PortfolioService(session, "personal-demo")
            first_profile = investment_profile()
            second_profile = first_profile.model_copy(
                update={"profile_id": "profile-second", "name": "Second training profile"}
            )
            service.create_profile(first_profile)
            service.create_profile(second_profile)
            service.create_asset(asset())
            service.create_position(position())
            second_position = position().model_copy(
                update={
                    "position_id": "position-second",
                    "profile_id": "profile-second",
                }
            )
            service.create_position(second_position)
            assert len(service.list_positions()) == 2
    finally:
        db.dispose()


def test_risk_summary_distinguishes_warning_and_reanalysis_bands(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            service = PortfolioService(session, "personal-demo")
            service.create_profile(investment_profile())
            service.create_asset(asset())
            service.create_position(position(value="400"))
            warning_snapshot = service.create_analysis_snapshot("position-007300", generated_at=NOW)
            assert (
                service.build_ai_risk_summary(warning_snapshot.snapshot_id).risk_band
                is PositionRiskBand.LOSS_WARNING
            )

            service.update_position(position(value="300"))
            reanalysis_snapshot = service.create_analysis_snapshot(
                "position-007300", generated_at=NOW
            )
            assert (
                service.build_ai_risk_summary(reanalysis_snapshot.snapshot_id).risk_band
                is PositionRiskBand.REANALYSIS_REQUIRED
            )
    finally:
        db.dispose()


def test_loopback_client_detection_is_strict() -> None:
    assert is_loopback_client("127.0.0.1") is True
    assert is_loopback_client("::1") is True
    assert is_loopback_client("localhost") is True
    assert is_loopback_client("192.0.2.1") is False
    assert is_loopback_client(None) is False
