"""Quality metrics and immutable local shadow-run snapshots."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.analysis import AnalysisResult, Report, ReportDifference
from app.domain.quality import QualityMetrics, ShadowRunRecord
from app.services.manual_pipeline import ManualPipelineOutcome
from app.storage.models import (
    AIExtractionRunRow,
    AnalysisResultRow,
    CrawlRunRow,
    NormalizedDocumentRow,
    RawDocumentRow,
    RelevanceAssessmentRow,
)
from app.storage.repositories import AuditRepository


def _ratio(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return Decimal("0")
    return (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001"))


def collect_quality_metrics(
    session: Session,
    workspace_id: str,
    *,
    now: datetime,
) -> QualityMetrics:
    raw_count = (
        session.scalar(
            select(func.count())
            .select_from(RawDocumentRow)
            .where(RawDocumentRow.workspace_id == workspace_id)
        )
        or 0
    )
    relevance_count = (
        session.scalar(
            select(func.count())
            .select_from(RelevanceAssessmentRow)
            .where(RelevanceAssessmentRow.workspace_id == workspace_id)
        )
        or 0
    )
    irrelevant_count = (
        session.scalar(
            select(func.count())
            .select_from(RelevanceAssessmentRow)
            .where(
                RelevanceAssessmentRow.workspace_id == workspace_id,
                RelevanceAssessmentRow.label == "IRRELEVANT",
            )
        )
        or 0
    )
    normalized_count = (
        session.scalar(
            select(func.count())
            .select_from(NormalizedDocumentRow)
            .where(NormalizedDocumentRow.workspace_id == workspace_id)
        )
        or 0
    )
    duplicate_count = (
        session.scalar(
            select(func.count())
            .select_from(NormalizedDocumentRow)
            .where(
                NormalizedDocumentRow.workspace_id == workspace_id,
                NormalizedDocumentRow.duplicate_of_document_id.is_not(None),
            )
        )
        or 0
    )
    crawl_count = (
        session.scalar(
            select(func.count())
            .select_from(CrawlRunRow)
            .where(CrawlRunRow.workspace_id == workspace_id)
        )
        or 0
    )
    failed_crawls = (
        session.scalar(
            select(func.count())
            .select_from(CrawlRunRow)
            .where(
                CrawlRunRow.workspace_id == workspace_id,
                CrawlRunRow.status != "SUCCEEDED",
            )
        )
        or 0
    )
    extraction_count = (
        session.scalar(
            select(func.count())
            .select_from(AIExtractionRunRow)
            .where(AIExtractionRunRow.workspace_id == workspace_id)
        )
        or 0
    )
    failed_extractions = (
        session.scalar(
            select(func.count())
            .select_from(AIExtractionRunRow)
            .where(
                AIExtractionRunRow.workspace_id == workspace_id,
                AIExtractionRunRow.status != "SUCCEEDED",
            )
        )
        or 0
    )
    token_totals = session.execute(
        select(
            func.coalesce(func.sum(AnalysisResultRow.input_tokens), 0),
            func.coalesce(func.sum(AnalysisResultRow.output_tokens), 0),
        ).where(AnalysisResultRow.workspace_id == workspace_id)
    ).one()
    return QualityMetrics(
        evaluated_at=now,
        raw_document_count=raw_count,
        relevance_assessment_count=relevance_count,
        duplicate_document_count=duplicate_count,
        crawl_run_count=crawl_count,
        ai_extraction_run_count=extraction_count,
        irrelevant_ratio=_ratio(irrelevant_count, relevance_count),
        duplicate_ratio=_ratio(duplicate_count, normalized_count),
        source_failure_ratio=_ratio(failed_crawls, crawl_count),
        ai_schema_failure_ratio=_ratio(failed_extractions, extraction_count),
        analysis_input_tokens=token_totals[0],
        analysis_output_tokens=token_totals[1],
        estimated_cost_cny=None,
    )


def build_shadow_record(
    *,
    position_id: str,
    report: Report,
    previous_report: Report | None,
    difference: ReportDifference | None,
    crawl: ManualPipelineOutcome,
    metrics: QualityMetrics,
    started_at: datetime,
    finished_at: datetime,
) -> ShadowRunRecord:
    reasons: list[str] = []
    if previous_report is None:
        reasons.append("initial shadow record")
    elif previous_report.report_id == report.report_id:
        reasons.append(f"no new report: {crawl.report_outcome or 'NO_REPORT_TRIGGERED'}")
        if crawl.report_reason:
            reasons.append(crawl.report_reason)
    elif difference is None:
        reasons.append("report was regenerated without a comparable predecessor")
    else:
        reasons.append(
            "decision label changed" if difference.decision_changed else "decision label unchanged"
        )
        if difference.added_evidence_ids:
            reasons.append(f"{len(difference.added_evidence_ids)} evidence item(s) added")
        if difference.removed_evidence_ids:
            reasons.append(f"{len(difference.removed_evidence_ids)} evidence item(s) removed")
        if len(reasons) == 1:
            reasons.append("supporting evidence set unchanged")
    analysis: AnalysisResult = report.analysis
    return ShadowRunRecord(
        shadow_run_id=str(
            uuid5(
                NAMESPACE_URL,
                f"shadow:{position_id}:{report.report_id}:{started_at.isoformat()}:"
                f"{finished_at.isoformat()}",
            )
        ),
        started_at=started_at,
        finished_at=finished_at,
        position_id=position_id,
        report_id=report.report_id,
        previous_report_id=previous_report.report_id if previous_report else None,
        decision_label=report.decision.label,
        decision_changed=difference.decision_changed if difference else False,
        change_reasons=tuple(reasons),
        evidence_count=len(report.evidence_ids),
        failed_source_count=crawl.failed_source_count,
        provider_name=analysis.provider_name,
        model_version=analysis.model_version,
        prompt_version=analysis.prompt_version,
        rule_version=report.rule_version,
        pipeline_version=report.pipeline_version,
        metrics=metrics,
        status="DEGRADED" if analysis.degraded or crawl.failed_source_count else "SUCCEEDED",
    )


def save_shadow_record(record: ShadowRunRecord, data_dir: Path) -> Path:
    target_dir = data_dir.expanduser().resolve() / "shadow-runs"
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = target_dir / f"{record.shadow_run_id}.json"
    payload = record.model_dump_json(indent=2) + "\n"
    if target.exists():
        if target.read_text(encoding="utf-8") != payload:
            raise ValueError("shadow-run snapshots cannot be overwritten")
        return target
    temporary = target.with_suffix(".tmp")
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    os.replace(temporary, target)
    latest = target_dir / "latest.json"
    latest_temporary = target_dir / "latest.tmp"
    latest_temporary.write_text(payload, encoding="utf-8", newline="\n")
    os.replace(latest_temporary, latest)
    return target


def record_shadow_audit(
    session: Session,
    workspace_id: str,
    record: ShadowRunRecord,
) -> None:
    AuditRepository(session, workspace_id).record(
        event_type="shadow_run.completed",
        actor="local-shadow-runner",
        target_type="report",
        target_id=record.report_id,
        outcome=record.status,
        occurred_at=record.finished_at.astimezone(UTC),
        details={
            "shadow_run_id": record.shadow_run_id,
            "decision_label": record.decision_label.value,
            "decision_changed": record.decision_changed,
            "failed_source_count": record.failed_source_count,
            "advisory_only": True,
        },
    )
