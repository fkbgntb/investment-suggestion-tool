"""Select still-valid evidence and recompute time-sensitive score components."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.enums import SourceKind, TrustTier
from app.domain.evidence import Evidence, EvidenceScore
from app.domain.portfolio import Position
from app.services.decision import DECISION_RULE_VERSION
from app.storage.models import EvidenceItemRow, EvidenceScoreRow, RawDocumentRow, ReportRow

_HALF_LIFE_DAYS = {
    "SHORT": Decimal("3"),
    "MEDIUM": Decimal("14"),
    "LONG": Decimal("60"),
    "UNKNOWN": Decimal("7"),
}
_ACTIONABLE_TRUST = {TrustTier.PRIMARY, TrustTier.PROFESSIONAL}
_FOUR_PLACES = Decimal("0.0001")
EFFECTIVE_SCORING_VERSION = "effective-freshness-1.0.0"


@dataclass(frozen=True)
class EvidenceSelection:
    evidence: tuple[Evidence, ...]
    scores: tuple[EvidenceScore, ...]
    data_as_of: datetime
    new_evidence_ids: tuple[str, ...]
    actionable_evidence_ids: tuple[str, ...]
    expired_evidence_count: int
    fingerprint: str

    @property
    def aggregator_only(self) -> bool:
        return bool(self.evidence) and all(
            score.source_kind is SourceKind.AGGREGATOR for score in self.scores
        )


def _unit(value: Decimal) -> Decimal:
    return min(Decimal("1"), max(Decimal("0"), value)).quantize(
        _FOUR_PLACES, rounding=ROUND_HALF_UP
    )


class EffectiveEvidenceSelector:
    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def select(
        self,
        *,
        now: datetime,
        position: Position,
        report_date: date,
        limit: int = 100,
    ) -> EvidenceSelection:
        if now.tzinfo is None:
            raise ValueError("evidence selection time must include a timezone")
        normalized_now = now.astimezone(UTC)
        rows = self.session.execute(
            select(EvidenceItemRow, EvidenceScoreRow, RawDocumentRow)
            .join(
                EvidenceScoreRow,
                (EvidenceScoreRow.workspace_id == EvidenceItemRow.workspace_id)
                & (EvidenceScoreRow.evidence_id == EvidenceItemRow.evidence_id),
            )
            .join(
                RawDocumentRow,
                (RawDocumentRow.workspace_id == EvidenceItemRow.workspace_id)
                & (RawDocumentRow.document_id == EvidenceItemRow.document_id),
            )
            .where(EvidenceItemRow.workspace_id == self.workspace_id)
            .order_by(EvidenceScoreRow.created_at.desc(), EvidenceItemRow.evidence_id)
        ).all()
        latest: dict[str, tuple[Evidence, EvidenceScore, datetime]] = {}
        expired = 0
        for evidence_row, score_row, raw_row in rows:
            if evidence_row.evidence_id in latest:
                continue
            evidence = Evidence.model_validate(evidence_row.payload)
            published_at = raw_row.published_at or raw_row.fetched_at
            effective = self._effective_score(
                EvidenceScore.model_validate(score_row.payload),
                impact_horizon=evidence.draft.impact_horizon,
                published_at=published_at,
                now=normalized_now,
            )
            if effective is None:
                expired += 1
                continue
            latest[evidence.evidence_id] = (evidence, effective, published_at)
            if len(latest) >= limit:
                break
        selected = tuple(latest.values())
        evidence = tuple(item[0] for item in selected)
        scores = tuple(item[1] for item in selected)
        data_as_of = max(
            (item[2].astimezone(UTC) for item in selected),
            default=normalized_now,
        )
        previous_ids = self._latest_report_evidence_ids()
        current_ids = tuple(item.evidence_id for item in evidence)
        new_ids = tuple(item for item in current_ids if item not in previous_ids)
        actionable_ids = tuple(
            score.evidence_id for score in scores if score.trust_tier in _ACTIONABLE_TRUST
        )
        fingerprint = self._fingerprint(
            position=position,
            report_date=report_date,
            evidence=evidence,
            scores=scores,
        )
        return EvidenceSelection(
            evidence=evidence,
            scores=scores,
            data_as_of=data_as_of,
            new_evidence_ids=new_ids,
            actionable_evidence_ids=actionable_ids,
            expired_evidence_count=expired,
            fingerprint=fingerprint,
        )

    @staticmethod
    def _effective_score(
        score: EvidenceScore,
        *,
        impact_horizon: str,
        published_at: datetime,
        now: datetime,
    ) -> EvidenceScore | None:
        half_life = _HALF_LIFE_DAYS[impact_horizon]
        age_seconds = max(
            0,
            (now - published_at.astimezone(UTC)).total_seconds(),
        )
        age_days = Decimal(str(age_seconds / 86_400))
        if age_days > half_life * 4:
            return None
        recency = _unit(Decimal(str(0.5 ** float(age_days / half_life))))
        total = _unit(
            score.source_quality
            * score.independence
            * recency
            * score.relevance
            * score.directness
            * score.extraction_confidence
        )
        if score.confidence_cap is not None:
            total = min(total, score.confidence_cap)
        return score.model_copy(
            update={
                "recency": recency,
                "total": _unit(total),
                "scoring_version": EFFECTIVE_SCORING_VERSION,
                "scored_at": now,
                "component_reasons": (
                    *score.component_reasons,
                    f"按报告时间重新计算时效，半衰期 {half_life} 天",
                ),
            }
        )

    def _latest_report_evidence_ids(self) -> set[str]:
        latest = self.session.scalar(
            select(ReportRow)
            .where(ReportRow.workspace_id == self.workspace_id)
            .order_by(ReportRow.generated_at.desc(), ReportRow.report_id.desc())
            .limit(1)
        )
        if latest is None:
            return set()
        return {
            str(value)
            for value in latest.input_snapshot.get("evidence_ids", ())
            if isinstance(value, str)
        }

    @staticmethod
    def _fingerprint(
        *,
        position: Position,
        report_date: date,
        evidence: tuple[Evidence, ...],
        scores: tuple[EvidenceScore, ...],
    ) -> str:
        payload = {
            "position": position.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "scores": [item.model_dump(mode="json") for item in scores],
            "rule_version": DECISION_RULE_VERSION,
            "report_date": report_date.isoformat(),
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
