"""Deterministic extraction of a small set of verified official facts."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from app.domain.enums import EvidenceDirection
from app.domain.evidence import (
    EvidenceDraft,
    EvidenceExtractionRequest,
    EvidenceExtractionResult,
)

PROVIDER_NAME = "official-rules"
MODEL_VERSION = "official-rules-1.0.0"

_RULES = (
    (
        re.compile(r"样本股数\s*(?P<value>\d{1,4})"),
        "market_snapshot",
        Decimal("0.95"),
        Decimal("0.9"),
        lambda match: f"官方事实表披露样本股数为{match.group('value')}。",
    ),
    (
        re.compile(r"指数代码[：:\s]+(?P<value>H30184)"),
        "index_methodology",
        Decimal("0.98"),
        Decimal("1"),
        lambda match: f"官方编制方案确认指数代码为{match.group('value')}。",
    ),
    (
        re.compile(r"单个样本权重不超过\s*(?P<value>15%)"),
        "index_weight_limit",
        Decimal("0.98"),
        Decimal("1"),
        lambda match: f"官方编制方案规定单个样本权重上限为{match.group('value')}。",
    ),
    (
        re.compile(r"基金份额拆分比例为\s*(?P<value>1:2)"),
        "fund_split",
        Decimal("1"),
        Decimal("1"),
        lambda match: f"官方公告确认基金份额拆分比例为{match.group('value')}。",
    ),
    (
        re.compile(r"基金代码\s+(?P<value>007300)"),
        "fund_identity",
        Decimal("0.98"),
        Decimal("0.8"),
        lambda match: f"官方产品资料确认联接基金代码为{match.group('value')}。",
    ),
    (
        re.compile(r"基金代码\s+(?P<value>512480)"),
        "fund_identity",
        Decimal("0.98"),
        Decimal("0.8"),
        lambda match: f"官方产品资料确认目标 ETF 代码为{match.group('value')}。",
    ),
)
_MICRON_NEWS = re.compile(
    r"(?P<title>Micron\s+.{20,220}?)\s+"
    r"(?P<date>(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\s+\d{1,2},\s+\d{4})\s+"
    r"(?P<summary>.{20,220}?)(?=\s+PDF Version)",
    re.IGNORECASE,
)


def extract_official_facts(
    request: EvidenceExtractionRequest,
    *,
    completed_at: datetime,
) -> EvidenceExtractionResult | None:
    """Return locally verified facts, or None when the document needs semantic extraction."""

    if request.source_kind not in {
        "OFFICIAL",
        "REGULATOR",
        "FUND_MANAGER",
        "COMPANY_OFFICIAL",
    }:
        return None
    searchable = f"{request.title}\n{request.summary or ''}\n{request.normalized_body}"
    evidence: list[EvidenceDraft] = []
    seen_claim_types: set[str] = set()
    for pattern, claim_type, confidence, directness, claim_builder in _RULES:
        match = pattern.search(searchable)
        if match is None or claim_type in seen_claim_types:
            continue
        quote = match.group(0)
        evidence.append(
            EvidenceDraft(
                claim=claim_builder(match),
                direction=EvidenceDirection.UNKNOWN,
                quote=quote,
                topic_ids=request.topic_ids,
                entity_ids=request.entity_ids,
                confidence=confidence,
                uncertainty="该事实本身不构成加仓或减仓方向。",
                claim_type=claim_type,
                impact_horizon="UNKNOWN",
                directness=directness,
            )
        )
        seen_claim_types.add(claim_type)
    micron = _MICRON_NEWS.search(searchable)
    if micron is not None:
        quote = micron.group(0)
        evidence.append(
            EvidenceDraft(
                claim=f"{micron.group('title').strip()}（{micron.group('date')}）",
                direction=EvidenceDirection.UNKNOWN,
                quote=quote,
                topic_ids=request.topic_ids,
                entity_ids=request.entity_ids,
                confidence=Decimal("0.9"),
                uncertainty="公司新闻是行业信号，不代表 007300 的实际持仓事件。",
                claim_type="company_news",
                impact_horizon="UNKNOWN",
                directness=Decimal("0.6"),
            )
        )
    if not evidence:
        return None
    return EvidenceExtractionResult(
        document_id=request.document_id,
        evidence=tuple(evidence),
        unknowns=("确定性抽取仅保留可由原文精确定位的官方事实。",),
        provider_name=PROVIDER_NAME,
        model_version=MODEL_VERSION,
        prompt_version=request.prompt_version,
        completed_at=completed_at,
        relevance=Decimal("1"),
        event_type=evidence[0].claim_type,
        related_topic_ids=request.topic_ids,
        related_entity_ids=request.entity_ids,
        source_is_primary=True,
        input_tokens=0,
        output_tokens=0,
        elapsed_ms=0,
    )
