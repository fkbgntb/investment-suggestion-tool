"""Untrusted document content and trusted pipeline control metadata."""

from __future__ import annotations

from pydantic import AnyHttpUrl, AwareDatetime, Field, JsonValue, model_validator

from app.domain.base import DomainModel, IdempotencyKey, Identifier, Sha256
from app.domain.enums import DocumentState


class ExternalDocumentContent(DomainModel):
    """Untrusted source-controlled fields; never interpreted as system instructions."""

    source_url: AnyHttpUrl
    title: str = Field(min_length=1, max_length=1000)
    body: str = Field(min_length=1, max_length=2_000_000)
    published_at: AwareDatetime | None = None
    author: str | None = Field(default=None, max_length=300)
    language: str = Field(min_length=2, max_length=16, pattern=r"^[A-Za-z-]+$")
    metadata: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)


class RawDocumentControl(DomainModel):
    """System-owned fields kept separate from untrusted external content."""

    document_id: Identifier
    source_id: Identifier
    state: DocumentState
    state_version: int = Field(ge=0)
    content_sha256: Sha256
    idempotency: IdempotencyKey
    discovered_at: AwareDatetime
    fetched_at: AwareDatetime | None = None
    failure_count: int = Field(default=0, ge=0, le=100)
    last_error_code: str | None = Field(default=None, max_length=120)


class RawDocument(DomainModel):
    """Aggregate whose shape makes the trust boundary explicit."""

    external: ExternalDocumentContent
    control: RawDocumentControl


class DiscoveredDocument(DomainModel):
    source_id: Identifier
    source_url: AnyHttpUrl
    discovered_at: AwareDatetime
    external_reference: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=1000)
    summary: str | None = Field(default=None, max_length=20_000)
    publisher: str | None = Field(default=None, max_length=300)
    language: str | None = Field(default=None, max_length=32)
    published_at: AwareDatetime | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)


class EventCluster(DomainModel):
    cluster_id: Identifier
    topic_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=50)
    document_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=1000)
    canonical_document_id: Identifier
    event_key: str = Field(min_length=1, max_length=300)
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime

    @model_validator(mode="after")
    def validate_cluster(self) -> EventCluster:
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("document ids in a cluster must be unique")
        if self.canonical_document_id not in self.document_ids:
            raise ValueError("canonical document must belong to the cluster")
        if self.first_seen_at > self.last_seen_at:
            raise ValueError("cluster first seen time cannot follow last seen time")
        return self
