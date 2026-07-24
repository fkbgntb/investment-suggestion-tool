from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.enums import AssetType, SourceKind, TrustTier
from app.domain.portfolio import Asset
from app.domain.taxonomy import Source
from app.services.market_metrics import (
    MarketPoint,
    calculate_market_snapshot,
    extract_h30184_factsheet_snapshot,
)
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.repositories import (
    IdempotencyConflict,
    MarketSnapshotRepository,
    PortfolioRepository,
    SourceRepository,
    WorkspaceRepository,
)

NOW = datetime(2026, 7, 2, 7, 0, tzinfo=UTC)


def test_split_adjustment_is_not_treated_as_a_real_crash() -> None:
    snapshot = calculate_market_snapshot(
        asset_id="asset-512480",
        code="512480",
        source_id="sse-512480",
        points=(
            MarketPoint(as_of=NOW - timedelta(days=1), price=Decimal("10")),
            MarketPoint(
                as_of=NOW,
                price=Decimal("5"),
                split_adjustment_factor=Decimal("2"),
            ),
        ),
    )

    assert snapshot.daily_change_ratio == Decimal("0")
    assert snapshot.period_return_ratio == Decimal("0")
    assert snapshot.maximum_drawdown_ratio == Decimal("0")
    assert snapshot.close_price == Decimal("5")
    assert snapshot.split_adjustment_factor == Decimal("2")


def test_h30184_factsheet_extracts_only_unambiguous_sample_count_and_date() -> None:
    snapshot = extract_h30184_factsheet_snapshot(
        "中证全指半导体产品与设备指数 H30184 样本股数 87 "
        "2013年7月15日 2026年6月30日 滚动市盈率 128.42 13.13年年化"
    )

    assert snapshot is not None
    assert snapshot.code == "H30184"
    assert snapshot.sample_count == 87
    assert snapshot.as_of.isoformat() == "2026-06-30T00:00:00+08:00"
    assert snapshot.price_earnings_ratio is None


def test_market_metrics_are_deterministic() -> None:
    snapshot = calculate_market_snapshot(
        asset_id="asset-h30184",
        code="H30184",
        source_id="csi-h30184",
        points=(
            MarketPoint(as_of=NOW - timedelta(days=2), price=Decimal("100")),
            MarketPoint(as_of=NOW - timedelta(days=1), price=Decimal("110")),
            MarketPoint(as_of=NOW, price=Decimal("99"), volume=Decimal("12345")),
        ),
        price_earnings_ratio=Decimal("104.55"),
        price_book_ratio=Decimal("9.06"),
    )

    assert snapshot.daily_change_ratio == Decimal("-0.1")
    assert snapshot.period_return_ratio == Decimal("-0.01")
    assert snapshot.maximum_drawdown_ratio == Decimal("-0.1")
    assert snapshot.volatility_ratio is not None
    assert snapshot.volume == Decimal("12345")


def test_market_snapshot_repository_is_idempotent_and_workspace_scoped(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'market.sqlite3').as_posix()}"
    upgrade_database(database_url)
    database = Database(database_url)
    snapshot = calculate_market_snapshot(
        asset_id="asset-512480",
        code="512480",
        source_id="sse-512480",
        points=(MarketPoint(as_of=NOW, price=Decimal("5")),),
    )
    try:
        with database.session() as session:
            WorkspaceRepository(session).create("personal", "Personal")
            PortfolioRepository(session, "personal").add_asset(
                Asset(
                    asset_id="asset-512480",
                    fund_code="512480",
                    name="Semiconductor ETF",
                    asset_type=AssetType.ETF,
                    market="CN",
                )
            )
            SourceRepository(session, "personal").add(
                Source(
                    source_id="sse-512480",
                    name="SSE",
                    kind=SourceKind.REGULATOR,
                    trust_tier=TrustTier.PRIMARY,
                    base_url="https://www.sse.com.cn/",
                    regions=("cn",),
                    languages=("zh",),
                    adapter_name="official-document",
                )
            )
            repository = MarketSnapshotRepository(session, "personal")
            first, created = repository.add_if_absent(snapshot)
            duplicate, duplicate_created = repository.add_if_absent(snapshot)
            assert created is True
            assert duplicate_created is False
            assert duplicate.market_snapshot_id == first.market_snapshot_id
            assert repository.list_for_asset("asset-512480") == (snapshot,)

            changed = snapshot.model_copy(update={"volume": Decimal("1")})
            with pytest.raises(IdempotencyConflict):
                repository.add_if_absent(changed)
    finally:
        database.dispose()
