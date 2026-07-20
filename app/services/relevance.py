"""Low-cost, explainable relevance screening before any AI call."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.documents import NormalizedDocument
from app.domain.enums import DocumentState, RelevanceLabel, TaxonomyNodeKind
from app.domain.relevance import HumanRelevanceLabel, RelevanceAssessment, RelevanceRuleHit
from app.domain.taxonomy import TaxonomyConfiguration
from app.storage.models import (
    HumanRelevanceLabelRow,
    NormalizedDocumentRow,
    RawDocumentRow,
    RelevanceAssessmentRow,
)
from app.storage.repositories import AuditRepository, TaxonomyRepository

RULE_VERSION = "semiconductor-keywords-1.0.0"
_AMBIGUOUS_TERMS = {"memory"}
_AMBIGUOUS_CONTEXT = {
    "chip",
    "semiconductor",
    "dram",
    "nand",
    "hbm",
    "wafer",
    "foundry",
    "存储芯片",
    "半导体",
    "晶圆",
}
_EXCLUSION_PHRASES = {
    "human memory",
    "working memory",
    "memory care",
    "memory foam",
    "in memory of",
}


class RelevanceClassifier(Protocol):
    classifier_name: str
    rule_version: str

    def assess(
        self,
        document: NormalizedDocument,
        taxonomy: TaxonomyConfiguration,
        *,
        assessed_at: datetime,
    ) -> RelevanceAssessment: ...


def _contains(text: str, term: str) -> bool:
    folded_term = term.casefold().strip()
    if not folded_term:
        return False
    if all(character.isascii() for character in folded_term):
        return re.search(rf"(?<!\w){re.escape(folded_term)}(?!\w)", text) is not None
    return folded_term in text


class RuleBasedRelevanceClassifier:
    classifier_name = "rule-based-relevance"

    def __init__(
        self,
        *,
        relevant_threshold: Decimal = Decimal("0.35"),
        review_threshold: Decimal = Decimal("0.15"),
        rule_version: str = RULE_VERSION,
    ) -> None:
        self.relevant_threshold = relevant_threshold
        self.review_threshold = review_threshold
        self.rule_version = rule_version

    def assess(
        self,
        document: NormalizedDocument,
        taxonomy: TaxonomyConfiguration,
        *,
        assessed_at: datetime,
    ) -> RelevanceAssessment:
        text = f"{document.title}\n{document.summary or ''}\n{document.body}".casefold()
        relevant_topics = {item.topic_id for item in taxonomy.exposures if item.enabled}
        relevant_entities = {
            item.entity_id for item in taxonomy.exposures if item.enabled and item.entity_id
        }
        entity_topics: dict[str, set[str]] = {}
        for relation in taxonomy.influence_relations:
            if (
                relation.enabled
                and relation.source_kind is TaxonomyNodeKind.ENTITY
                and relation.target_kind is TaxonomyNodeKind.TOPIC
                and relation.target_id in relevant_topics
            ):
                relevant_entities.add(relation.source_id)
                entity_topics.setdefault(relation.source_id, set()).add(relation.target_id)

        hits: list[RelevanceRuleHit] = []
        topic_hits: set[str] = set()
        entity_hits: set[str] = set()
        contextual = any(_contains(text, term) for term in _AMBIGUOUS_CONTEXT)
        for topic in taxonomy.topics:
            if not topic.enabled or topic.topic_id not in relevant_topics:
                continue
            base_terms = (topic.name, *topic.aliases, *topic.keywords)
            if topic.topic_id.endswith(".memory"):
                base_terms = (*base_terms, "memory")
            terms = tuple({term.casefold(): term for term in base_terms}.values())
            for term in terms:
                if term.casefold() in _AMBIGUOUS_TERMS and not contextual:
                    continue
                if not _contains(text, term):
                    continue
                points = Decimal("0.25") if term == topic.name else Decimal("0.15")
                hits.append(
                    RelevanceRuleHit(
                        rule_type="topic_term",
                        term=term,
                        target_id=topic.topic_id,
                        points=points,
                    )
                )
                topic_hits.add(topic.topic_id)

        for entity in taxonomy.entities:
            if not entity.enabled or entity.entity_id not in relevant_entities:
                continue
            terms = tuple(
                {term.casefold(): term for term in (entity.name, *entity.aliases)}.values()
            )
            for term in terms:
                if not _contains(text, term):
                    continue
                hits.append(
                    RelevanceRuleHit(
                        rule_type="entity_term",
                        term=term,
                        target_id=entity.entity_id,
                        points=Decimal("0.25"),
                    )
                )
                entity_hits.add(entity.entity_id)
                topic_hits.update(entity_topics.get(entity.entity_id, ()))
                break

        excluded = tuple(sorted(term for term in _EXCLUSION_PHRASES if _contains(text, term)))
        score = min(Decimal("1"), sum((hit.points for hit in hits), start=Decimal("0")))
        reasons: list[str] = []
        if excluded and score < self.relevant_threshold:
            score = Decimal("0")
            reasons.append(f"排除语境命中：{', '.join(excluded)}")
        if len(entity_hits) > 50:
            label = RelevanceLabel.REVIEW
            reasons.append("异常实体数量过多，需要人工复核")
        elif score >= self.relevant_threshold:
            label = RelevanceLabel.RELEVANT
            reasons.append("主题或产业链实体命中达到相关阈值")
        elif score >= self.review_threshold:
            label = RelevanceLabel.REVIEW
            reasons.append("存在弱相关信号，但不足以自动进入 AI 提取")
        else:
            label = RelevanceLabel.IRRELEVANT
            reasons.append("未命中足够的半导体主题或产业链信号")
        assessment_key = f"{document.document_id}:{self.rule_version}:{taxonomy.config_version}"
        return RelevanceAssessment(
            assessment_id=str(uuid5(NAMESPACE_URL, assessment_key)),
            document_id=document.document_id,
            label=label,
            score=score,
            topic_ids=tuple(sorted(topic_hits)),
            entity_ids=tuple(sorted(entity_hits)),
            hits=tuple(hits[:500]),
            reasons=tuple(reasons),
            rule_version=self.rule_version,
            taxonomy_version=taxonomy.config_version,
            assessed_at=assessed_at,
        )


class RelevanceService:
    def __init__(
        self,
        session: Session,
        workspace_id: str,
        classifier: RelevanceClassifier | None = None,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.classifier = classifier or RuleBasedRelevanceClassifier()
        self.audit = AuditRepository(session, workspace_id)

    def classify_pending(self, *, now: datetime, limit: int = 500) -> tuple[int, int, int]:
        taxonomy = TaxonomyRepository(self.session, self.workspace_id).get_active()
        if taxonomy is None:
            raise RuntimeError("an active taxonomy configuration is required")
        rows = self.session.scalars(
            select(NormalizedDocumentRow)
            .outerjoin(
                RelevanceAssessmentRow,
                (RelevanceAssessmentRow.workspace_id == NormalizedDocumentRow.workspace_id)
                & (RelevanceAssessmentRow.document_id == NormalizedDocumentRow.document_id)
                & (RelevanceAssessmentRow.rule_version == self.classifier.rule_version)
                & (RelevanceAssessmentRow.taxonomy_version == taxonomy.config_version),
            )
            .where(
                NormalizedDocumentRow.workspace_id == self.workspace_id,
                RelevanceAssessmentRow.assessment_id.is_(None),
            )
            .order_by(NormalizedDocumentRow.normalized_at, NormalizedDocumentRow.document_id)
            .limit(limit)
        ).all()
        relevant = review = irrelevant = 0
        for row in rows:
            document = NormalizedDocument.model_validate(row.payload)
            assessment = self.classifier.assess(document, taxonomy, assessed_at=now)
            self.session.add(
                RelevanceAssessmentRow(
                    assessment_id=assessment.assessment_id,
                    workspace_id=self.workspace_id,
                    document_id=document.document_id,
                    label=assessment.label.value,
                    score=str(assessment.score),
                    rule_version=assessment.rule_version,
                    taxonomy_version=assessment.taxonomy_version,
                    assessed_at=assessment.assessed_at,
                    schema_version=assessment.schema_version,
                    payload=assessment.model_dump(mode="json"),
                )
            )
            raw = self.session.scalar(
                select(RawDocumentRow).where(
                    RawDocumentRow.workspace_id == self.workspace_id,
                    RawDocumentRow.document_id == document.document_id,
                )
            )
            if raw is not None:
                raw.state = DocumentState.CLASSIFIED.value
                raw.state_version += 1
                raw.updated_at = now
            relevant += assessment.label is RelevanceLabel.RELEVANT
            review += assessment.label is RelevanceLabel.REVIEW
            irrelevant += assessment.label is RelevanceLabel.IRRELEVANT
        self.session.flush()
        return relevant, review, irrelevant

    def add_human_label(
        self,
        document_id: str,
        label: RelevanceLabel,
        *,
        note: str | None,
        now: datetime,
    ) -> HumanRelevanceLabel:
        document = self.session.scalar(
            select(NormalizedDocumentRow).where(
                NormalizedDocumentRow.workspace_id == self.workspace_id,
                NormalizedDocumentRow.document_id == document_id,
            )
        )
        if document is None:
            raise LookupError("normalized document was not found")
        value = HumanRelevanceLabel(
            label_id=str(uuid4()),
            document_id=document_id,
            label=label,
            note=note,
            labeled_at=now,
        )
        self.session.add(
            HumanRelevanceLabelRow(
                label_id=value.label_id,
                workspace_id=self.workspace_id,
                document_id=document_id,
                label=label.value,
                labeled_at=now,
                schema_version=value.schema_version,
                payload=value.model_dump(mode="json"),
            )
        )
        self.audit.record(
            event_type="human_relevance_labeled",
            actor="local_user",
            target_type="document",
            target_id=document_id,
            outcome="completed",
            details={"label": label.value, "has_note": note is not None},
        )
        self.session.flush()
        return value
