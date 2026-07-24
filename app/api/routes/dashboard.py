"""Local personal-dashboard APIs for evidence, reports, crawl, and analysis."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import Settings
from app.domain.analysis import AnalysisResult, DecisionResult, Report, ReportDifference
from app.domain.base import Money
from app.domain.enums import EvidenceDirection, SourceHealthStatus
from app.domain.evidence import Evidence, EvidenceScore
from app.domain.portfolio import Position
from app.security.browser import rate_limit, require_csrf, set_csrf_cookie
from app.security.local_access import require_local_access
from app.services.analysis_workflow import AnalysisWorkflowError, AnalysisWorkflowService
from app.services.manual_pipeline import ManualPipelineOutcome, run_manual_pipeline
from app.services.portfolio import PortfolioConflict, PortfolioNotFound, PortfolioService
from app.services.reports import ReportService
from app.storage.database import Database
from app.storage.models import (
    EvidenceItemRow,
    EvidenceScoreRow,
    RawDocumentRow,
    ScheduledTaskRow,
    SourceHealthRow,
    SourceRow,
)

router = APIRouter(tags=["personal-dashboard"], dependencies=[Depends(require_local_access)])


class EvidenceView(BaseModel):
    evidence_id: str
    document_id: str
    source_id: str
    title: str
    source_url: str
    claim: str
    direction: EvidenceDirection
    total_score: Decimal | None = None
    scoring_version: str | None = None
    scored_at: datetime | None = None


class SourceHealthView(BaseModel):
    source_id: str
    name: str
    source_role: str
    source_kind: str
    enabled: bool
    status: SourceHealthStatus
    consecutive_failures: int
    last_success_at: datetime | None = None
    last_error_code: str | None = None


class AnalysisRunRequest(BaseModel):
    position_id: str = Field(min_length=1, max_length=128)
    portfolio_value: Money | None = None
    maximum_topic_weight: Decimal = Field(default=Decimal("0.35"), gt=0, le=1)
    target_topic_weight: Decimal = Field(default=Decimal("0.25"), gt=0, le=1)


class AnalysisRunResponse(BaseModel):
    decision: DecisionResult
    analysis: AnalysisResult
    report: Report


class PositionLocalUpdate(BaseModel):
    units: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    current_value: Money
    average_cost_per_unit: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    latest_purchase_on: date
    recurring_contribution: Money | None = None


class JobView(BaseModel):
    task_id: str
    task_type: str
    status: str
    not_before: datetime
    finished_at: datetime | None = None
    report_outcome: str | None = None
    report_reason: str | None = None
    considered_evidence_count: int | None = None
    new_evidence_count: int | None = None
    report_id: str | None = None


class PublicSettingsView(BaseModel):
    workspace_id: str
    schedule_hours: int = 3
    data_directory: str
    deepseek_configured: bool
    deepseek_model: str
    alpha_vantage_configured: bool
    portfolio_reference_value_configured: bool
    portfolio_reference_value: Decimal | None = None


def _database(request: Request) -> Database:
    return request.app.state.database


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/session/csrf", include_in_schema=False)
def csrf_session(request: Request) -> Response:
    response = Response(content='{"status":"ready"}', media_type="application/json")
    set_csrf_cookie(response, request)
    return response


@router.get("/positions", response_model=list[Position])
def positions_alias(request: Request) -> tuple[Position, ...]:
    with _database(request).session() as session:
        return PortfolioService(session, _settings(request).portfolio_workspace_id).list_positions()


@router.post(
    "/positions",
    response_model=Position,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
def create_position_alias(position: Position, request: Request) -> Position:
    rate_limit(request, "position-create")
    with _database(request).session() as session:
        try:
            return PortfolioService(
                session, _settings(request).portfolio_workspace_id
            ).create_position(position)
        except PortfolioConflict as error:
            raise HTTPException(status_code=409, detail="position creation conflicts") from error


@router.post(
    "/positions/{position_id}/local-update",
    response_model=Position,
    dependencies=[Depends(require_csrf)],
)
def update_local_position(
    position_id: str,
    update: PositionLocalUpdate,
    request: Request,
) -> Position:
    rate_limit(request, "position-update")
    with _database(request).session() as session:
        service = PortfolioService(session, _settings(request).portfolio_workspace_id)
        try:
            existing = service.get_position(position_id)
            payload = existing.model_dump()
            payload.update(update.model_dump())
            payload["snapshot_at"] = datetime.now(UTC)
            return service.update_position(Position.model_validate(payload))
        except PortfolioNotFound as error:
            raise HTTPException(status_code=404, detail="position not found") from error
        except (PortfolioConflict, ValueError) as error:
            raise HTTPException(status_code=409, detail="position update conflicts") from error


@router.get("/evidence", response_model=list[EvidenceView])
def list_evidence(request: Request, limit: int = 100) -> tuple[EvidenceView, ...]:
    safe_limit = min(500, max(1, limit))
    with _database(request).session() as session:
        rows = session.execute(
            select(EvidenceItemRow, RawDocumentRow, EvidenceScoreRow)
            .join(
                RawDocumentRow,
                (RawDocumentRow.workspace_id == EvidenceItemRow.workspace_id)
                & (RawDocumentRow.document_id == EvidenceItemRow.document_id),
            )
            .outerjoin(
                EvidenceScoreRow,
                (EvidenceScoreRow.workspace_id == EvidenceItemRow.workspace_id)
                & (EvidenceScoreRow.evidence_id == EvidenceItemRow.evidence_id),
            )
            .where(EvidenceItemRow.workspace_id == _settings(request).portfolio_workspace_id)
            .order_by(EvidenceItemRow.created_at.desc(), EvidenceScoreRow.created_at.desc())
            .limit(safe_limit * 3)
        ).all()
        values: dict[str, EvidenceView] = {}
        for evidence_row, raw, score_row in rows:
            if evidence_row.evidence_id in values:
                continue
            evidence = Evidence.model_validate(evidence_row.payload)
            score = EvidenceScore.model_validate(score_row.payload) if score_row else None
            values[evidence.evidence_id] = EvidenceView(
                evidence_id=evidence.evidence_id,
                document_id=evidence.document_id,
                source_id=raw.source_id,
                title=raw.title,
                source_url=raw.source_url,
                claim=evidence.draft.claim,
                direction=evidence.draft.direction,
                total_score=score.total if score else None,
                scoring_version=score.scoring_version if score else None,
                scored_at=score.scored_at if score else None,
            )
            if len(values) >= safe_limit:
                break
        return tuple(values.values())


@router.get("/reports/latest", response_model=Report | None)
def latest_report(request: Request) -> Report | None:
    with _database(request).session() as session:
        reports = ReportService(session, _settings(request).portfolio_workspace_id).list_reports(
            limit=1
        )
        return reports[0] if reports else None


@router.get("/reports", response_model=list[Report])
def list_reports(request: Request, limit: int = 100) -> tuple[Report, ...]:
    with _database(request).session() as session:
        return ReportService(session, _settings(request).portfolio_workspace_id).list_reports(
            limit=min(500, max(1, limit))
        )


@router.get("/reports/{report_id}", response_model=Report)
def get_report(report_id: str, request: Request) -> Report:
    with _database(request).session() as session:
        result = ReportService(session, _settings(request).portfolio_workspace_id).get(report_id)
        if result is None:
            raise HTTPException(status_code=404, detail="report not found")
        return result[0]


@router.get("/reports/{report_id}/html", response_class=HTMLResponse, include_in_schema=False)
def get_report_html(report_id: str, request: Request) -> HTMLResponse:
    with _database(request).session() as session:
        result = ReportService(session, _settings(request).portfolio_workspace_id).get(report_id)
        if result is None:
            raise HTTPException(status_code=404, detail="report not found")
        response = HTMLResponse(result[1])
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'none'; style-src 'none'; script-src 'none'; "
            "frame-src 'none'; base-uri 'none'; form-action 'none'"
        )
        return response


@router.get("/reports/{older_report_id}/diff/{newer_report_id}", response_model=ReportDifference)
def compare_reports(
    older_report_id: str, newer_report_id: str, request: Request
) -> ReportDifference:
    with _database(request).session() as session:
        try:
            return ReportService(session, _settings(request).portfolio_workspace_id).diff(
                older_report_id, newer_report_id
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail="report not found") from error


@router.get("/sources/health", response_model=list[SourceHealthView])
def source_health(request: Request) -> tuple[SourceHealthView, ...]:
    with _database(request).session() as session:
        workspace_id = _settings(request).portfolio_workspace_id
        rows = session.execute(
            select(SourceRow, SourceHealthRow)
            .outerjoin(
                SourceHealthRow,
                (SourceHealthRow.workspace_id == SourceRow.workspace_id)
                & (SourceHealthRow.source_id == SourceRow.source_id),
            )
            .where(SourceRow.workspace_id == workspace_id)
            .order_by(SourceRow.source_id)
        ).all()
        return tuple(
            SourceHealthView(
                source_id=source.source_id,
                name=str(source.payload.get("name", source.source_id)),
                source_role=str(source.payload.get("role") or "NEWS_DISCOVERY"),
                source_kind=str(source.payload.get("kind") or "UNKNOWN"),
                enabled=source.enabled,
                status=(
                    SourceHealthStatus(health.status)
                    if health is not None
                    else SourceHealthStatus.UNKNOWN
                ),
                consecutive_failures=health.consecutive_failures if health else 0,
                last_success_at=health.last_success_at if health else None,
                last_error_code=health.last_error_code if health else None,
            )
            for source, health in rows
        )


@router.post(
    "/jobs/crawl",
    response_model=ManualPipelineOutcome,
    dependencies=[Depends(require_csrf)],
)
async def crawl_once(request: Request) -> ManualPipelineOutcome:
    rate_limit(request, "manual-crawl")
    return await run_manual_pipeline(_database(request), _settings(request), now=datetime.now(UTC))


@router.get("/jobs", response_model=list[JobView])
def jobs(request: Request, limit: int = 100) -> tuple[JobView, ...]:
    with _database(request).session() as session:
        rows = session.scalars(
            select(ScheduledTaskRow)
            .where(ScheduledTaskRow.workspace_id == _settings(request).portfolio_workspace_id)
            .order_by(ScheduledTaskRow.created_at.desc())
            .limit(min(500, max(1, limit)))
        ).all()
        return tuple(
            JobView(
                task_id=row.task_id,
                task_type=row.task_type,
                status=row.status,
                not_before=row.not_before,
                finished_at=row.finished_at,
                report_outcome=(
                    str(row.payload["result"].get("status"))
                    if isinstance(row.payload.get("result"), dict)
                    and row.payload["result"].get("status")
                    else None
                ),
                report_reason=(
                    str(row.payload["result"].get("reason"))
                    if isinstance(row.payload.get("result"), dict)
                    and row.payload["result"].get("reason")
                    else None
                ),
                considered_evidence_count=(
                    int(row.payload["result"].get("considered_evidence_count", 0))
                    if isinstance(row.payload.get("result"), dict)
                    and "considered_evidence_count" in row.payload["result"]
                    else None
                ),
                new_evidence_count=(
                    int(row.payload["result"].get("new_evidence_count", 0))
                    if isinstance(row.payload.get("result"), dict)
                    and "new_evidence_count" in row.payload["result"]
                    else None
                ),
                report_id=(
                    str(row.payload["result"].get("report_id"))
                    if isinstance(row.payload.get("result"), dict)
                    and row.payload["result"].get("report_id")
                    else None
                ),
            )
            for row in rows
        )


@router.get("/settings/public", response_model=PublicSettingsView)
def public_settings(request: Request) -> PublicSettingsView:
    settings = _settings(request)
    return PublicSettingsView(
        workspace_id=settings.portfolio_workspace_id,
        data_directory=str(settings.data_dir),
        deepseek_configured=settings.deepseek_api_key is not None,
        deepseek_model=settings.deepseek_model,
        alpha_vantage_configured=settings.alpha_vantage_api_key is not None,
        portfolio_reference_value_configured=settings.portfolio_reference_value is not None,
        portfolio_reference_value=settings.portfolio_reference_value,
    )


@router.post(
    "/analysis/run",
    response_model=AnalysisRunResponse,
    dependencies=[Depends(require_csrf)],
)
async def run_analysis(payload: AnalysisRunRequest, request: Request) -> AnalysisRunResponse:
    rate_limit(request, "analysis-run")
    settings = _settings(request)
    portfolio_value = payload.portfolio_value
    if portfolio_value is None and settings.portfolio_reference_value is not None:
        portfolio_value = Money(
            amount=settings.portfolio_reference_value,
            currency="CNY",
        )
    if portfolio_value is None:
        raise HTTPException(status_code=422, detail="portfolio reference value is required")
    with _database(request).session() as session:
        try:
            decision_row, analysis_row, report_row = await AnalysisWorkflowService(
                session,
                settings.portfolio_workspace_id,
                settings,
            ).run(
                position_id=payload.position_id,
                portfolio_value=portfolio_value,
                now=datetime.now(UTC),
                maximum_topic_weight=payload.maximum_topic_weight,
                target_topic_weight=payload.target_topic_weight,
            )
        except (AnalysisWorkflowError, PortfolioNotFound, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="analysis inputs are incomplete or invalid",
            ) from error
        return AnalysisRunResponse(
            decision=DecisionResult.model_validate(decision_row.payload),
            analysis=AnalysisResult.model_validate(analysis_row.payload),
            report=Report.model_validate(report_row.payload),
        )
