"""Local source registry with audited, data-only configuration changes."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.collectors.registry import AdapterNotRegistered, AdapterRegistry
from app.domain.collection import SourceAdapterState, SourceHealthSnapshot, URLPolicy
from app.domain.enums import SourceHealthStatus
from app.domain.taxonomy import Source
from app.storage.repositories import (
    AuditRepository,
    ConcurrentStateChange,
    SourceRepository,
    WorkspaceRepository,
)


class SourceNotFound(LookupError):
    pass


class SourceConflict(ValueError):
    pass


class SourceService:
    def __init__(
        self,
        session: Session,
        workspace_id: str,
        adapter_registry: AdapterRegistry,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.adapter_registry = adapter_registry
        workspaces = WorkspaceRepository(session)
        if not workspaces.exists(workspace_id):
            workspaces.create(workspace_id, "Personal investment analysis")
        self.repository = SourceRepository(session, workspace_id)
        self.audit = AuditRepository(session, workspace_id)

    def create(self, source: Source) -> Source:
        self._validate_configuration(source)
        existing = self.repository.get(source.source_id)
        if existing is not None:
            if existing == source:
                return existing
            raise SourceConflict("source ID already exists with different configuration")
        try:
            with self.session.begin_nested():
                self.repository.add(source)
                self.repository.save_health(self._initial_health(source))
        except IntegrityError as error:
            raise SourceConflict("source ID or base URL already exists") from error
        self._audit("source_created", source, previous=None)
        return source

    def get(self, source_id: str) -> Source:
        source = self.repository.get(source_id)
        if source is None:
            raise SourceNotFound("source was not found")
        return source

    def list(self) -> tuple[Source, ...]:
        return self.repository.list()

    def list_schedulable(self) -> tuple[Source, ...]:
        """Only this method may feed the scheduler; disabled sources are excluded."""

        return self.repository.list(enabled_only=True)

    def update(self, source_id: str, replacement: Source) -> Source:
        if source_id != replacement.source_id:
            raise SourceConflict("source ID cannot be changed")
        current = self.get(source_id)
        self._validate_configuration(replacement)
        state = self.repository.get_adapter_state(source_id)
        if state is not None and current.adapter_name != replacement.adapter_name:
            raise SourceConflict("adapter cannot change after cursor state has been created")
        if current == replacement:
            return current
        try:
            updated = self.repository.update(replacement)
        except IntegrityError as error:
            raise SourceConflict("source base URL already exists") from error
        if not updated:
            raise SourceNotFound("source was not found")
        if current.enabled != replacement.enabled:
            health = self.repository.get_health(source_id) or self._initial_health(current)
            self.repository.save_health(
                health.model_copy(
                    update={
                        "status": (
                            SourceHealthStatus.UNKNOWN
                            if replacement.enabled
                            else SourceHealthStatus.DISABLED
                        )
                    }
                )
            )
        self._audit("source_updated", replacement, previous=current)
        return replacement

    def disable(self, source_id: str) -> Source:
        current = self.get(source_id)
        if not current.enabled:
            return current
        disabled = current.model_copy(update={"enabled": False})
        self.repository.update(disabled)
        health = self.repository.get_health(source_id) or self._initial_health(current)
        self.repository.save_health(
            health.model_copy(update={"status": SourceHealthStatus.DISABLED})
        )
        self._audit("source_disabled", disabled, previous=current)
        return disabled

    def health(self, source_id: str) -> SourceHealthSnapshot:
        source = self.get(source_id)
        snapshot = self.repository.get_health(source_id)
        if snapshot is None:
            snapshot = self._initial_health(source)
            self.repository.save_health(snapshot)
        return snapshot

    def record_health(self, snapshot: SourceHealthSnapshot) -> SourceHealthSnapshot:
        source = self.get(snapshot.source_id)
        persisted = (
            snapshot
            if source.enabled
            else snapshot.model_copy(update={"status": SourceHealthStatus.DISABLED})
        )
        self.repository.save_health(persisted)
        return persisted

    def adapter_state(self, source_id: str) -> SourceAdapterState | None:
        self.get(source_id)
        return self.repository.get_adapter_state(source_id)

    def advance_cursor(
        self,
        source_id: str,
        *,
        adapter_version: str,
        cursor: str | None,
        expected_version: int,
        occurred_at: datetime | None = None,
    ) -> SourceAdapterState:
        source = self.get(source_id)
        self.adapter_registry.require(source.adapter_name)
        if not source.enabled:
            raise SourceConflict("disabled sources cannot advance adapter state")
        state = SourceAdapterState(
            source_id=source_id,
            adapter_name=source.adapter_name,
            adapter_version=adapter_version,
            state_version=expected_version + 1,
            cursor=cursor,
            updated_at=occurred_at or datetime.now(UTC),
        )
        try:
            self.repository.save_adapter_state(state, expected_version=expected_version)
        except ConcurrentStateChange as error:
            raise SourceConflict(str(error)) from error
        return state

    def _validate_configuration(self, source: Source) -> None:
        try:
            self.adapter_registry.require(source.adapter_name)
        except AdapterNotRegistered as error:
            raise SourceConflict(str(error)) from error
        URLPolicy(
            source_id=source.source_id,
            allowed_hosts=source.allowed_domains,
        )

    @staticmethod
    def _initial_health(source: Source) -> SourceHealthSnapshot:
        return SourceHealthSnapshot(
            source_id=source.source_id,
            status=(SourceHealthStatus.UNKNOWN if source.enabled else SourceHealthStatus.DISABLED),
            consecutive_failures=0,
        )

    def _audit(self, event_type: str, source: Source, *, previous: Source | None) -> None:
        self.audit.record(
            event_type=event_type,
            actor="local_user",
            target_type="source",
            target_id=source.source_id,
            outcome="completed",
            details={
                "adapter_name": source.adapter_name,
                "enabled": source.enabled,
                "previous_enabled": previous.enabled if previous is not None else None,
                "trust_tier": source.trust_tier.value,
                "previous_trust_tier": (
                    previous.trust_tier.value if previous is not None else None
                ),
                "config_version": source.config_version,
                "source": "local_api",
            },
        )
