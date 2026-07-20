"""Assemble, validate, render, persist, and compare immutable reports."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.analysis import (
    AnalysisResult,
    DecisionContext,
    DecisionResult,
    Report,
    ReportDifference,
    ReportSource,
)
from app.domain.contracts import RenderedReport, ReportRenderRequest
from app.domain.enums import DocumentState, ReportFormat
from app.domain.evidence import Evidence
from app.reports.html import HTMLReportRenderer
from app.storage.models import (
    AnalysisResultRow,
    AnalysisRunRow,
    DecisionResultRow,
    EvidenceItemRow,
    RawDocumentRow,
    ReportRow,
    SourceHealthRow,
)


class ReportService:
    def __init__(
        self,
        session: Session,
        workspace_id: str,
        renderer: HTMLReportRenderer | None = None,
        *,
        stale_after_hours: int = 30,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.renderer = renderer or HTMLReportRenderer()
        self.stale_after_hours = stale_after_hours

    async def generate_pending(self, *, now: datetime, limit: int = 10) -> tuple[int, int]:
        if now.tzinfo is None:
            raise ValueError("report generation time must include a timezone")
        rows = self.session.execute(
            select(AnalysisRunRow, DecisionResultRow, AnalysisResultRow)
            .join(
                DecisionResultRow,
                (DecisionResultRow.workspace_id == AnalysisRunRow.workspace_id)
                & (DecisionResultRow.analysis_run_id == AnalysisRunRow.analysis_run_id),
            )
            .join(
                AnalysisResultRow,
                (AnalysisResultRow.workspace_id == AnalysisRunRow.workspace_id)
                & (AnalysisResultRow.analysis_run_id == AnalysisRunRow.analysis_run_id),
            )
            .outerjoin(
                ReportRow,
                (ReportRow.workspace_id == AnalysisRunRow.workspace_id)
                & (ReportRow.analysis_run_id == AnalysisRunRow.analysis_run_id)
                & (ReportRow.template_version == self.renderer.template_version),
            )
            .where(
                AnalysisRunRow.workspace_id == self.workspace_id,
                ReportRow.report_id.is_(None),
            )
            .order_by(AnalysisRunRow.created_at, AnalysisRunRow.analysis_run_id)
            .limit(limit)
        ).all()
        generated = stale = 0
        for run, decision_row, analysis_row in rows:
            report = self._assemble(run, decision_row, analysis_row, now)
            rendered = await self.renderer.render(
                ReportRenderRequest(report=report, output_format=ReportFormat.HTML)
            )
            self._persist(run, report, rendered)
            generated += 1
            stale += report.data_is_stale
        self.session.flush()
        return generated, stale

    def get(self, report_id: str) -> tuple[Report, str] | None:
        row = self.session.scalar(
            select(ReportRow).where(
                ReportRow.workspace_id == self.workspace_id,
                ReportRow.report_id == report_id,
            )
        )
        if row is None:
            return None
        return Report.model_validate(row.payload), row.rendered_content

    def list_reports(self, *, limit: int = 100) -> tuple[Report, ...]:
        rows = self.session.scalars(
            select(ReportRow)
            .where(ReportRow.workspace_id == self.workspace_id)
            .order_by(ReportRow.generated_at.desc(), ReportRow.report_id.desc())
            .limit(limit)
        ).all()
        return tuple(Report.model_validate(row.payload) for row in rows)

    def diff(self, older_report_id: str, newer_report_id: str) -> ReportDifference:
        older_value = self.get(older_report_id)
        newer_value = self.get(newer_report_id)
        if older_value is None or newer_value is None:
            raise LookupError("one or both report snapshots were not found")
        older, _ = older_value
        newer, _ = newer_value
        older_ids = set(older.evidence_ids)
        newer_ids = set(newer.evidence_ids)
        return ReportDifference(
            older_report_id=older.report_id,
            newer_report_id=newer.report_id,
            decision_changed=older.decision.label is not newer.decision.label,
            older_label=older.decision.label,
            newer_label=newer.decision.label,
            confidence_change=newer.analysis.confidence - older.analysis.confidence,
            added_evidence_ids=tuple(sorted(newer_ids - older_ids)),
            removed_evidence_ids=tuple(sorted(older_ids - newer_ids)),
        )

    def _assemble(
        self,
        run: AnalysisRunRow,
        decision_row: DecisionResultRow,
        analysis_row: AnalysisResultRow,
        now: datetime,
    ) -> Report:
        context_payload = run.input_snapshot.get("context", run.input_snapshot)
        context = DecisionContext.model_validate(context_payload)
        decision = DecisionResult.model_validate(decision_row.payload)
        analysis = AnalysisResult.model_validate(analysis_row.payload)
        evidence_ids = tuple(item.evidence_id for item in context.evidence)
        sources = self._sources(context.evidence)
        source_ids = tuple(sorted({source.source_id for source in sources}))
        age_hours = max(
            0,
            (now.astimezone(UTC) - context.data_as_of.astimezone(UTC)).total_seconds() / 3600,
        )
        report_id = str(
            uuid5(
                NAMESPACE_URL,
                f"{run.analysis_run_id}:{self.renderer.template_version}:{now.isoformat()}",
            )
        )
        return Report(
            report_id=report_id,
            asset_id=context.asset_id,
            topic_ids=context.topic_ids,
            decision=decision,
            analysis=analysis,
            source_ids=source_ids,
            evidence_ids=evidence_ids,
            sources=sources,
            data_as_of=context.data_as_of,
            data_is_stale=age_hours > self.stale_after_hours,
            stale_after_hours=self.stale_after_hours,
            generated_at=now,
            pipeline_version=context.pipeline_version,
            rule_version=decision.rule_version,
            prompt_version=analysis.prompt_version,
            template_version=self.renderer.template_version,
        )

    def _sources(self, evidence: tuple[Evidence, ...]) -> tuple[ReportSource, ...]:
        by_id = {item.evidence_id: item for item in evidence}
        rows = self.session.execute(
            select(EvidenceItemRow, RawDocumentRow)
            .join(
                RawDocumentRow,
                (RawDocumentRow.workspace_id == EvidenceItemRow.workspace_id)
                & (RawDocumentRow.document_id == EvidenceItemRow.document_id),
            )
            .where(
                EvidenceItemRow.workspace_id == self.workspace_id,
                EvidenceItemRow.evidence_id.in_(tuple(by_id)),
            )
        ).all()
        health = {
            row.source_id: row.status
            for row in self.session.scalars(
                select(SourceHealthRow).where(SourceHealthRow.workspace_id == self.workspace_id)
            ).all()
        }
        return tuple(
            ReportSource(
                evidence_id=evidence_row.evidence_id,
                source_id=raw.source_id,
                title=raw.title,
                url=raw.source_url,
                health_status=health.get(raw.source_id, "UNKNOWN"),
            )
            for evidence_row, raw in rows
        )

    def _persist(
        self,
        run: AnalysisRunRow,
        report: Report,
        rendered: RenderedReport,
    ) -> None:
        content = rendered.content.decode("utf-8")
        self.session.add(
            ReportRow(
                report_id=report.report_id,
                workspace_id=self.workspace_id,
                analysis_run_id=run.analysis_run_id,
                pipeline_version=report.pipeline_version,
                rule_version=report.rule_version,
                prompt_version=report.prompt_version,
                template_version=report.template_version,
                media_type=rendered.media_type,
                content_sha256=rendered.content_sha256,
                rendered_content=content,
                generated_at=report.generated_at,
                input_snapshot={
                    "analysis_run_id": run.analysis_run_id,
                    "decision_id": report.decision.decision_id,
                    "analysis_id": report.analysis.analysis_id,
                    "evidence_ids": report.evidence_ids,
                    "source_ids": report.source_ids,
                },
                schema_version=report.schema_version,
                payload=report.model_dump(mode="json"),
            )
        )
        for evidence_id in report.evidence_ids:
            evidence_row = self.session.scalar(
                select(EvidenceItemRow).where(
                    EvidenceItemRow.workspace_id == self.workspace_id,
                    EvidenceItemRow.evidence_id == evidence_id,
                )
            )
            if evidence_row is None:
                continue
            raw = self.session.scalar(
                select(RawDocumentRow).where(
                    RawDocumentRow.workspace_id == self.workspace_id,
                    RawDocumentRow.document_id == evidence_row.document_id,
                )
            )
            if raw is not None:
                raw.state = DocumentState.PUBLISHED.value
                raw.state_version += 1
                raw.updated_at = report.generated_at
