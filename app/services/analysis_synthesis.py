"""Budgeted synthesis orchestration over persisted deterministic decisions."""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.evidence import AIProviderError
from app.ai.synthesis import (
    SYNTHESIS_PROMPT_VERSION,
    DeepSeekAIProvider,
    RuleSynthesisProvider,
)
from app.config import Settings
from app.domain.analysis import AnalysisResult, DecisionContext, DecisionResult
from app.domain.contracts import AnalysisRequest
from app.storage.models import AnalysisResultRow, AnalysisRunRow, DecisionResultRow


class SynthesisProvider(Protocol):
    provider_name: str

    async def synthesize(self, request: AnalysisRequest) -> AnalysisResult: ...


def build_synthesis_provider(settings: Settings) -> SynthesisProvider:
    if settings.deepseek_api_key is None:
        return RuleSynthesisProvider()
    return DeepSeekAIProvider(
        credential=settings.deepseek_api_key.get_secret_value(),
        model=settings.deepseek_model,
        base_url=str(settings.deepseek_base_url),
        max_input_characters=settings.deepseek_max_input_characters,
        max_output_tokens=settings.deepseek_max_output_tokens,
        timeout_seconds=settings.deepseek_timeout_seconds,
        proxy_url=(str(settings.collector_proxy_url) if settings.collector_proxy_url else None),
    )


class AnalysisSynthesisService:
    def __init__(
        self,
        session: Session,
        workspace_id: str,
        provider: SynthesisProvider,
        *,
        prompt_version: str = SYNTHESIS_PROMPT_VERSION,
        model_version: str,
        max_calls_per_day: int = 10,
        daily_token_budget: int = 50_000,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.provider = provider
        self.prompt_version = prompt_version
        self.model_version = model_version
        self.max_calls_per_day = max_calls_per_day
        self.daily_token_budget = daily_token_budget

    async def synthesize_pending(self, *, now: datetime, limit: int = 10) -> tuple[int, int, int]:
        if now.tzinfo is None:
            raise ValueError("analysis time must include a timezone")
        calls, tokens = self._daily_usage(now)
        rows = self.session.execute(
            select(AnalysisRunRow, DecisionResultRow)
            .join(
                DecisionResultRow,
                (DecisionResultRow.workspace_id == AnalysisRunRow.workspace_id)
                & (DecisionResultRow.analysis_run_id == AnalysisRunRow.analysis_run_id),
            )
            .outerjoin(
                AnalysisResultRow,
                (AnalysisResultRow.workspace_id == AnalysisRunRow.workspace_id)
                & (AnalysisResultRow.analysis_run_id == AnalysisRunRow.analysis_run_id)
                & (AnalysisResultRow.prompt_version == self.prompt_version)
                & (AnalysisResultRow.model_version == self.model_version),
            )
            .where(
                AnalysisRunRow.workspace_id == self.workspace_id,
                AnalysisResultRow.analysis_id.is_(None),
            )
            .order_by(AnalysisRunRow.created_at, AnalysisRunRow.analysis_run_id)
            .limit(limit)
        ).all()
        completed = degraded = budget_fallbacks = 0
        for run, decision_row in rows:
            request = self._request(run, decision_row, now)
            provider = self.provider
            error_code: str | None = None
            budget_reached = provider.provider_name == "deepseek" and (
                calls >= self.max_calls_per_day or tokens >= self.daily_token_budget
            )
            if budget_reached:
                provider = RuleSynthesisProvider()
                error_code = "DAILY_BUDGET_REACHED"
                budget_fallbacks += 1
            attempted_input_tokens = 0
            attempted_output_tokens = 0
            try:
                result = await provider.synthesize(request)
                attempted_input_tokens = result.input_tokens
                attempted_output_tokens = result.output_tokens
            except AIProviderError as error:
                error_code = error.code
                attempted_input_tokens = int(getattr(provider, "last_input_tokens", 0))
                attempted_output_tokens = int(getattr(provider, "last_output_tokens", 0))
                result = await RuleSynthesisProvider().synthesize(request)
            completed += 1
            degraded += result.degraded
            if self.provider.provider_name == "deepseek" and not budget_reached:
                calls += 1
                tokens += attempted_input_tokens + attempted_output_tokens
            self._persist(
                run,
                result,
                requested_provider_name=provider.provider_name,
                input_tokens=attempted_input_tokens,
                output_tokens=attempted_output_tokens,
                error_code=error_code,
            )
        self.session.flush()
        return completed, degraded, budget_fallbacks

    def _request(
        self,
        run: AnalysisRunRow,
        decision_row: DecisionResultRow,
        now: datetime,
    ) -> AnalysisRequest:
        context_payload = run.input_snapshot.get("context", run.input_snapshot)
        return AnalysisRequest(
            context=DecisionContext.model_validate(context_payload),
            decision=DecisionResult.model_validate(decision_row.payload),
            prompt_version=self.prompt_version,
            analyzed_at=now,
        )

    def _persist(
        self,
        run: AnalysisRunRow,
        result: AnalysisResult,
        *,
        requested_provider_name: str,
        input_tokens: int,
        output_tokens: int,
        error_code: str | None,
    ) -> None:
        self.session.add(
            AnalysisResultRow(
                analysis_id=result.analysis_id,
                workspace_id=self.workspace_id,
                analysis_run_id=run.analysis_run_id,
                status="DEGRADED" if result.degraded else "SUCCEEDED",
                provider_name=requested_provider_name,
                model_version=self.model_version,
                prompt_version=result.prompt_version,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error_code=error_code,
                completed_at=result.analyzed_at,
                schema_version=result.schema_version,
                payload=result.model_dump(mode="json"),
            )
        )
        run.prompt_version = result.prompt_version
        run.payload = {
            **run.payload,
            "status": "ANALYZED",
            "analysis_id": result.analysis_id,
            "analysis_provider": result.provider_name,
            "analysis_model": result.model_version,
            "analysis_degraded": result.degraded,
        }
        run.updated_at = result.analyzed_at

    def _daily_usage(self, now: datetime) -> tuple[int, int]:
        normalized = now.astimezone(UTC)
        day_start = datetime.combine(normalized.date(), time.min, tzinfo=UTC)
        day_end = datetime.combine(normalized.date(), time.max, tzinfo=UTC)
        row = self.session.execute(
            select(
                func.count(AnalysisResultRow.analysis_id),
                func.coalesce(
                    func.sum(AnalysisResultRow.input_tokens + AnalysisResultRow.output_tokens), 0
                ),
            ).where(
                AnalysisResultRow.workspace_id == self.workspace_id,
                AnalysisResultRow.provider_name == "deepseek",
                AnalysisResultRow.completed_at >= day_start,
                AnalysisResultRow.completed_at <= day_end,
            )
        ).one()
        return int(row[0]), int(row[1])
