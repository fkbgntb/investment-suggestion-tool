"""Trusted local publication and activation of versioned taxonomy data."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.taxonomy import TaxonomyConfiguration
from app.storage.repositories import (
    AuditRepository,
    PortfolioRepository,
    TaxonomyRepository,
    WorkspaceRepository,
)


class TaxonomyNotFound(LookupError):
    """A requested taxonomy version does not exist in the local workspace."""


class TaxonomyConflict(ValueError):
    """A taxonomy publication conflicts with persisted state or references."""


class TaxonomyService:
    """Manage data-only taxonomy versions without accepting external-document writes."""

    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id
        workspaces = WorkspaceRepository(session)
        if not workspaces.exists(workspace_id):
            workspaces.create(workspace_id, "Personal investment analysis")
        self.repository = TaxonomyRepository(session, workspace_id)
        self.portfolio = PortfolioRepository(session, workspace_id)
        self.audit = AuditRepository(session, workspace_id)

    def publish(self, configuration: TaxonomyConfiguration) -> TaxonomyConfiguration:
        existing_by_version = self.repository.get_by_version(configuration.config_version)
        existing_by_id = self.repository.get_by_id(configuration.configuration_id)
        existing = existing_by_version or existing_by_id
        if existing is not None:
            if existing != configuration:
                raise TaxonomyConflict(
                    "configuration ID or version already exists with different data"
                )
            self._activate_if_needed(existing, event_type="taxonomy_configuration_reactivated")
            return existing

        active = self.repository.get_active()
        if active is None:
            if configuration.based_on_version is not None:
                raise TaxonomyConflict("the first configuration cannot declare a base version")
        elif configuration.based_on_version != active.config_version:
            raise TaxonomyConflict(
                "new configuration must be based on the currently active version"
            )

        now = datetime.now(UTC)
        if configuration.created_at > now:
            raise TaxonomyConflict("configuration creation time cannot be in the future")
        for asset_id in {exposure.asset_id for exposure in configuration.exposures}:
            if self.portfolio.get_asset(asset_id) is None:
                raise TaxonomyConflict("configuration exposure references an unknown asset")

        try:
            with self.session.begin_nested():
                self.repository.add_configuration(configuration)
                self.repository.activate(configuration)
        except IntegrityError as error:
            raise TaxonomyConflict("configuration ID or version already exists") from error

        self._audit(
            "taxonomy_configuration_published",
            configuration,
            previous_version=active.config_version if active is not None else None,
        )
        return configuration

    def activate(self, config_version: str) -> TaxonomyConfiguration:
        configuration = self.get(config_version)
        self._activate_if_needed(configuration, event_type="taxonomy_configuration_activated")
        return configuration

    def get(self, config_version: str) -> TaxonomyConfiguration:
        configuration = self.repository.get_by_version(config_version)
        if configuration is None:
            raise TaxonomyNotFound("taxonomy configuration version was not found")
        return configuration

    def get_active(self) -> TaxonomyConfiguration:
        configuration = self.repository.get_active()
        if configuration is None:
            raise TaxonomyNotFound("no active taxonomy configuration exists")
        return configuration

    def list_configurations(self) -> tuple[TaxonomyConfiguration, ...]:
        return self.repository.list_configurations()

    def _activate_if_needed(self, configuration: TaxonomyConfiguration, *, event_type: str) -> None:
        active = self.repository.get_active()
        if active is not None and active.config_version == configuration.config_version:
            return
        self.repository.activate(configuration)
        self._audit(
            event_type,
            configuration,
            previous_version=active.config_version if active is not None else None,
        )

    def _audit(
        self,
        event_type: str,
        configuration: TaxonomyConfiguration,
        *,
        previous_version: str | None,
    ) -> None:
        self.audit.record(
            event_type=event_type,
            actor="local_user",
            target_type="taxonomy_configuration",
            target_id=configuration.configuration_id,
            outcome="completed",
            details={
                "previous_version": previous_version,
                "new_version": configuration.config_version,
                "topic_count": len(configuration.topics),
                "entity_count": len(configuration.entities),
                "relation_count": len(configuration.influence_relations),
                "exposure_count": len(configuration.exposures),
                "source": "local_api",
            },
        )
