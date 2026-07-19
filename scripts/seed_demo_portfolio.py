"""Create the known 007300 demo records without overwriting existing local data."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.config import Settings
from app.domain.base import Money, MoneyRange
from app.domain.enums import AssetType
from app.domain.portfolio import Asset, InvestmentProfile, Position
from app.services.portfolio import PortfolioService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.paths import prepare_storage_paths


def money(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="CNY")


def demo_profile() -> InvestmentProfile:
    return InvestmentProfile(
        profile_id="profile-demo",
        name="个人训练账户",
        maximum_portfolio_loss=money("1000"),
        fund_loss_warning=money("200"),
        fund_reanalysis_threshold=money("300"),
        single_add_range=MoneyRange(minimum=money("100"), maximum=money("200")),
        monthly_contribution=money("50"),
        accepts_long_term_volatility=True,
    )


def demo_asset() -> Asset:
    return Asset(
        asset_id="asset-007300",
        fund_code="007300",
        name="国联安中证全指半导体产品与设备ETF联接A",
        asset_type=AssetType.ETF_LINK,
        market="CN",
        tracking_asset_code="512480",
    )


def demo_position() -> Position:
    return Position(
        position_id="position-007300",
        profile_id="profile-demo",
        asset_id="asset-007300",
        units=Decimal("157.89"),
        cost_basis=money("640"),
        current_value=money("653.59"),
        average_cost_per_unit=Decimal("4.0535"),
        opened_on=date(2026, 4, 1),
        latest_purchase_on=date(2026, 7, 1),
        recurring_contribution=money("50"),
        purchase_lots=(),
        holding_period_data_complete=False,
        snapshot_at=datetime(2026, 7, 17, 0, 0, tzinfo=UTC),
    )


def main() -> int:
    settings = Settings()
    paths = prepare_storage_paths(settings)
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    created: list[str] = []
    skipped: list[str] = []
    try:
        with database.session() as session:
            service = PortfolioService(session, settings.portfolio_workspace_id)
            profile = demo_profile()
            asset = demo_asset()
            position = demo_position()

            if service.repository.get_profile(profile.profile_id) is None:
                service.create_profile(profile)
                created.append(profile.profile_id)
            else:
                skipped.append(profile.profile_id)

            if service.repository.get_asset(asset.asset_id) is None:
                service.create_asset(asset)
                created.append(asset.asset_id)
            else:
                skipped.append(asset.asset_id)

            if service.repository.get_position(position.position_id) is None:
                service.create_position(position)
                created.append(position.position_id)
            else:
                skipped.append(position.position_id)
    finally:
        database.dispose()

    print(f"portfolio data directory: {paths.data_dir}")
    print(f"created record IDs: {', '.join(created) if created else 'none'}")
    print(f"existing record IDs left unchanged: {', '.join(skipped) if skipped else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
