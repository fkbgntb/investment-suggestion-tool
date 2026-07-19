"""Investment profile, fund asset, position, and market snapshot models."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import AnyHttpUrl, AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, Identifier, Money, MoneyRange, SignedRatio, UnitInterval
from app.domain.enums import AssetType, FeeDataStatus, PositionRiskBand


class RedemptionFeeTier(DomainModel):
    minimum_holding_days: int = Field(ge=0, le=36_500)
    maximum_holding_days: int | None = Field(default=None, ge=0, le=36_500)
    fee_rate: UnitInterval

    @model_validator(mode="after")
    def validate_day_range(self) -> RedemptionFeeTier:
        if (
            self.maximum_holding_days is not None
            and self.maximum_holding_days < self.minimum_holding_days
        ):
            raise ValueError("redemption fee tier maximum days cannot precede minimum days")
        return self


class FundFeePolicy(DomainModel):
    status: FeeDataStatus = FeeDataStatus.UNKNOWN
    purchase_fee_rate: UnitInterval | None = None
    redemption_fee_tiers: tuple[RedemptionFeeTier, ...] = Field(
        default_factory=tuple, max_length=100
    )
    source_name: str | None = Field(default=None, max_length=200)
    source_url: AnyHttpUrl | None = None
    effective_on: date | None = None
    verified_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_status_and_tiers(self) -> FundFeePolicy:
        if self.status is FeeDataStatus.UNKNOWN:
            if (
                any(
                    value is not None
                    for value in (
                        self.purchase_fee_rate,
                        self.source_name,
                        self.source_url,
                        self.effective_on,
                        self.verified_at,
                    )
                )
                or self.redemption_fee_tiers
            ):
                raise ValueError("unknown fee data cannot contain unverified fee values")
            return self

        if not self.source_name or self.effective_on is None:
            raise ValueError("known fee data requires a source and effective date")
        if self.status is FeeDataStatus.OFFICIAL_VERIFIED and (
            self.source_url is None or self.verified_at is None
        ):
            raise ValueError("official fee data requires a source URL and verification time")

        tiers = sorted(self.redemption_fee_tiers, key=lambda item: item.minimum_holding_days)
        for previous, current in zip(tiers, tiers[1:], strict=False):
            if previous.maximum_holding_days is None:
                raise ValueError("an open-ended redemption fee tier must be last")
            if current.minimum_holding_days <= previous.maximum_holding_days:
                raise ValueError("redemption fee tiers cannot overlap")
        return self


class PurchaseLot(DomainModel):
    lot_id: Identifier
    purchased_on: date
    confirmed_on: date | None = None
    units: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    cost_amount: Money

    @model_validator(mode="after")
    def validate_confirmation_date(self) -> PurchaseLot:
        if self.confirmed_on is not None and self.confirmed_on < self.purchased_on:
            raise ValueError("confirmation date cannot precede purchase date")
        if self.cost_amount.amount <= 0:
            raise ValueError("purchase lot cost must be positive")
        return self


class InvestmentProfile(DomainModel):
    profile_id: Identifier
    name: str = Field(min_length=1, max_length=120)
    base_currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    maximum_portfolio_loss: Money
    fund_loss_warning: Money
    fund_reanalysis_threshold: Money
    single_add_range: MoneyRange | None = None
    monthly_contribution: Money | None = None
    accepts_long_term_volatility: bool
    advisory_only: bool = True

    @model_validator(mode="after")
    def validate_risk_boundaries(self) -> InvestmentProfile:
        money_values = (
            self.maximum_portfolio_loss,
            self.fund_loss_warning,
            self.fund_reanalysis_threshold,
            self.monthly_contribution,
        )
        for value in money_values:
            if value is not None and value.currency != self.base_currency:
                raise ValueError("profile money values must use the base currency")
        if (
            self.single_add_range is not None
            and self.single_add_range.minimum.currency != self.base_currency
        ):
            raise ValueError("single add range must use the base currency")
        if self.fund_loss_warning.amount > self.fund_reanalysis_threshold.amount:
            raise ValueError("loss warning cannot exceed the reanalysis threshold")
        if self.fund_reanalysis_threshold.amount > self.maximum_portfolio_loss.amount:
            raise ValueError("fund threshold cannot exceed the portfolio boundary")
        if not self.advisory_only:
            raise ValueError("this system only supports advisory profiles")
        return self


class Asset(DomainModel):
    asset_id: Identifier
    fund_code: str = Field(min_length=6, max_length=32, pattern=r"^[A-Za-z0-9.:-]+$")
    name: str = Field(min_length=1, max_length=240)
    asset_type: AssetType
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    market: str = Field(min_length=2, max_length=32)
    tracking_asset_code: str | None = Field(default=None, max_length=32)
    fee_policy: FundFeePolicy = Field(default_factory=FundFeePolicy)


class Position(DomainModel):
    position_id: Identifier
    profile_id: Identifier
    asset_id: Identifier
    units: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    cost_basis: Money
    current_value: Money
    average_cost_per_unit: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    opened_on: date
    latest_purchase_on: date
    recurring_contribution: Money | None = None
    purchase_lots: tuple[PurchaseLot, ...] = Field(default_factory=tuple, max_length=500)
    holding_period_data_complete: bool = False
    snapshot_at: AwareDatetime

    @model_validator(mode="after")
    def validate_position(self) -> Position:
        if self.opened_on > self.latest_purchase_on:
            raise ValueError("latest purchase cannot precede position opening")
        if self.latest_purchase_on > self.snapshot_at.date():
            raise ValueError("latest purchase cannot follow the position snapshot")
        if self.cost_basis.amount <= 0:
            raise ValueError("position cost basis must be positive")
        if self.cost_basis.currency != self.current_value.currency:
            raise ValueError("position monetary values must use one currency")
        if (
            self.recurring_contribution is not None
            and self.recurring_contribution.currency != self.current_value.currency
        ):
            raise ValueError("recurring contribution currency must match position")
        if self.holding_period_data_complete and not self.purchase_lots:
            raise ValueError("complete holding-period data requires at least one purchase lot")
        if any(
            lot.cost_amount.currency != self.current_value.currency for lot in self.purchase_lots
        ):
            raise ValueError("purchase lot currency must match position")
        if self.purchase_lots:
            if min(lot.purchased_on for lot in self.purchase_lots) < self.opened_on:
                raise ValueError("purchase lot cannot precede position opening")
            if max(lot.purchased_on for lot in self.purchase_lots) > self.latest_purchase_on:
                raise ValueError("purchase lot cannot follow latest purchase date")
            if any(
                lot.confirmed_on is not None and lot.confirmed_on > self.snapshot_at.date()
                for lot in self.purchase_lots
            ):
                raise ValueError("purchase lot confirmation cannot follow the position snapshot")
        return self


class PositionAnalysisSnapshot(DomainModel):
    snapshot_id: Identifier
    position: Position
    asset_type: AssetType
    fee_policy: FundFeePolicy
    generated_at: AwareDatetime
    purpose: Literal["ANALYSIS"] = "ANALYSIS"


class PortfolioAIRiskSummary(DomainModel):
    """Privacy-minimized portfolio input permitted to leave the local machine."""

    snapshot_id: Identifier
    asset_id: Identifier
    asset_type: AssetType
    unrealized_return_ratio: Decimal = Field(ge=Decimal("-1"), le=Decimal("100"))
    loss_boundary_used: UnitInterval
    risk_band: PositionRiskBand
    recurring_contribution_active: bool
    holding_period_days: int = Field(ge=0, le=36_500)
    holding_period_data_complete: bool
    fee_data_status: FeeDataStatus
    generated_at: AwareDatetime


class MarketSnapshot(DomainModel):
    asset_id: Identifier
    as_of: AwareDatetime
    net_asset_value: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    daily_change_ratio: SignedRatio | None = None
    source_id: Identifier
    is_stale: bool = False
