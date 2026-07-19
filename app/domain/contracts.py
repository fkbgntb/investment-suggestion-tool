"""Framework-independent request and response contracts for replaceable modules."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import AnyHttpUrl, AwareDatetime, Field, JsonValue, model_validator

from app.domain.analysis import AnalysisResult, DecisionContext, DecisionResult, Report
from app.domain.base import DomainModel, IdempotencyKey, Identifier, Sha256
from app.domain.documents import DiscoveredDocument, ExternalDocumentContent
from app.domain.enums import ReportFormat
from app.domain.evidence import EvidenceExtractionRequest, EvidenceExtractionResult
from app.domain.portfolio import Asset, InvestmentProfile, MarketSnapshot, Position


class SourceDiscoveryRequest(DomainModel):
    source_id: Identifier
    topic_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=50)
    since: AwareDatetime
    until: AwareDatetime
    cursor: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_window(self) -> SourceDiscoveryRequest:
        if self.since > self.until:
            raise ValueError("source discovery start cannot follow end")
        return self


class SourceDiscoveryResult(DomainModel):
    documents: tuple[DiscoveredDocument, ...] = Field(default_factory=tuple, max_length=10_000)
    next_cursor: str | None = Field(default=None, max_length=1000)


class SourceFetchRequest(DomainModel):
    source_id: Identifier
    source_url: AnyHttpUrl
    idempotency: IdempotencyKey


class SourceFetchResult(DomainModel):
    """Adapter output deliberately excludes system-owned state and persistence fields."""

    source_id: Identifier
    content: ExternalDocumentContent
    content_sha256: Sha256
    fetched_at: AwareDatetime


class MarketDataRequest(DomainModel):
    asset: Asset
    as_of: AwareDatetime


class AnalysisRequest(DomainModel):
    context: DecisionContext
    prompt_version: str = Field(min_length=1, max_length=120)


class ReportRenderRequest(DomainModel):
    report: Report
    output_format: ReportFormat


class RenderedReport(DomainModel):
    report_id: Identifier
    output_format: ReportFormat
    media_type: str = Field(min_length=3, max_length=100)
    content: bytes = Field(min_length=1, max_length=10_000_000)
    content_sha256: Sha256


class NotificationMessage(DomainModel):
    notification_id: Identifier
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20_000)
    report_id: Identifier | None = None


class DeliveryReceipt(DomainModel):
    notification_id: Identifier
    provider_message_id: str | None = Field(default=None, max_length=300)
    accepted: bool
    accepted_at: AwareDatetime


class TaskRequest(DomainModel):
    task_id: Identifier
    task_type: Identifier
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    idempotency: IdempotencyKey
    not_before: AwareDatetime


class DispatchReceipt(DomainModel):
    task_id: Identifier
    accepted: bool
    duplicate: bool
    dispatch_reference: str | None = Field(default=None, max_length=300)


class StorageWriteRequest(DomainModel):
    """Typed envelope; storage interprets data, never executable commands."""

    record_type: Identifier
    record_id: Identifier
    payload: dict[str, JsonValue]
    idempotency: IdempotencyKey


class StorageWriteResult(DomainModel):
    record_type: Identifier
    record_id: Identifier
    created: bool
    duplicate: bool


class StorageRecord(DomainModel):
    record_type: Identifier
    record_id: Identifier
    payload: dict[str, JsonValue]


class PortfolioImportRequest(DomainModel):
    """Local-only input for a future CSV or OCR adapter."""

    source_format: Literal["CSV", "OCR"]
    filename: str = Field(min_length=1, max_length=255)
    content: bytes = Field(min_length=1, max_length=10_000_000)
    content_sha256: Sha256


class PortfolioImportResult(DomainModel):
    profiles: tuple[InvestmentProfile, ...] = Field(default_factory=tuple, max_length=100)
    assets: tuple[Asset, ...] = Field(default_factory=tuple, max_length=10_000)
    positions: tuple[Position, ...] = Field(default_factory=tuple, max_length=10_000)
    warnings: tuple[str, ...] = Field(default_factory=tuple, max_length=1_000)


@runtime_checkable
class SourceAdapter(Protocol):
    adapter_name: str

    async def discover(self, request: SourceDiscoveryRequest) -> SourceDiscoveryResult: ...

    async def fetch(self, request: SourceFetchRequest) -> SourceFetchResult: ...


@runtime_checkable
class MarketDataProvider(Protocol):
    provider_name: str

    async def get_snapshot(self, request: MarketDataRequest) -> MarketSnapshot: ...


@runtime_checkable
class AIProvider(Protocol):
    provider_name: str

    async def extract(self, request: EvidenceExtractionRequest) -> EvidenceExtractionResult: ...

    async def synthesize(self, request: AnalysisRequest) -> AnalysisResult: ...


@runtime_checkable
class DecisionPolicy(Protocol):
    policy_name: str

    def evaluate(self, context: DecisionContext) -> DecisionResult: ...


@runtime_checkable
class ReportRenderer(Protocol):
    renderer_name: str

    async def render(self, request: ReportRenderRequest) -> RenderedReport: ...


@runtime_checkable
class NotificationProvider(Protocol):
    provider_name: str

    async def send(self, message: NotificationMessage) -> DeliveryReceipt: ...


@runtime_checkable
class TaskDispatcher(Protocol):
    dispatcher_name: str

    async def enqueue(self, request: TaskRequest) -> DispatchReceipt: ...


@runtime_checkable
class StorageProvider(Protocol):
    provider_name: str

    async def save_if_absent(self, request: StorageWriteRequest) -> StorageWriteResult: ...

    async def get(self, record_type: Identifier, record_id: Identifier) -> StorageRecord | None: ...


@runtime_checkable
class PortfolioImportAdapter(Protocol):
    adapter_name: str

    async def parse(self, request: PortfolioImportRequest) -> PortfolioImportResult: ...
