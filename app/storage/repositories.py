"""Database-agnostic repositories with workspace isolation and idempotency."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.collection import FetchFailure, SourceAdapterState, SourceHealthSnapshot
from app.domain.documents import RawDocument
from app.domain.enums import TransitionOutcome
from app.domain.portfolio import (
    Asset,
    InvestmentProfile,
    MarketSnapshot,
    Position,
    PositionAnalysisSnapshot,
)
from app.domain.state_machine import StateTransitionRecord
from app.domain.taxonomy import Source, TaxonomyConfiguration
from app.storage.models import (
    ActiveTaxonomyConfigurationRow,
    AssetRow,
    AuditEventRow,
    CrawlRunRow,
    InvestmentProfileRow,
    MarketSnapshotRow,
    PositionRow,
    PositionSnapshotRow,
    RawDocumentRow,
    ScheduledTaskRow,
    SchedulerStateRow,
    SourceAdapterStateRow,
    SourceHealthRow,
    SourceRow,
    TaxonomyConfigurationRow,
    WorkspaceRow,
    utc_now,
)


class ConcurrentStateChange(RuntimeError):
    """The persisted document state changed after the transition was evaluated."""


class AuditDetailRejected(ValueError):
    """Audit details attempted to include sensitive or oversized values."""


class IdempotencyConflict(ValueError):
    """An idempotency key was reused with a different operation payload."""


@dataclass(frozen=True)
class CrawlRunInput:
    crawl_run_id: str
    workspace_id: str
    source_id: str
    idempotency_key: str
    status: str
    scheduled_at: datetime
    payload: dict[str, Any]


_SENSITIVE_AUDIT_KEYS = {
    "account",
    "account_number",
    "api_key",
    "authorization",
    "cookie",
    "headers",
    "holding",
    "holdings",
    "password",
    "position",
    "positions",
    "raw_body",
    "secret",
    "token",
}


def _assert_safe_audit_details(value: object, *, path: str = "details") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
            key_parts = set(normalized.split("_"))
            contains_sensitive_phrase = any(
                normalized == sensitive
                or normalized.startswith(f"{sensitive}_")
                or normalized.endswith(f"_{sensitive}")
                or f"_{sensitive}_" in normalized
                for sensitive in _SENSITIVE_AUDIT_KEYS
            )
            if contains_sensitive_phrase or key_parts & _SENSITIVE_AUDIT_KEYS:
                raise AuditDetailRejected(f"sensitive audit field is not allowed: {path}.{key}")
            _assert_safe_audit_details(nested, path=f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, nested in enumerate(value):
            _assert_safe_audit_details(nested, path=f"{path}[{index}]")


class WorkspaceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self, workspace_id: str, name: str, *, raw_document_retention_days: int = 90
    ) -> WorkspaceRow:
        if not 1 <= raw_document_retention_days <= 3650:
            raise ValueError("raw document retention must be between 1 and 3650 days")
        row = WorkspaceRow(
            workspace_id=workspace_id,
            name=name,
            raw_document_retention_days=raw_document_retention_days,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def exists(self, workspace_id: str) -> bool:
        query = (
            select(func.count())
            .select_from(WorkspaceRow)
            .where(WorkspaceRow.workspace_id == workspace_id)
        )
        return bool(self.session.scalar(query))

    def delete(self, workspace_id: str) -> bool:
        result = self.session.execute(
            delete(WorkspaceRow).where(WorkspaceRow.workspace_id == workspace_id)
        )
        return bool(result.rowcount)


class PortfolioRepository:
    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def add_profile(self, profile: InvestmentProfile) -> InvestmentProfileRow:
        row = InvestmentProfileRow(
            profile_id=profile.profile_id,
            workspace_id=self.workspace_id,
            name=profile.name,
            schema_version=profile.schema_version,
            payload=profile.model_dump(mode="json"),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_profile(self, profile_id: str) -> InvestmentProfile | None:
        row = self.session.scalar(
            select(InvestmentProfileRow).where(
                InvestmentProfileRow.workspace_id == self.workspace_id,
                InvestmentProfileRow.profile_id == profile_id,
            )
        )
        return InvestmentProfile.model_validate(row.payload) if row is not None else None

    def list_profiles(self) -> tuple[InvestmentProfile, ...]:
        rows = self.session.scalars(
            select(InvestmentProfileRow)
            .where(InvestmentProfileRow.workspace_id == self.workspace_id)
            .order_by(InvestmentProfileRow.created_at)
        )
        return tuple(InvestmentProfile.model_validate(row.payload) for row in rows)

    def update_profile(self, profile: InvestmentProfile) -> bool:
        statement = (
            update(InvestmentProfileRow)
            .where(
                InvestmentProfileRow.workspace_id == self.workspace_id,
                InvestmentProfileRow.profile_id == profile.profile_id,
            )
            .values(
                name=profile.name,
                schema_version=profile.schema_version,
                payload=profile.model_dump(mode="json"),
                updated_at=utc_now(),
            )
        )
        return self.session.execute(statement).rowcount == 1

    def delete_profile(self, profile_id: str) -> bool:
        result = self.session.execute(
            delete(InvestmentProfileRow).where(
                InvestmentProfileRow.workspace_id == self.workspace_id,
                InvestmentProfileRow.profile_id == profile_id,
            )
        )
        return result.rowcount == 1

    def add_asset(self, asset: Asset) -> AssetRow:
        row = AssetRow(
            asset_id=asset.asset_id,
            workspace_id=self.workspace_id,
            fund_code=asset.fund_code,
            asset_type=asset.asset_type.value,
            schema_version=asset.schema_version,
            payload=asset.model_dump(mode="json"),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_asset(self, asset_id: str) -> Asset | None:
        row = self.session.scalar(
            select(AssetRow).where(
                AssetRow.workspace_id == self.workspace_id,
                AssetRow.asset_id == asset_id,
            )
        )
        return Asset.model_validate(row.payload) if row is not None else None

    def list_assets(self) -> tuple[Asset, ...]:
        rows = self.session.scalars(
            select(AssetRow)
            .where(AssetRow.workspace_id == self.workspace_id)
            .order_by(AssetRow.fund_code)
        )
        return tuple(Asset.model_validate(row.payload) for row in rows)

    def update_asset(self, asset: Asset) -> bool:
        statement = (
            update(AssetRow)
            .where(
                AssetRow.workspace_id == self.workspace_id,
                AssetRow.asset_id == asset.asset_id,
            )
            .values(
                fund_code=asset.fund_code,
                asset_type=asset.asset_type.value,
                schema_version=asset.schema_version,
                payload=asset.model_dump(mode="json"),
                updated_at=utc_now(),
            )
        )
        return self.session.execute(statement).rowcount == 1

    def add_position(self, position: Position) -> PositionRow:
        row = PositionRow(
            position_id=position.position_id,
            workspace_id=self.workspace_id,
            profile_id=position.profile_id,
            asset_id=position.asset_id,
            snapshot_at=position.snapshot_at,
            schema_version=position.schema_version,
            payload=position.model_dump(mode="json"),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_position(self, position_id: str) -> Position | None:
        row = self.session.scalar(
            select(PositionRow).where(
                PositionRow.workspace_id == self.workspace_id,
                PositionRow.position_id == position_id,
            )
        )
        return Position.model_validate(row.payload) if row is not None else None

    def list_positions(self) -> tuple[Position, ...]:
        rows = self.session.scalars(
            select(PositionRow)
            .where(PositionRow.workspace_id == self.workspace_id)
            .order_by(PositionRow.snapshot_at.desc())
        )
        return tuple(Position.model_validate(row.payload) for row in rows)

    def update_position(self, position: Position) -> bool:
        statement = (
            update(PositionRow)
            .where(
                PositionRow.workspace_id == self.workspace_id,
                PositionRow.position_id == position.position_id,
            )
            .values(
                profile_id=position.profile_id,
                asset_id=position.asset_id,
                snapshot_at=position.snapshot_at,
                schema_version=position.schema_version,
                payload=position.model_dump(mode="json"),
                updated_at=utc_now(),
            )
        )
        return self.session.execute(statement).rowcount == 1

    def delete_position(self, position_id: str) -> bool:
        result = self.session.execute(
            delete(PositionRow).where(
                PositionRow.workspace_id == self.workspace_id,
                PositionRow.position_id == position_id,
            )
        )
        return result.rowcount == 1

    def add_position_snapshot(self, snapshot: PositionAnalysisSnapshot) -> PositionSnapshotRow:
        row = PositionSnapshotRow(
            snapshot_id=snapshot.snapshot_id,
            workspace_id=self.workspace_id,
            position_id=snapshot.position.position_id,
            asset_id=snapshot.position.asset_id,
            purpose=snapshot.purpose,
            schema_version=snapshot.schema_version,
            payload=snapshot.model_dump(mode="json"),
            generated_at=snapshot.generated_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_position_snapshot(self, snapshot_id: str) -> PositionAnalysisSnapshot | None:
        row = self.session.scalar(
            select(PositionSnapshotRow).where(
                PositionSnapshotRow.workspace_id == self.workspace_id,
                PositionSnapshotRow.snapshot_id == snapshot_id,
            )
        )
        return PositionAnalysisSnapshot.model_validate(row.payload) if row is not None else None


class MarketSnapshotRepository:
    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def add_if_absent(self, snapshot: MarketSnapshot) -> tuple[MarketSnapshotRow, bool]:
        payload = snapshot.model_dump(mode="json")
        existing = self.session.scalar(
            select(MarketSnapshotRow).where(
                MarketSnapshotRow.workspace_id == self.workspace_id,
                MarketSnapshotRow.asset_id == snapshot.asset_id,
                MarketSnapshotRow.code == snapshot.code,
                MarketSnapshotRow.source_id == snapshot.source_id,
                MarketSnapshotRow.as_of == snapshot.as_of,
            )
        )
        if existing is not None:
            if existing.payload != payload:
                raise IdempotencyConflict(
                    "the market snapshot key was reused with a different payload"
                )
            return existing, False
        snapshot_id = str(
            uuid5(
                NAMESPACE_URL,
                (
                    f"market:{self.workspace_id}:{snapshot.asset_id}:"
                    f"{snapshot.code}:{snapshot.source_id}:{snapshot.as_of.isoformat()}"
                ),
            )
        )
        row = MarketSnapshotRow(
            market_snapshot_id=snapshot_id,
            workspace_id=self.workspace_id,
            asset_id=snapshot.asset_id,
            source_id=snapshot.source_id,
            code=snapshot.code,
            as_of=snapshot.as_of,
            schema_version=snapshot.schema_version,
            payload=payload,
        )
        self.session.add(row)
        self.session.flush()
        return row, True

    def list_for_asset(self, asset_id: str, *, limit: int = 500) -> tuple[MarketSnapshot, ...]:
        if not 1 <= limit <= 10_000:
            raise ValueError("market snapshot limit must be between 1 and 10000")
        rows = self.session.scalars(
            select(MarketSnapshotRow)
            .where(
                MarketSnapshotRow.workspace_id == self.workspace_id,
                MarketSnapshotRow.asset_id == asset_id,
            )
            .order_by(MarketSnapshotRow.as_of.desc())
            .limit(limit)
        )
        return tuple(MarketSnapshot.model_validate(row.payload) for row in rows)


class TaxonomyRepository:
    """Persist complete, immutable taxonomy versions and a separate active pointer."""

    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def add_configuration(self, configuration: TaxonomyConfiguration) -> TaxonomyConfigurationRow:
        row = TaxonomyConfigurationRow(
            configuration_id=configuration.configuration_id,
            workspace_id=self.workspace_id,
            config_version=configuration.config_version,
            schema_version=configuration.schema_version,
            payload=configuration.model_dump(mode="json"),
            created_at=configuration.created_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_by_version(self, config_version: str) -> TaxonomyConfiguration | None:
        row = self.session.scalar(
            select(TaxonomyConfigurationRow).where(
                TaxonomyConfigurationRow.workspace_id == self.workspace_id,
                TaxonomyConfigurationRow.config_version == config_version,
            )
        )
        return TaxonomyConfiguration.model_validate(row.payload) if row is not None else None

    def get_by_id(self, configuration_id: str) -> TaxonomyConfiguration | None:
        row = self.session.scalar(
            select(TaxonomyConfigurationRow).where(
                TaxonomyConfigurationRow.workspace_id == self.workspace_id,
                TaxonomyConfigurationRow.configuration_id == configuration_id,
            )
        )
        return TaxonomyConfiguration.model_validate(row.payload) if row is not None else None

    def list_configurations(self) -> tuple[TaxonomyConfiguration, ...]:
        rows = self.session.scalars(
            select(TaxonomyConfigurationRow)
            .where(TaxonomyConfigurationRow.workspace_id == self.workspace_id)
            .order_by(TaxonomyConfigurationRow.created_at.desc())
        )
        return tuple(TaxonomyConfiguration.model_validate(row.payload) for row in rows)

    def get_active(self) -> TaxonomyConfiguration | None:
        statement = (
            select(TaxonomyConfigurationRow)
            .join(
                ActiveTaxonomyConfigurationRow,
                (
                    ActiveTaxonomyConfigurationRow.workspace_id
                    == TaxonomyConfigurationRow.workspace_id
                )
                & (
                    ActiveTaxonomyConfigurationRow.configuration_id
                    == TaxonomyConfigurationRow.configuration_id
                ),
            )
            .where(ActiveTaxonomyConfigurationRow.workspace_id == self.workspace_id)
        )
        row = self.session.scalar(statement)
        return TaxonomyConfiguration.model_validate(row.payload) if row is not None else None

    def activate(self, configuration: TaxonomyConfiguration) -> None:
        current = self.session.get(ActiveTaxonomyConfigurationRow, self.workspace_id)
        if current is None:
            self.session.add(
                ActiveTaxonomyConfigurationRow(
                    workspace_id=self.workspace_id,
                    configuration_id=configuration.configuration_id,
                    config_version=configuration.config_version,
                )
            )
        else:
            current.configuration_id = configuration.configuration_id
            current.config_version = configuration.config_version
            current.updated_at = utc_now()
        self.session.flush()


class SourceRepository:
    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def add(self, source: Source) -> SourceRow:
        row = SourceRow(
            source_id=source.source_id,
            workspace_id=self.workspace_id,
            base_url=str(source.base_url),
            adapter_name=source.adapter_name,
            enabled=source.enabled,
            schema_version=source.schema_version,
            payload=source.model_dump(mode="json"),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, source_id: str) -> Source | None:
        row = self.session.scalar(
            select(SourceRow).where(
                SourceRow.workspace_id == self.workspace_id,
                SourceRow.source_id == source_id,
            )
        )
        return Source.model_validate(row.payload) if row is not None else None

    def list(self, *, enabled_only: bool = False) -> tuple[Source, ...]:
        statement = select(SourceRow).where(SourceRow.workspace_id == self.workspace_id)
        if enabled_only:
            statement = statement.where(SourceRow.enabled.is_(True))
        rows = self.session.scalars(statement.order_by(SourceRow.source_id))
        return tuple(Source.model_validate(row.payload) for row in rows)

    def update(self, source: Source) -> bool:
        statement = (
            update(SourceRow)
            .where(
                SourceRow.workspace_id == self.workspace_id,
                SourceRow.source_id == source.source_id,
            )
            .values(
                base_url=str(source.base_url),
                adapter_name=source.adapter_name,
                enabled=source.enabled,
                schema_version=source.schema_version,
                payload=source.model_dump(mode="json"),
                updated_at=utc_now(),
            )
        )
        return self.session.execute(statement).rowcount == 1

    def get_health(self, source_id: str) -> SourceHealthSnapshot | None:
        row = self.session.scalar(
            select(SourceHealthRow).where(
                SourceHealthRow.workspace_id == self.workspace_id,
                SourceHealthRow.source_id == source_id,
            )
        )
        if row is None:
            return None
        return SourceHealthSnapshot(
            source_id=row.source_id,
            status=row.status,
            consecutive_failures=row.consecutive_failures,
            last_error_code=row.last_error_code,
            last_success_at=row.last_success_at,
            last_failure_at=row.last_failure_at,
            circuit_open_until=row.circuit_open_until,
        )

    def save_health(self, snapshot: SourceHealthSnapshot) -> None:
        row = self.session.scalar(
            select(SourceHealthRow).where(
                SourceHealthRow.workspace_id == self.workspace_id,
                SourceHealthRow.source_id == snapshot.source_id,
            )
        )
        values = snapshot.model_dump(mode="python", exclude={"schema_version", "source_id"})
        if row is None:
            self.session.add(
                SourceHealthRow(
                    source_id=snapshot.source_id,
                    workspace_id=self.workspace_id,
                    **values,
                )
            )
        else:
            for field, value in values.items():
                setattr(row, field, value)
            row.updated_at = utc_now()
        self.session.flush()

    def get_adapter_state(self, source_id: str) -> SourceAdapterState | None:
        row = self.session.scalar(
            select(SourceAdapterStateRow).where(
                SourceAdapterStateRow.workspace_id == self.workspace_id,
                SourceAdapterStateRow.source_id == source_id,
            )
        )
        if row is None:
            return None
        return SourceAdapterState(
            source_id=row.source_id,
            adapter_name=row.adapter_name,
            adapter_version=row.adapter_version,
            state_version=row.state_version,
            cursor=row.cursor,
            updated_at=row.updated_at,
        )

    def save_adapter_state(
        self,
        state: SourceAdapterState,
        *,
        expected_version: int,
    ) -> None:
        current = self.get_adapter_state(state.source_id)
        if current is None:
            if expected_version != 0 or state.state_version != 1:
                raise ConcurrentStateChange("adapter state version changed before persistence")
            try:
                with self.session.begin_nested():
                    self.session.add(
                        SourceAdapterStateRow(
                            source_id=state.source_id,
                            workspace_id=self.workspace_id,
                            adapter_name=state.adapter_name,
                            adapter_version=state.adapter_version,
                            state_version=state.state_version,
                            cursor=state.cursor,
                            updated_at=state.updated_at,
                        )
                    )
                    self.session.flush()
            except IntegrityError as error:
                raise ConcurrentStateChange(
                    "adapter state version changed before persistence"
                ) from error
            return
        if current.state_version != expected_version or state.state_version != expected_version + 1:
            raise ConcurrentStateChange("adapter state version changed before persistence")
        result = self.session.execute(
            update(SourceAdapterStateRow)
            .where(
                SourceAdapterStateRow.workspace_id == self.workspace_id,
                SourceAdapterStateRow.source_id == state.source_id,
                SourceAdapterStateRow.state_version == expected_version,
            )
            .values(
                adapter_name=state.adapter_name,
                adapter_version=state.adapter_version,
                state_version=state.state_version,
                cursor=state.cursor,
                updated_at=state.updated_at,
            )
        )
        if result.rowcount != 1:
            raise ConcurrentStateChange("adapter state version changed before persistence")


class SchedulerStateRepository:
    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def get_or_create(
        self,
        job_name: str,
        *,
        now: datetime,
        next_due_at: datetime,
    ) -> SchedulerStateRow:
        row = self.session.scalar(
            select(SchedulerStateRow).where(
                SchedulerStateRow.workspace_id == self.workspace_id,
                SchedulerStateRow.job_name == job_name,
            )
        )
        if row is not None:
            return row
        identifier = str(uuid5(NAMESPACE_URL, f"{self.workspace_id}:scheduler:{job_name}"))
        candidate = SchedulerStateRow(
            scheduler_state_id=identifier,
            workspace_id=self.workspace_id,
            job_name=job_name,
            next_due_at=next_due_at,
        )
        try:
            with self.session.begin_nested():
                self.session.add(candidate)
                self.session.flush()
            return candidate
        except IntegrityError:
            row = self.session.scalar(
                select(SchedulerStateRow).where(
                    SchedulerStateRow.workspace_id == self.workspace_id,
                    SchedulerStateRow.job_name == job_name,
                )
            )
            if row is None:
                raise
            return row

    def acquire(
        self,
        job_name: str,
        *,
        owner: str,
        now: datetime,
        lease_until: datetime,
        next_due_at: datetime,
    ) -> SchedulerStateRow | None:
        row = self.get_or_create(job_name, now=now, next_due_at=next_due_at)
        result = self.session.execute(
            update(SchedulerStateRow)
            .where(
                SchedulerStateRow.workspace_id == self.workspace_id,
                SchedulerStateRow.job_name == job_name,
                (SchedulerStateRow.lease_expires_at.is_(None))
                | (SchedulerStateRow.lease_expires_at <= now),
            )
            .values(lease_owner=owner, lease_expires_at=lease_until, updated_at=now)
        )
        if result.rowcount != 1:
            return None
        self.session.refresh(row)
        return row

    def complete(
        self,
        job_name: str,
        *,
        owner: str,
        completed_at: datetime,
        next_due_at: datetime,
        summary_date: str | None = None,
        cleanup_date: str | None = None,
    ) -> bool:
        values: dict[str, Any] = {
            "last_completed_at": completed_at,
            "next_due_at": next_due_at,
            "lease_owner": None,
            "lease_expires_at": None,
            "updated_at": utc_now(),
        }
        if summary_date is not None:
            values["last_summary_date"] = summary_date
        if cleanup_date is not None:
            values["last_cleanup_date"] = cleanup_date
        result = self.session.execute(
            update(SchedulerStateRow)
            .where(
                SchedulerStateRow.workspace_id == self.workspace_id,
                SchedulerStateRow.job_name == job_name,
                SchedulerStateRow.lease_owner == owner,
            )
            .values(**values)
        )
        return result.rowcount == 1

    def release(self, job_name: str, *, owner: str) -> bool:
        result = self.session.execute(
            update(SchedulerStateRow)
            .where(
                SchedulerStateRow.workspace_id == self.workspace_id,
                SchedulerStateRow.job_name == job_name,
                SchedulerStateRow.lease_owner == owner,
            )
            .values(lease_owner=None, lease_expires_at=None, updated_at=utc_now())
        )
        return result.rowcount == 1


class TaskQueueRepository:
    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def enqueue(
        self,
        *,
        scope: str,
        key: str,
        payload_sha256: str,
        task_id: str,
        task_type: str,
        payload: dict[str, Any],
        not_before: datetime,
    ) -> bool:
        idempotency_key = f"{scope}:{key}"
        existing = self.session.scalar(
            select(ScheduledTaskRow).where(
                ScheduledTaskRow.workspace_id == self.workspace_id,
                ScheduledTaskRow.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.payload_sha256 != payload_sha256 or existing.task_id != task_id:
                raise IdempotencyConflict("task key was reused with different payload")
            return False
        row = ScheduledTaskRow(
            task_id=task_id,
            workspace_id=self.workspace_id,
            task_type=task_type,
            status="PENDING",
            idempotency_key=idempotency_key,
            payload_sha256=payload_sha256,
            payload=payload,
            not_before=not_before,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            return False
        return True

    def list_due(self, task_type: str, *, now: datetime) -> tuple[ScheduledTaskRow, ...]:
        return tuple(
            self.session.scalars(
                select(ScheduledTaskRow)
                .where(
                    ScheduledTaskRow.workspace_id == self.workspace_id,
                    ScheduledTaskRow.task_type == task_type,
                    ScheduledTaskRow.status == "PENDING",
                    ScheduledTaskRow.not_before <= now,
                )
                .order_by(ScheduledTaskRow.not_before, ScheduledTaskRow.task_id)
            ).all()
        )

    def mark_succeeded(
        self,
        task_id: str,
        *,
        finished_at: datetime,
        result: dict[str, Any] | None = None,
    ) -> bool:
        row = self.session.scalar(
            select(ScheduledTaskRow).where(
                ScheduledTaskRow.workspace_id == self.workspace_id,
                ScheduledTaskRow.task_id == task_id,
                ScheduledTaskRow.status == "PENDING",
            )
        )
        if row is None:
            return False
        row.status = "SUCCEEDED"
        row.finished_at = finished_at
        if result is not None:
            row.payload = {**row.payload, "result": result}
        row.updated_at = finished_at
        self.session.flush()
        return True

    def mark_failed(
        self,
        task_id: str,
        *,
        finished_at: datetime,
        result: dict[str, Any],
        retryable: bool = True,
    ) -> bool:
        row = self.session.scalar(
            select(ScheduledTaskRow).where(
                ScheduledTaskRow.workspace_id == self.workspace_id,
                ScheduledTaskRow.task_id == task_id,
                ScheduledTaskRow.status == "PENDING",
            )
        )
        if row is None:
            return False
        row.status = "RETRYABLE_FAILED" if retryable else "PERMANENT_FAILED"
        row.finished_at = finished_at
        row.payload = {**row.payload, "result": result}
        row.updated_at = finished_at
        self.session.flush()
        return True


class CrawlRunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_if_absent(self, value: CrawlRunInput) -> tuple[CrawlRunRow, bool]:
        existing = self.session.scalar(
            select(CrawlRunRow).where(
                CrawlRunRow.workspace_id == value.workspace_id,
                CrawlRunRow.idempotency_key == value.idempotency_key,
            )
        )
        if existing is not None:
            self._assert_same_operation(existing, value)
            return existing, False

        row = CrawlRunRow(
            crawl_run_id=value.crawl_run_id,
            workspace_id=value.workspace_id,
            source_id=value.source_id,
            idempotency_key=value.idempotency_key,
            status=value.status,
            scheduled_at=value.scheduled_at,
            schema_version="1.0",
            payload=value.payload,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            existing = self.session.scalar(
                select(CrawlRunRow).where(
                    CrawlRunRow.workspace_id == value.workspace_id,
                    CrawlRunRow.idempotency_key == value.idempotency_key,
                )
            )
            if existing is None:
                raise
            self._assert_same_operation(existing, value)
            return existing, False
        return row, True

    def count_since(self, *, workspace_id: str, source_id: str, since: datetime) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(CrawlRunRow)
                .where(
                    CrawlRunRow.workspace_id == workspace_id,
                    CrawlRunRow.source_id == source_id,
                    CrawlRunRow.scheduled_at >= since,
                )
            )
            or 0
        )

    def mark_fetch_failure(
        self,
        *,
        workspace_id: str,
        crawl_run_id: str,
        failure: FetchFailure,
    ) -> bool:
        row = self.session.scalar(
            select(CrawlRunRow).where(
                CrawlRunRow.workspace_id == workspace_id,
                CrawlRunRow.crawl_run_id == crawl_run_id,
                CrawlRunRow.source_id == failure.source_id,
            )
        )
        if row is None:
            return False
        row.status = "RETRYABLE_FAILED" if failure.retryable else "PERMANENT_FAILED"
        row.finished_at = failure.occurred_at
        row.payload = {
            **row.payload,
            "failure": {
                "error_code": failure.error_code.value,
                "retryable": failure.retryable,
            },
        }
        row.updated_at = failure.occurred_at
        self.session.flush()
        return True

    def mark_succeeded(
        self,
        *,
        workspace_id: str,
        crawl_run_id: str,
        finished_at: datetime,
        summary: dict[str, Any],
    ) -> bool:
        row = self.session.scalar(
            select(CrawlRunRow).where(
                CrawlRunRow.workspace_id == workspace_id,
                CrawlRunRow.crawl_run_id == crawl_run_id,
            )
        )
        if row is None:
            return False
        row.status = "SUCCEEDED"
        row.finished_at = finished_at
        row.payload = {**row.payload, "result": summary}
        row.updated_at = finished_at
        self.session.flush()
        return True

    @staticmethod
    def _assert_same_operation(existing: CrawlRunRow, value: CrawlRunInput) -> None:
        if existing.source_id != value.source_id or existing.payload != value.payload:
            raise IdempotencyConflict(
                "crawl idempotency key was reused with different source or payload"
            )


class RawDocumentRepository:
    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def add_if_absent(self, document: RawDocument) -> tuple[RawDocumentRow, bool]:
        existing = self.session.scalar(
            select(RawDocumentRow).where(
                RawDocumentRow.workspace_id == self.workspace_id,
                RawDocumentRow.content_hash == document.control.content_sha256,
            )
        )
        if existing is not None:
            return existing, False

        row = RawDocumentRow(
            document_id=document.control.document_id,
            workspace_id=self.workspace_id,
            source_id=document.control.source_id,
            source_url=str(document.external.source_url),
            title=document.external.title,
            raw_body=document.external.body,
            content_hash=document.control.content_sha256,
            schema_version=document.schema_version,
            state=document.control.state.value,
            state_version=document.control.state_version,
            published_at=document.external.published_at,
            fetched_at=document.control.fetched_at or document.control.discovered_at,
            metadata_payload={
                "author": document.external.author,
                "language": document.external.language,
                **document.external.metadata,
            },
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            existing = self.session.scalar(
                select(RawDocumentRow).where(
                    RawDocumentRow.workspace_id == self.workspace_id,
                    RawDocumentRow.content_hash == document.control.content_sha256,
                )
            )
            if existing is None:
                raise
            return existing, False
        return row, True

    def get(self, document_id: str) -> RawDocumentRow | None:
        return self.session.scalar(
            select(RawDocumentRow).where(
                RawDocumentRow.workspace_id == self.workspace_id,
                RawDocumentRow.document_id == document_id,
            )
        )

    def count_since(self, *, source_id: str, since: datetime) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(RawDocumentRow)
                .where(
                    RawDocumentRow.workspace_id == self.workspace_id,
                    RawDocumentRow.source_id == source_id,
                    RawDocumentRow.fetched_at >= since,
                )
            )
            or 0
        )

    def apply_transition(self, transition: StateTransitionRecord) -> bool:
        if transition.outcome is TransitionOutcome.REJECTED:
            raise ValueError("rejected transitions cannot be persisted as document state")
        if transition.outcome is TransitionOutcome.NOOP:
            return False

        statement = (
            update(RawDocumentRow)
            .where(
                RawDocumentRow.workspace_id == self.workspace_id,
                RawDocumentRow.document_id == transition.document_id,
                RawDocumentRow.state == transition.from_state.value,
                RawDocumentRow.state_version == transition.previous_version,
            )
            .values(
                state=transition.requested_state.value,
                state_version=transition.next_version,
                updated_at=transition.occurred_at,
            )
        )
        result = self.session.execute(statement)
        if result.rowcount != 1:
            raise ConcurrentStateChange("document state or version changed before persistence")
        return True


class AuditRepository:
    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def record(
        self,
        *,
        event_type: str,
        actor: str,
        target_type: str,
        target_id: str,
        outcome: str,
        details: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> AuditEventRow:
        safe_details = details or {}
        _assert_safe_audit_details(safe_details)
        try:
            serialized_details = json.dumps(safe_details, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise AuditDetailRejected("audit details must be JSON serializable") from error
        if len(serialized_details.encode("utf-8")) > 20_000:
            raise AuditDetailRejected("audit details exceed the 20 KB limit")

        event_time = occurred_at or utc_now()
        stable_source = (
            f"{self.workspace_id}:{event_type}:{target_type}:{target_id}:{event_time.isoformat()}"
        )
        row = AuditEventRow(
            audit_event_id=str(uuid5(NAMESPACE_URL, stable_source)),
            workspace_id=self.workspace_id,
            event_type=event_type,
            actor=actor,
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            occurred_at=event_time,
            details=safe_details,
        )
        self.session.add(row)
        self.session.flush()
        return row
