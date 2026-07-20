"""Persisted, budgeted evidence extraction orchestration."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.evidence import (
    PROMPT_VERSION,
    AIProviderError,
    DeepSeekEvidenceProvider,
    RuleEvidenceProvider,
    detect_prompt_injection,
)
from app.config import Settings
from app.domain.contracts import AIProvider
from app.domain.documents import NormalizedDocument
from app.domain.enums import DocumentState, RelevanceLabel
from app.domain.evidence import Evidence, EvidenceExtractionRequest, EvidenceExtractionResult
from app.domain.taxonomy import Source
from app.services.relevance import RULE_VERSION as RELEVANCE_RULE_VERSION
from app.storage.models import (
    AIExtractionRunRow,
    EventClusterRow,
    EvidenceItemRow,
    NormalizedDocumentRow,
    RawDocumentRow,
    RelevanceAssessmentRow,
    SourceRow,
)
from app.storage.repositories import TaxonomyRepository


def build_evidence_provider(settings: Settings) -> AIProvider:
    if settings.deepseek_api_key is None:
        return RuleEvidenceProvider()
    return DeepSeekEvidenceProvider(
        credential=settings.deepseek_api_key.get_secret_value(),
        model=settings.deepseek_model,
        base_url=str(settings.deepseek_base_url),
        max_input_characters=settings.deepseek_max_input_characters,
        max_output_tokens=settings.deepseek_max_output_tokens,
        timeout_seconds=settings.deepseek_timeout_seconds,
        proxy_url=(str(settings.collector_proxy_url) if settings.collector_proxy_url else None),
    )


class EvidenceExtractionService:
    def __init__(
        self,
        session: Session,
        workspace_id: str,
        provider: AIProvider,
        *,
        model_version: str,
        prompt_version: str = PROMPT_VERSION,
        max_input_characters: int = 12_000,
        max_calls_per_day: int = 20,
        daily_token_budget: int = 100_000,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.provider = provider
        self.model_version = model_version
        self.prompt_version = prompt_version
        self.max_input_characters = max_input_characters
        self.max_calls_per_day = max_calls_per_day
        self.daily_token_budget = daily_token_budget

    async def extract_pending(self, *, now: datetime, limit: int = 10) -> tuple[int, int, int]:
        calls, tokens = self._daily_usage(now)
        if calls >= self.max_calls_per_day or tokens >= self.daily_token_budget:
            return 0, 0, 1
        taxonomy = TaxonomyRepository(self.session, self.workspace_id).get_active()
        if taxonomy is None:
            return 0, 0, 0
        rows = self.session.execute(
            select(NormalizedDocumentRow, RelevanceAssessmentRow, SourceRow)
            .join(
                RelevanceAssessmentRow,
                (RelevanceAssessmentRow.workspace_id == NormalizedDocumentRow.workspace_id)
                & (RelevanceAssessmentRow.document_id == NormalizedDocumentRow.document_id),
            )
            .join(
                SourceRow,
                (SourceRow.workspace_id == NormalizedDocumentRow.workspace_id)
                & (SourceRow.source_id == NormalizedDocumentRow.source_id),
            )
            .outerjoin(
                AIExtractionRunRow,
                (AIExtractionRunRow.workspace_id == NormalizedDocumentRow.workspace_id)
                & (AIExtractionRunRow.document_id == NormalizedDocumentRow.document_id)
                & (AIExtractionRunRow.provider_name == self.provider.provider_name)
                & (AIExtractionRunRow.model_version == self.model_version)
                & (AIExtractionRunRow.prompt_version == self.prompt_version),
            )
            .where(
                NormalizedDocumentRow.workspace_id == self.workspace_id,
                NormalizedDocumentRow.duplicate_of_document_id.is_(None),
                RelevanceAssessmentRow.label == RelevanceLabel.RELEVANT.value,
                RelevanceAssessmentRow.rule_version == RELEVANCE_RULE_VERSION,
                RelevanceAssessmentRow.taxonomy_version == taxonomy.config_version,
                AIExtractionRunRow.extraction_run_id.is_(None),
            )
            .order_by(NormalizedDocumentRow.normalized_at, NormalizedDocumentRow.document_id)
            .limit(limit)
        ).all()
        succeeded = failed = 0
        for normalized_row, assessment_row, source_row in rows:
            if calls >= self.max_calls_per_day or tokens >= self.daily_token_budget:
                break
            request = self._request(normalized_row, assessment_row, source_row)
            input_hash = sha256(
                json.dumps(request.model_dump(mode="json"), sort_keys=True).encode()
            ).hexdigest()
            try:
                result = await self.provider.extract(request)
                self._validate_result(request, result)
                self._persist_success(normalized_row, result, input_hash=input_hash, now=now)
                succeeded += 1
                tokens += result.input_tokens + result.output_tokens
            except (AIProviderError, ValueError) as error:
                code = error.code if isinstance(error, AIProviderError) else "INVALID_MODEL_OUTPUT"
                self._persist_failure(
                    normalized_row.document_id,
                    input_hash=input_hash,
                    error_code=code,
                    input_tokens=int(getattr(self.provider, "last_input_tokens", 0)),
                    output_tokens=int(getattr(self.provider, "last_output_tokens", 0)),
                    elapsed_ms=int(getattr(self.provider, "last_elapsed_ms", 0)),
                    now=now,
                )
                tokens += int(getattr(self.provider, "last_input_tokens", 0))
                tokens += int(getattr(self.provider, "last_output_tokens", 0))
                failed += 1
            calls += 1
        self.session.flush()
        return (
            succeeded,
            failed,
            int(calls >= self.max_calls_per_day or tokens >= self.daily_token_budget),
        )

    def _daily_usage(self, now: datetime) -> tuple[int, int]:
        rows = self.session.scalars(
            select(AIExtractionRunRow).where(
                AIExtractionRunRow.workspace_id == self.workspace_id,
                AIExtractionRunRow.provider_name == self.provider.provider_name,
            )
        ).all()
        today = now.astimezone(UTC).date()
        daily = [row for row in rows if row.completed_at.astimezone(UTC).date() == today]
        return len(daily), sum(row.input_tokens + row.output_tokens for row in daily)

    def _request(
        self,
        normalized_row: NormalizedDocumentRow,
        assessment_row: RelevanceAssessmentRow,
        source_row: SourceRow,
    ) -> EvidenceExtractionRequest:
        document = NormalizedDocument.model_validate(normalized_row.payload)
        assessment = assessment_row.payload
        source = Source.model_validate(source_row.payload)
        bounded_body = document.body[: self.max_input_characters]
        injection_flags = detect_prompt_injection(
            f"{document.title}\n{document.summary or ''}\n{bounded_body}"
        )
        suspicious = tuple(
            sorted(
                {
                    *document.suspicious_flags,
                    *(f"prompt_injection:{item}" for item in injection_flags),
                }
            )
        )
        return EvidenceExtractionRequest(
            document_id=document.document_id,
            title=document.title,
            summary=document.summary,
            normalized_body=bounded_body,
            language=document.detected_language,
            topic_ids=tuple(assessment.get("topic_ids", ())),
            entity_ids=tuple(assessment.get("entity_ids", ())),
            source_kind=source.kind.value,
            published_at=document.published_at,
            suspicious_flags=suspicious,
            prompt_version=self.prompt_version,
        )

    @staticmethod
    def _validate_result(
        request: EvidenceExtractionRequest,
        result: EvidenceExtractionResult,
    ) -> None:
        if (
            result.document_id != request.document_id
            or result.prompt_version != request.prompt_version
        ):
            raise ValueError("provider result control fields do not match the request")
        if not set(result.related_topic_ids).issubset(request.topic_ids):
            raise ValueError("provider result contains an unknown topic")
        if not set(result.related_entity_ids).issubset(request.entity_ids):
            raise ValueError("provider result contains an unknown entity")
        expected_primary = request.source_kind in {
            "OFFICIAL",
            "REGULATOR",
            "FUND_MANAGER",
            "COMPANY_OFFICIAL",
        }
        if result.source_is_primary != expected_primary:
            raise ValueError("provider result altered trusted source provenance")
        searchable = f"{request.title}\n{request.summary or ''}\n{request.normalized_body}"
        for draft in result.evidence:
            if not set(draft.topic_ids).issubset(request.topic_ids):
                raise ValueError("provider evidence contains an unknown topic")
            if not set(draft.entity_ids).issubset(request.entity_ids):
                raise ValueError("provider evidence contains an unknown entity")
            if draft.quote is None or len(draft.quote) > 500 or draft.quote not in searchable:
                raise ValueError("provider evidence excerpt is not present in the document")

    def _cluster_id(self, document_id: str) -> str | None:
        rows = self.session.scalars(
            select(EventClusterRow).where(EventClusterRow.workspace_id == self.workspace_id)
        ).all()
        return next(
            (
                row.cluster_id
                for row in rows
                if document_id in tuple(row.payload.get("document_ids", ()))
            ),
            None,
        )

    def _persist_success(
        self,
        normalized_row: NormalizedDocumentRow,
        result: EvidenceExtractionResult,
        *,
        input_hash: str,
        now: datetime,
    ) -> None:
        cluster_id = self._cluster_id(normalized_row.document_id)
        evidence_ids: list[str] = []
        for index, draft in enumerate(result.evidence):
            evidence_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"{normalized_row.document_id}:{result.model_version}:{result.prompt_version}:{index}",
                )
            )
            evidence = Evidence(
                evidence_id=evidence_id,
                document_id=normalized_row.document_id,
                cluster_id=cluster_id,
                draft=draft,
                extracted_at=result.completed_at,
                extractor_name=result.provider_name,
                model_version=result.model_version,
                prompt_version=result.prompt_version,
            )
            self.session.add(
                EvidenceItemRow(
                    evidence_id=evidence_id,
                    workspace_id=self.workspace_id,
                    document_id=normalized_row.document_id,
                    cluster_id=cluster_id,
                    schema_version=evidence.schema_version,
                    payload=evidence.model_dump(mode="json"),
                )
            )
            evidence_ids.append(evidence_id)
        payload: dict[str, Any] = {
            "evidence_ids": evidence_ids,
            "relevance": str(result.relevance),
            "event_type": result.event_type,
            "related_topic_ids": list(result.related_topic_ids),
            "related_entity_ids": list(result.related_entity_ids),
            "source_is_primary": result.source_is_primary,
            "unknowns": list(result.unknowns),
        }
        self._add_run(
            normalized_row.document_id,
            status="SUCCEEDED",
            input_hash=input_hash,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            elapsed_ms=result.elapsed_ms,
            error_code=None,
            completed_at=result.completed_at,
            payload=payload,
        )
        raw = self.session.scalar(
            select(RawDocumentRow).where(
                RawDocumentRow.workspace_id == self.workspace_id,
                RawDocumentRow.document_id == normalized_row.document_id,
            )
        )
        if raw is not None:
            raw.state = DocumentState.EXTRACTED.value
            raw.state_version += 1
            raw.updated_at = now

    def _persist_failure(
        self,
        document_id: str,
        *,
        input_hash: str,
        error_code: str,
        input_tokens: int,
        output_tokens: int,
        elapsed_ms: int,
        now: datetime,
    ) -> None:
        self._add_run(
            document_id,
            status="NEEDS_REVIEW",
            input_hash=input_hash,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            elapsed_ms=elapsed_ms,
            error_code=error_code,
            completed_at=now,
            payload={"review_reason": error_code},
        )

    def _add_run(
        self,
        document_id: str,
        *,
        status: str,
        input_hash: str,
        input_tokens: int,
        output_tokens: int,
        elapsed_ms: int,
        error_code: str | None,
        completed_at: datetime,
        payload: dict[str, Any],
    ) -> None:
        key = (
            f"{document_id}:{self.provider.provider_name}:"
            f"{self.model_version}:{self.prompt_version}"
        )
        self.session.add(
            AIExtractionRunRow(
                extraction_run_id=str(uuid5(NAMESPACE_URL, key)),
                workspace_id=self.workspace_id,
                document_id=document_id,
                status=status,
                provider_name=self.provider.provider_name,
                model_version=self.model_version,
                prompt_version=self.prompt_version,
                input_sha256=input_hash,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                elapsed_ms=elapsed_ms,
                attempts=int(getattr(self.provider, "last_attempts", 1)),
                error_code=error_code,
                completed_at=completed_at,
                schema_version="1.0",
                payload=payload,
            )
        )
