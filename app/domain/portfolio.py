"""Investment profile, fund asset, position, and market snapshot models."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, Identifier, Money, MoneyRange, SignedRatio
from app.domain.enums import AssetType


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
    snapshot_at: AwareDatetime

    @model_validator(mode="after")
    def validate_position(self) -> Position:
        if self.opened_on > self.latest_purchase_on:
            raise ValueError("latest purchase cannot precede position opening")
        if self.cost_basis.currency != self.current_value.currency:
            raise ValueError("position monetary values must use one currency")
        if (
            self.recurring_contribution is not None
            and self.recurring_contribution.currency != self.current_value.currency
        ):
            raise ValueError("recurring contribution currency must match position")
        return self


class MarketSnapshot(DomainModel):
    asset_id: Identifier
    as_of: AwareDatetime
    net_asset_value: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    daily_change_ratio: SignedRatio | None = None
    source_id: Identifier
    is_stale: bool = False
