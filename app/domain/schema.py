"""Single versioned JSON Schema catalog for cross-module contracts."""

from __future__ import annotations

import json
from pathlib import Path

from app.domain.analysis import AnalysisResult, DecisionContext, DecisionResult, Report
from app.domain.base import DomainModel, IdempotencyKey, Money, MoneyRange
from app.domain.collection import FetchFailure, SourceHealthSnapshot, URLPolicy
from app.domain.contracts import (
    AnalysisRequest,
    DeliveryReceipt,
    DispatchReceipt,
    MarketDataRequest,
    NotificationMessage,
    PortfolioImportRequest,
    PortfolioImportResult,
    RenderedReport,
    ReportRenderRequest,
    SourceDiscoveryRequest,
    SourceDiscoveryResult,
    SourceFetchRequest,
    SourceFetchResult,
    StorageRecord,
    StorageWriteRequest,
    StorageWriteResult,
    TaskRequest,
)
from app.domain.documents import (
    DiscoveredDocument,
    EventCluster,
    ExternalDocumentContent,
    RawDocument,
    RawDocumentControl,
)
from app.domain.evidence import (
    Evidence,
    EvidenceDraft,
    EvidenceExtractionRequest,
    EvidenceExtractionResult,
    EvidenceScore,
)
from app.domain.jobs import JobRun
from app.domain.portfolio import (
    Asset,
    FundFeePolicy,
    InvestmentProfile,
    MarketSnapshot,
    PortfolioAIRiskSummary,
    Position,
    PositionAnalysisSnapshot,
    PurchaseLot,
    RedemptionFeeTier,
)
from app.domain.state_machine import StateTransitionRecord
from app.domain.taxonomy import (
    Entity,
    Exposure,
    InfluenceRelation,
    Source,
    TaxonomyConfiguration,
    Topic,
)


class DomainContractBundle(DomainModel):
    """Schema-only catalog whose definitions are the supported public contracts."""

    money: Money
    money_range: MoneyRange
    idempotency_key: IdempotencyKey
    url_policy: URLPolicy
    fetch_failure: FetchFailure
    source_health_snapshot: SourceHealthSnapshot
    investment_profile: InvestmentProfile
    asset: Asset
    position: Position
    purchase_lot: PurchaseLot
    redemption_fee_tier: RedemptionFeeTier
    fund_fee_policy: FundFeePolicy
    position_analysis_snapshot: PositionAnalysisSnapshot
    portfolio_ai_risk_summary: PortfolioAIRiskSummary
    market_snapshot: MarketSnapshot
    topic: Topic
    entity: Entity
    influence_relation: InfluenceRelation
    exposure: Exposure
    taxonomy_configuration: TaxonomyConfiguration
    source: Source
    external_document_content: ExternalDocumentContent
    raw_document_control: RawDocumentControl
    raw_document: RawDocument
    discovered_document: DiscoveredDocument
    event_cluster: EventCluster
    evidence_draft: EvidenceDraft
    evidence: Evidence
    evidence_score: EvidenceScore
    evidence_extraction_request: EvidenceExtractionRequest
    evidence_extraction_result: EvidenceExtractionResult
    decision_context: DecisionContext
    decision_result: DecisionResult
    analysis_result: AnalysisResult
    report: Report
    job_run: JobRun
    state_transition_record: StateTransitionRecord
    source_discovery_request: SourceDiscoveryRequest
    source_discovery_result: SourceDiscoveryResult
    source_fetch_request: SourceFetchRequest
    source_fetch_result: SourceFetchResult
    market_data_request: MarketDataRequest
    analysis_request: AnalysisRequest
    report_render_request: ReportRenderRequest
    rendered_report: RenderedReport
    notification_message: NotificationMessage
    delivery_receipt: DeliveryReceipt
    task_request: TaskRequest
    dispatch_receipt: DispatchReceipt
    storage_write_request: StorageWriteRequest
    storage_write_result: StorageWriteResult
    storage_record: StorageRecord
    portfolio_import_request: PortfolioImportRequest
    portfolio_import_result: PortfolioImportResult


def build_domain_schema() -> dict[str, object]:
    schema = DomainContractBundle.model_json_schema(
        ref_template="#/$defs/{model}", mode="validation"
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://investment-suggestion-tool.local/schemas/domain-contracts-v1.json"
    schema["x-schema-version"] = "1.0"
    return schema


def render_domain_schema() -> str:
    return json.dumps(build_domain_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def export_domain_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_domain_schema(), encoding="utf-8", newline="\n")
