from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.enums import FeeDataStatus
from app.domain.portfolio import FundFeePolicy, Position, PurchaseLot, RedemptionFeeTier
from tests.domain_factories import NOW, OPENED_ON, money


def test_fee_tiers_require_valid_ranges_and_official_provenance() -> None:
    with pytest.raises(ValidationError, match="maximum days"):
        RedemptionFeeTier(
            minimum_holding_days=30,
            maximum_holding_days=7,
            fee_rate=Decimal("0.01"),
        )

    with pytest.raises(ValidationError, match="source URL"):
        FundFeePolicy(
            status=FeeDataStatus.OFFICIAL_VERIFIED,
            source_name="official prospectus",
            effective_on=OPENED_ON,
        )

    with pytest.raises(ValidationError, match="open-ended"):
        FundFeePolicy(
            status=FeeDataStatus.USER_PROVIDED,
            source_name="manual note",
            effective_on=OPENED_ON,
            redemption_fee_tiers=(
                RedemptionFeeTier(
                    minimum_holding_days=0,
                    maximum_holding_days=None,
                    fee_rate=Decimal("0.01"),
                ),
                RedemptionFeeTier(
                    minimum_holding_days=30,
                    maximum_holding_days=60,
                    fee_rate=Decimal("0"),
                ),
            ),
        )


def test_purchase_lot_requires_positive_cost_and_ordered_confirmation() -> None:
    with pytest.raises(ValidationError, match="positive"):
        PurchaseLot(
            lot_id="lot-1",
            purchased_on=OPENED_ON,
            units=Decimal("1"),
            cost_amount=money("0"),
        )
    with pytest.raises(ValidationError, match="confirmation"):
        PurchaseLot(
            lot_id="lot-1",
            purchased_on=date(2026, 4, 2),
            confirmed_on=date(2026, 4, 1),
            units=Decimal("1"),
            cost_amount=money("5"),
        )


def test_position_rejects_incomplete_or_inconsistent_holding_period_data() -> None:
    base = {
        "position_id": "position-test",
        "profile_id": "profile-demo",
        "asset_id": "asset-007300",
        "units": Decimal("1"),
        "cost_basis": money("10"),
        "current_value": money("11"),
        "average_cost_per_unit": Decimal("10"),
        "opened_on": OPENED_ON,
        "latest_purchase_on": date(2026, 7, 1),
        "snapshot_at": NOW,
    }
    with pytest.raises(ValidationError, match="at least one"):
        Position(**base, holding_period_data_complete=True)

    with pytest.raises(ValidationError, match="snapshot"):
        Position(
            **{
                **base,
                "latest_purchase_on": date(2026, 7, 20),
            }
        )

    future_confirmation = PurchaseLot(
        lot_id="lot-1",
        purchased_on=OPENED_ON,
        confirmed_on=date(2026, 7, 20),
        units=Decimal("1"),
        cost_amount=money("10"),
    )
    with pytest.raises(ValidationError, match="confirmation"):
        Position(**base, purchase_lots=(future_confirmation,))

    with pytest.raises(ValidationError, match="currency"):
        Position(
            **{
                **base,
                "current_value": {"amount": "11", "currency": "USD"},
            }
        )


def test_position_snapshot_requires_timezone_aware_time() -> None:
    with pytest.raises(ValidationError):
        Position(
            position_id="position-test",
            profile_id="profile-demo",
            asset_id="asset-007300",
            units=Decimal("1"),
            cost_basis=money("10"),
            current_value=money("11"),
            average_cost_per_unit=Decimal("10"),
            opened_on=OPENED_ON,
            latest_purchase_on=OPENED_ON,
            snapshot_at=datetime(2026, 7, 19),
        )

    valid = datetime(2026, 7, 19, tzinfo=UTC)
    assert valid.tzinfo is UTC
