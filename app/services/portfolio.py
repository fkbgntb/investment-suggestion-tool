"""Portfolio use cases with local privacy and audit boundaries."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from ipaddress import ip_address
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.enums import AssetType, PositionRiskBand
from app.domain.portfolio import (
    Asset,
    InvestmentProfile,
    PortfolioAIRiskSummary,
    Position,
    PositionAnalysisSnapshot,
)
from app.storage.models import utc_now
from app.storage.repositories import AuditRepository, PortfolioRepository, WorkspaceRepository


class PortfolioNotFound(LookupError):
    pass


class PortfolioConflict(ValueError):
    pass


class PortfolioPrivacyError(ValueError):
    pass


_FORBIDDEN_AI_KEYS = {
    "account",
    "account_number",
    "available_cash",
    "cost_basis",
    "current_value",
    "name",
    "password",
    "profile_id",
    "units",
}


def identify_asset_type(
    *, exchange_traded: bool, feeder_fund: bool, index_tracking: bool
) -> AssetType:
    """Classify from explicit product facts; never guess from a product name."""
    if exchange_traded and feeder_fund:
        raise ValueError("a feeder fund cannot also be an exchange-traded fund share")
    if feeder_fund:
        return AssetType.ETF_LINK
    if exchange_traded:
        return AssetType.ETF
    if index_tracking:
        return AssetType.INDEX_FUND
    return AssetType.UNKNOWN


def _assert_private_ai_payload(value: object, *, path: str = "summary") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _FORBIDDEN_AI_KEYS:
                raise PortfolioPrivacyError(f"private field is forbidden in AI input: {path}.{key}")
            _assert_private_ai_payload(nested, path=f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, nested in enumerate(value):
            _assert_private_ai_payload(nested, path=f"{path}[{index}]")


def is_loopback_client(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return host.casefold() == "localhost"


class PortfolioService:
    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.repository = PortfolioRepository(session, workspace_id)
        self.audit = AuditRepository(session, workspace_id)

    def ensure_workspace(self) -> None:
        workspaces = WorkspaceRepository(self.session)
        if not workspaces.exists(self.workspace_id):
            workspaces.create(self.workspace_id, "Personal demo workspace")

    def create_profile(self, profile: InvestmentProfile) -> InvestmentProfile:
        self.ensure_workspace()
        if self.repository.get_profile(profile.profile_id) is not None:
            raise PortfolioConflict("investment profile already exists")
        try:
            with self.session.begin_nested():
                self.repository.add_profile(profile)
        except IntegrityError as error:
            raise PortfolioConflict("investment profile conflicts with stored data") from error
        self._audit_change("profile_created", "investment_profile", profile.profile_id)
        return profile

    def update_profile(self, profile: InvestmentProfile) -> InvestmentProfile:
        if not self.repository.update_profile(profile):
            raise PortfolioNotFound("investment profile was not found")
        self._audit_change("profile_updated", "investment_profile", profile.profile_id)
        return profile

    def get_profile(self, profile_id: str) -> InvestmentProfile:
        profile = self.repository.get_profile(profile_id)
        if profile is None:
            raise PortfolioNotFound("investment profile was not found")
        return profile

    def list_profiles(self) -> tuple[InvestmentProfile, ...]:
        return self.repository.list_profiles()

    def create_asset(self, asset: Asset) -> Asset:
        self.ensure_workspace()
        if self.repository.get_asset(asset.asset_id) is not None:
            raise PortfolioConflict("asset already exists")
        try:
            with self.session.begin_nested():
                self.repository.add_asset(asset)
        except IntegrityError as error:
            raise PortfolioConflict("fund code or asset identifier already exists") from error
        self._audit_change("asset_created", "asset", asset.asset_id)
        return asset

    def update_asset(self, asset: Asset) -> Asset:
        try:
            with self.session.begin_nested():
                updated = self.repository.update_asset(asset)
        except IntegrityError as error:
            raise PortfolioConflict("fund code conflicts with another asset") from error
        if not updated:
            raise PortfolioNotFound("asset was not found")
        self._audit_change("asset_updated", "asset", asset.asset_id)
        return asset

    def get_asset(self, asset_id: str) -> Asset:
        asset = self.repository.get_asset(asset_id)
        if asset is None:
            raise PortfolioNotFound("asset was not found")
        return asset

    def list_assets(self) -> tuple[Asset, ...]:
        return self.repository.list_assets()

    def create_position(self, position: Position) -> Position:
        self.ensure_workspace()
        if self.repository.get_position(position.position_id) is not None:
            raise PortfolioConflict("position already exists")
        self._validate_position_references(position)
        try:
            with self.session.begin_nested():
                self.repository.add_position(position)
        except IntegrityError as error:
            raise PortfolioConflict("position conflicts with stored data") from error
        self._audit_change("holding_created", "holding_record", position.position_id)
        return position

    def update_position(self, position: Position) -> Position:
        self._validate_position_references(position)
        try:
            with self.session.begin_nested():
                updated = self.repository.update_position(position)
        except IntegrityError as error:
            raise PortfolioConflict("position update conflicts with stored data") from error
        if not updated:
            raise PortfolioNotFound("position was not found")
        self._audit_change("holding_updated", "holding_record", position.position_id)
        return position

    def get_position(self, position_id: str) -> Position:
        position = self.repository.get_position(position_id)
        if position is None:
            raise PortfolioNotFound("position was not found")
        return position

    def list_positions(self) -> tuple[Position, ...]:
        return self.repository.list_positions()

    def delete_position(self, position_id: str) -> None:
        if not self.repository.delete_position(position_id):
            raise PortfolioNotFound("position was not found")
        self._audit_change("holding_deleted", "holding_record", position_id)

    def create_analysis_snapshot(
        self, position_id: str, *, generated_at: datetime | None = None
    ) -> PositionAnalysisSnapshot:
        position = self.get_position(position_id)
        asset = self.get_asset(position.asset_id)
        timestamp = generated_at or utc_now()
        snapshot = PositionAnalysisSnapshot(
            snapshot_id=f"position-snapshot-{uuid4()}",
            position=position,
            asset_type=asset.asset_type,
            fee_policy=asset.fee_policy,
            generated_at=timestamp,
        )
        self.repository.add_position_snapshot(snapshot)
        self._audit_change("analysis_snapshot_created", "analysis_snapshot", snapshot.snapshot_id)
        return snapshot

    def build_ai_risk_summary(self, snapshot_id: str) -> PortfolioAIRiskSummary:
        snapshot = self.repository.get_position_snapshot(snapshot_id)
        if snapshot is None:
            raise PortfolioNotFound("position analysis snapshot was not found")
        profile = self.get_profile(snapshot.position.profile_id)

        cost = snapshot.position.cost_basis.amount
        current = snapshot.position.current_value.amount
        loss = max(cost - current, Decimal("0"))
        reanalysis_boundary = profile.fund_reanalysis_threshold.amount
        if reanalysis_boundary == 0:
            loss_boundary_used = Decimal("1") if loss > 0 else Decimal("0")
        else:
            loss_boundary_used = min(loss / reanalysis_boundary, Decimal("1"))

        if loss > 0 and loss >= profile.fund_reanalysis_threshold.amount:
            risk_band = PositionRiskBand.REANALYSIS_REQUIRED
        elif loss > 0 and loss >= profile.fund_loss_warning.amount:
            risk_band = PositionRiskBand.LOSS_WARNING
        else:
            risk_band = PositionRiskBand.WITHIN_PLAN

        holding_days = max(
            (snapshot.generated_at.date() - snapshot.position.opened_on).days,
            0,
        )
        summary = PortfolioAIRiskSummary(
            snapshot_id=snapshot.snapshot_id,
            asset_id=snapshot.position.asset_id,
            asset_type=snapshot.asset_type,
            unrealized_return_ratio=(current - cost) / cost,
            loss_boundary_used=loss_boundary_used,
            risk_band=risk_band,
            recurring_contribution_active=snapshot.position.recurring_contribution is not None,
            holding_period_days=holding_days,
            holding_period_data_complete=snapshot.position.holding_period_data_complete,
            fee_data_status=snapshot.fee_policy.status,
            generated_at=snapshot.generated_at,
        )
        _assert_private_ai_payload(summary.model_dump(mode="json"))
        return summary

    def _validate_position_references(self, position: Position) -> None:
        profile = self.repository.get_profile(position.profile_id)
        if profile is None:
            raise PortfolioConflict("position references an unknown investment profile")
        asset = self.repository.get_asset(position.asset_id)
        if asset is None:
            raise PortfolioConflict("position references an unknown asset")
        if asset.currency != position.current_value.currency:
            raise PortfolioConflict("position currency must match the asset currency")
        if position.snapshot_at > utc_now():
            raise PortfolioConflict("position snapshot time cannot be in the future")

    def _audit_change(self, event_type: str, target_type: str, target_id: str) -> None:
        self.audit.record(
            event_type=event_type,
            actor="local_user",
            target_type=target_type,
            target_id=target_id,
            outcome="completed",
            details={"source": "local_api"},
        )


def assert_private_ai_summary(summary: PortfolioAIRiskSummary) -> dict[str, Any]:
    payload = summary.model_dump(mode="json")
    _assert_private_ai_payload(payload)
    return payload
