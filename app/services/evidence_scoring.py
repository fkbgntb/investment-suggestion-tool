"""Transparent deterministic scoring with cluster-level origin independence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.enums import DocumentState, SourceKind, TrustTier
from app.domain.evidence import Evidence, EvidenceScore
from app.domain.taxonomy import Source
from app.storage.models import (
    AIExtractionRunRow,
    EventClusterRow,
    EvidenceItemRow,
    EvidenceScoreRow,
    RawDocumentRow,
    SourceRow,
)

SCORING_VERSION = "evidence-score-1.0.0"
_SOURCE_QUALITY = {
    TrustTier.PRIMARY: Decimal("0.95"),
    TrustTier.PROFESSIONAL: Decimal("0.80"),
    TrustTier.SECONDARY: Decimal("0.60"),
    TrustTier.SENTIMENT_ONLY: Decimal("0.25"),
}
_HALF_LIFE_DAYS = {
    "SHORT": Decimal("3"),
    "MEDIUM": Decimal("14"),
    "LONG": Decimal("60"),
    "UNKNOWN": Decimal("7"),
}
_FOUR_PLACES = Decimal("0.0001")


@dataclass(frozen=True)
class EvidenceScoringContext:
    evidence: Evidence
    source: Source
    relevance: Decimal
    published_at: datetime
    independent_source_count: int
    same_origin_reprint: bool


class EvidenceScorer(Protocol):
    scorer_name: str
    scoring_version: str

    def score(self, context: EvidenceScoringContext, *, scored_at: datetime) -> EvidenceScore: ...


def _bounded(value: Decimal) -> Decimal:
    return min(Decimal("1"), max(Decimal("0"), value)).quantize(
        _FOUR_PLACES, rounding=ROUND_HALF_UP
    )


class DeterministicEvidenceScorer:
    scorer_name = "deterministic-evidence-scorer"
    scoring_version = SCORING_VERSION

    def score(self, context: EvidenceScoringContext, *, scored_at: datetime) -> EvidenceScore:
        source_quality = _SOURCE_QUALITY[context.source.trust_tier]
        reasons = [f"来源等级：{context.source.trust_tier.value}"]
        confidence_cap: Decimal | None = None
        if context.source.kind is SourceKind.AGGREGATOR:
            source_quality = min(source_quality, Decimal("0.45"))
            confidence_cap = Decimal("0.35")
            reasons.append("聚合来源应用 0.35 总分上限")
        elif context.source.kind in {SourceKind.SOCIAL, SourceKind.COMMUNITY}:
            confidence_cap = Decimal("0.15")
            reasons.append("情绪来源应用 0.15 总分上限且不得单独触发动作")

        if context.same_origin_reprint:
            independence = Decimal("0")
            reasons.append("同一事件中的同源转载不增加独立证据")
        else:
            independence = min(
                Decimal("1"),
                Decimal("0.50") + Decimal("0.25") * Decimal(context.independent_source_count - 1),
            )
            reasons.append(f"事件独立来源数：{context.independent_source_count}")

        age_seconds = max(
            0,
            (scored_at.astimezone(UTC) - context.published_at.astimezone(UTC)).total_seconds(),
        )
        age_days = Decimal(str(age_seconds / 86_400))
        half_life = _HALF_LIFE_DAYS[context.evidence.draft.impact_horizon]
        recency = Decimal(str(0.5 ** float(age_days / half_life)))
        relevance = _bounded(context.relevance)
        directness = _bounded(context.evidence.draft.directness)
        extraction_confidence = _bounded(context.evidence.draft.confidence)
        total = _bounded(
            source_quality * independence * recency * relevance * directness * extraction_confidence
        )
        if confidence_cap is not None:
            total = min(total, confidence_cap)
        return EvidenceScore(
            evidence_id=context.evidence.evidence_id,
            source_quality=_bounded(source_quality),
            independence=_bounded(independence),
            recency=_bounded(recency),
            relevance=relevance,
            directness=directness,
            extraction_confidence=extraction_confidence,
            total=_bounded(total),
            source_kind=context.source.kind,
            trust_tier=context.source.trust_tier,
            independent_source_count=context.independent_source_count,
            same_origin_reprint=context.same_origin_reprint,
            confidence_cap=_bounded(confidence_cap) if confidence_cap is not None else None,
            component_reasons=tuple(reasons),
            scoring_version=self.scoring_version,
            scored_at=scored_at,
        )


class EvidenceScoringService:
    def __init__(
        self,
        session: Session,
        workspace_id: str,
        scorer: EvidenceScorer | None = None,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.scorer = scorer or DeterministicEvidenceScorer()

    def score_pending(self, *, now: datetime, limit: int = 1000) -> tuple[int, int, int]:
        rows = self.session.execute(
            select(EvidenceItemRow, RawDocumentRow, SourceRow)
            .join(
                RawDocumentRow,
                (RawDocumentRow.workspace_id == EvidenceItemRow.workspace_id)
                & (RawDocumentRow.document_id == EvidenceItemRow.document_id),
            )
            .join(
                SourceRow,
                (SourceRow.workspace_id == RawDocumentRow.workspace_id)
                & (SourceRow.source_id == RawDocumentRow.source_id),
            )
            .outerjoin(
                EvidenceScoreRow,
                (EvidenceScoreRow.workspace_id == EvidenceItemRow.workspace_id)
                & (EvidenceScoreRow.evidence_id == EvidenceItemRow.evidence_id)
                & (EvidenceScoreRow.scoring_version == self.scorer.scoring_version),
            )
            .where(
                EvidenceItemRow.workspace_id == self.workspace_id,
                EvidenceScoreRow.score_id.is_(None),
            )
            .order_by(EvidenceItemRow.created_at, EvidenceItemRow.evidence_id)
            .limit(limit)
        ).all()
        positive = negative = 0
        for evidence_row, raw_row, source_row in rows:
            evidence = Evidence.model_validate(evidence_row.payload)
            source = Source.model_validate(source_row.payload)
            unique_count, reprint = self._independence(evidence, raw_row)
            score = self.scorer.score(
                EvidenceScoringContext(
                    evidence=evidence,
                    source=source,
                    relevance=self._relevance(evidence.evidence_id),
                    published_at=raw_row.published_at or raw_row.fetched_at,
                    independent_source_count=unique_count,
                    same_origin_reprint=reprint,
                ),
                scored_at=now,
            )
            score_id = str(uuid5(NAMESPACE_URL, f"{evidence.evidence_id}:{score.scoring_version}"))
            self.session.add(
                EvidenceScoreRow(
                    score_id=score_id,
                    workspace_id=self.workspace_id,
                    evidence_id=evidence.evidence_id,
                    scoring_version=score.scoring_version,
                    schema_version=score.schema_version,
                    payload=score.model_dump(mode="json"),
                )
            )
            raw_row.state = DocumentState.SCORED.value
            raw_row.state_version += 1
            raw_row.updated_at = now
            positive += evidence.draft.direction.value == "POSITIVE"
            negative += evidence.draft.direction.value == "NEGATIVE"
        self.session.flush()
        return len(rows), positive, negative

    def _relevance(self, evidence_id: str) -> Decimal:
        runs = self.session.scalars(
            select(AIExtractionRunRow).where(
                AIExtractionRunRow.workspace_id == self.workspace_id,
                AIExtractionRunRow.status == "SUCCEEDED",
            )
        ).all()
        for run in runs:
            if evidence_id in tuple(run.payload.get("evidence_ids", ())):
                return Decimal(str(run.payload.get("relevance", "0")))
        return Decimal("0")

    def _independence(
        self,
        evidence: Evidence,
        raw: RawDocumentRow,
    ) -> tuple[int, bool]:
        if evidence.cluster_id is None:
            return 1, False
        cluster = self.session.scalar(
            select(EventClusterRow).where(
                EventClusterRow.workspace_id == self.workspace_id,
                EventClusterRow.cluster_id == evidence.cluster_id,
            )
        )
        if cluster is None:
            return 1, False
        document_ids = tuple(cluster.payload.get("document_ids", ()))
        documents = self.session.scalars(
            select(RawDocumentRow)
            .where(
                RawDocumentRow.workspace_id == self.workspace_id,
                RawDocumentRow.document_id.in_(document_ids),
            )
            .order_by(RawDocumentRow.fetched_at, RawDocumentRow.document_id)
        ).all()
        source_rows = {
            row.source_id: Source.model_validate(row.payload)
            for row in self.session.scalars(
                select(SourceRow).where(SourceRow.workspace_id == self.workspace_id)
            ).all()
        }

        def origin_key(document: RawDocumentRow) -> str:
            configured = source_rows.get(document.source_id)
            author = str(document.metadata_payload.get("author") or "").casefold().strip()
            if configured is not None and configured.kind is SourceKind.AGGREGATOR and author:
                return f"{document.source_id}:{author[:300]}"
            return document.source_id

        first_by_origin: dict[str, str] = {}
        for document in documents:
            first_by_origin.setdefault(origin_key(document), document.document_id)
        current_origin = origin_key(raw)
        return max(1, len(first_by_origin)), first_by_origin.get(current_origin) != raw.document_id
