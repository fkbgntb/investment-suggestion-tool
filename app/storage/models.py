"""SQLAlchemy persistence models; domain models remain framework-independent."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Persist UTC consistently and restore timezone awareness on SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime values must include a timezone")
        normalized = value.astimezone(UTC)
        if dialect.name == "sqlite":
            return normalized.replace(tzinfo=None)
        return normalized

    def process_result_value(self, value: datetime | None, _: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class WorkspaceScopedMixin:
    workspace_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


class SnapshotMixin(TimestampMixin, WorkspaceScopedMixin):
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class WorkspaceRow(TimestampMixin, Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "raw_document_retention_days BETWEEN 1 AND 3650",
            name="retention_days_range",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    raw_document_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=90, server_default="90"
    )
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class InvestmentProfileRow(SnapshotMixin, Base):
    __tablename__ = "investment_profiles"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "profile_id", name="uq_investment_profiles_workspace_profile"
        ),
    )

    profile_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)


class AssetRow(SnapshotMixin, Base):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "asset_id", name="uq_assets_workspace_asset"),
        UniqueConstraint("workspace_id", "fund_code", name="uq_assets_workspace_fund_code"),
    )

    asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    fund_code: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)


class PositionRow(SnapshotMixin, Base):
    __tablename__ = "positions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "profile_id"],
            ["investment_profiles.workspace_id", "investment_profiles.profile_id"],
            ondelete="CASCADE",
            name="fk_positions_workspace_profile",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "asset_id"],
            ["assets.workspace_id", "assets.asset_id"],
            ondelete="CASCADE",
            name="fk_positions_workspace_asset",
        ),
        UniqueConstraint(
            "workspace_id",
            "position_id",
            name="uq_positions_workspace_position",
        ),
        UniqueConstraint(
            "workspace_id",
            "profile_id",
            "asset_id",
            "snapshot_at",
            name="uq_positions_workspace_profile_asset_snapshot",
        ),
    )

    position_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(128))
    asset_id: Mapped[str] = mapped_column(String(128))
    snapshot_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class PositionSnapshotRow(WorkspaceScopedMixin, Base):
    __tablename__ = "position_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "snapshot_id",
            name="uq_position_snapshots_workspace_snapshot",
        ),
        Index(
            "ix_position_snapshots_workspace_position_generated",
            "workspace_id",
            "position_id",
            "generated_at",
        ),
    )

    snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    position_id: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class ImmutableSnapshotError(RuntimeError):
    """Position analysis snapshots are append-only application records."""


@event.listens_for(Session, "before_flush")
def _protect_position_snapshots(
    session: Session, _flush_context: object, _instances: object
) -> None:
    if any(isinstance(row, PositionSnapshotRow) for row in session.dirty):
        raise ImmutableSnapshotError("position analysis snapshots cannot be updated")
    if any(isinstance(row, PositionSnapshotRow) for row in session.deleted):
        raise ImmutableSnapshotError("position analysis snapshots cannot be deleted directly")


class TopicRow(SnapshotMixin, Base):
    __tablename__ = "topics"
    __table_args__ = (
        UniqueConstraint("workspace_id", "topic_id", name="uq_topics_workspace_topic"),
        UniqueConstraint(
            "workspace_id", "name", "config_version", name="uq_topics_workspace_name_version"
        ),
    )

    topic_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    config_version: Mapped[str] = mapped_column(String(64), nullable=False)


class EntityRow(SnapshotMixin, Base):
    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("workspace_id", "entity_id", name="uq_entities_workspace_entity"),
        UniqueConstraint(
            "workspace_id", "name", "entity_type", name="uq_entities_workspace_name_type"
        ),
    )

    entity_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)


class ExposureRow(SnapshotMixin, Base):
    __tablename__ = "exposures"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "asset_id"],
            ["assets.workspace_id", "assets.asset_id"],
            ondelete="CASCADE",
            name="fk_exposures_workspace_asset",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "topic_id"],
            ["topics.workspace_id", "topics.topic_id"],
            ondelete="CASCADE",
            name="fk_exposures_workspace_topic",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "entity_id"],
            ["entities.workspace_id", "entities.entity_id"],
            ondelete="CASCADE",
            name="fk_exposures_workspace_entity",
        ),
        UniqueConstraint(
            "workspace_id",
            "asset_id",
            "topic_id",
            "entity_id",
            "kind",
            "config_version",
            name="uq_exposures_workspace_mapping_version",
        ),
    )

    exposure_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(128))
    topic_id: Mapped[str] = mapped_column(String(128))
    entity_id: Mapped[str | None] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    config_version: Mapped[str] = mapped_column(String(64), nullable=False)


class SourceRow(SnapshotMixin, Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("workspace_id", "source_id", name="uq_sources_workspace_source"),
        UniqueConstraint("workspace_id", "base_url", name="uq_sources_workspace_base_url"),
    )

    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    adapter_name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CrawlRunRow(SnapshotMixin, Base):
    __tablename__ = "crawl_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["sources.workspace_id", "sources.source_id"],
            ondelete="CASCADE",
            name="fk_crawl_runs_workspace_source",
        ),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_crawl_runs_workspace_idempotency"
        ),
    )

    crawl_run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class RawDocumentRow(TimestampMixin, WorkspaceScopedMixin, Base):
    __tablename__ = "raw_documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["sources.workspace_id", "sources.source_id"],
            ondelete="CASCADE",
            name="fk_raw_documents_workspace_source",
        ),
        UniqueConstraint("workspace_id", "document_id", name="uq_raw_documents_workspace_document"),
        UniqueConstraint(
            "workspace_id", "content_hash", name="uq_raw_documents_workspace_content_hash"
        ),
        CheckConstraint("state_version >= 0", name="state_version_nonnegative"),
        Index("ix_raw_documents_workspace_state", "workspace_id", "state"),
    )

    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128))
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    raw_body: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0")
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    body_purged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    metadata_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class EventClusterRow(SnapshotMixin, Base):
    __tablename__ = "event_clusters"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "canonical_document_id"],
            ["raw_documents.workspace_id", "raw_documents.document_id"],
            ondelete="CASCADE",
            name="fk_event_clusters_workspace_document",
        ),
        UniqueConstraint("workspace_id", "cluster_id", name="uq_event_clusters_workspace_cluster"),
        UniqueConstraint("workspace_id", "event_key", name="uq_event_clusters_workspace_event_key"),
    )

    cluster_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_key: Mapped[str] = mapped_column(String(300), nullable=False)
    canonical_document_id: Mapped[str] = mapped_column(String(128))


class EvidenceItemRow(SnapshotMixin, Base):
    __tablename__ = "evidence_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "document_id"],
            ["raw_documents.workspace_id", "raw_documents.document_id"],
            ondelete="CASCADE",
            name="fk_evidence_items_workspace_document",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "cluster_id"],
            ["event_clusters.workspace_id", "event_clusters.cluster_id"],
            ondelete="CASCADE",
            name="fk_evidence_items_workspace_cluster",
        ),
        UniqueConstraint(
            "workspace_id", "evidence_id", name="uq_evidence_items_workspace_evidence"
        ),
    )

    evidence_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(128))
    cluster_id: Mapped[str | None] = mapped_column(String(128))


class EvidenceScoreRow(SnapshotMixin, Base):
    __tablename__ = "evidence_scores"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "evidence_id"],
            ["evidence_items.workspace_id", "evidence_items.evidence_id"],
            ondelete="CASCADE",
            name="fk_evidence_scores_workspace_evidence",
        ),
        UniqueConstraint(
            "workspace_id",
            "evidence_id",
            "scoring_version",
            name="uq_evidence_scores_workspace_evidence_version",
        ),
    )

    score_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(128))
    scoring_version: Mapped[str] = mapped_column(String(120), nullable=False)


class AnalysisRunRow(SnapshotMixin, Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "asset_id"],
            ["assets.workspace_id", "assets.asset_id"],
            ondelete="CASCADE",
            name="fk_analysis_runs_workspace_asset",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "position_snapshot_id"],
            ["position_snapshots.workspace_id", "position_snapshots.snapshot_id"],
            ondelete="RESTRICT",
            name="fk_analysis_runs_workspace_position_snapshot",
        ),
        UniqueConstraint("workspace_id", "analysis_run_id", name="uq_analysis_runs_workspace_run"),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_analysis_runs_workspace_idempotency"
        ),
    )

    analysis_run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(128))
    position_snapshot_id: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(120), nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class DecisionResultRow(SnapshotMixin, Base):
    __tablename__ = "decision_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "analysis_run_id"],
            ["analysis_runs.workspace_id", "analysis_runs.analysis_run_id"],
            ondelete="CASCADE",
            name="fk_decision_results_workspace_analysis",
        ),
    )

    decision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    analysis_run_id: Mapped[str] = mapped_column(String(128))
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(120), nullable=False)


class ReportRow(SnapshotMixin, Base):
    __tablename__ = "reports"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "analysis_run_id"],
            ["analysis_runs.workspace_id", "analysis_runs.analysis_run_id"],
            ondelete="CASCADE",
            name="fk_reports_workspace_analysis",
        ),
    )

    report_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    analysis_run_id: Mapped[str] = mapped_column(String(128))
    pipeline_version: Mapped[str] = mapped_column(String(120), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(120), nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class AuditEventRow(WorkspaceScopedMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_workspace_occurred", "workspace_id", "occurred_at"),)

    audit_event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(128), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class IdempotencyRecordRow(WorkspaceScopedMixin, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("workspace_id", "scope", "key", name="uq_idempotency_workspace_scope_key"),
    )

    idempotency_record_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    record_type: Mapped[str] = mapped_column(String(128), nullable=False)
    record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
