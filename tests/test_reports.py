from __future__ import annotations

import asyncio
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select

from app.ai.synthesis import MockSynthesisProvider
from app.domain.analysis import ReportSource
from app.domain.contracts import ReportRenderRequest
from app.domain.enums import DocumentState, EvidenceDirection, ReportFormat
from app.reports.html import HTML_TEMPLATE_VERSION, HTMLReportRenderer, safe_external_url
from app.services.analysis_synthesis import AnalysisSynthesisService
from app.services.decision import DecisionRunService
from app.services.portfolio import PortfolioService
from app.services.reports import ReportService
from app.storage.models import EvidenceItemRow, ImmutableReportError, RawDocumentRow, ReportRow
from app.storage.repositories import RawDocumentRepository
from tests.domain_factories import investment_profile, report
from tests.test_analysis_synthesis import NOW, output
from tests.test_decision_policy import context
from tests.test_normalization import database, raw_document
from tests.test_portfolio_service import asset, position


def test_html_renderer_escapes_untrusted_text_and_secures_links() -> None:
    value = report().model_copy(
        update={
            "template_version": HTML_TEMPLATE_VERSION,
            "sources": (
                ReportSource(
                    evidence_id="evidence-1",
                    source_id="source-1",
                    title='<script>alert("source")</script>',
                    url="https://example.com/story?q=1",
                    health_status="HEALTHY",
                ),
            ),
            "analysis": report().analysis.model_copy(
                update={"summary": '<img src=x onerror="alert(1)">'}
            ),
        }
    )
    rendered = asyncio.run(
        HTMLReportRenderer().render(
            ReportRenderRequest(report=value, output_format=ReportFormat.HTML)
        )
    )
    html = rendered.content.decode("utf-8")
    assert '<script>alert("source")</script>' not in html
    assert "&lt;script&gt;alert" in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert 'rel="noopener noreferrer nofollow"' in html
    assert "javascript:" not in html
    assert "综合立场：</strong>" in html
    assert f">{value.analysis.stance}<" not in html
    assert "不是上涨概率" in html
    assert rendered.content_sha256 == sha256(rendered.content).hexdigest()


@pytest.mark.parametrize(
    "url",
    (
        "javascript:alert(1)",
        "https://user:password@example.com/private",
        "file:///C:/secret",
        "https://example.com:444/private",
    ),
)
def test_unsafe_report_links_are_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="URL"):
        safe_external_url(url)


def persisted_context():
    value = context((EvidenceDirection.POSITIVE, EvidenceDirection.NEGATIVE))
    evidence = tuple(
        item.model_copy(
            update={
                "document_id": f"report-doc-{index}",
                "draft": item.draft.model_copy(
                    update={"claim": f'<img src=x onerror="claim-{index}">'}
                ),
            }
        )
        for index, item in enumerate(value.evidence, start=1)
    )
    return value.model_copy(update={"evidence": evidence})


def test_report_service_persists_clickable_immutable_snapshot_and_diff(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            portfolio = PortfolioService(session, "personal")
            portfolio.create_profile(investment_profile())
            portfolio.create_asset(asset())
            portfolio.create_position(position())
            snapshot = portfolio.create_analysis_snapshot("position-007300", generated_at=NOW)
            value = persisted_context()
            raw_repository = RawDocumentRepository(session, "personal")
            for index, evidence in enumerate(value.evidence, start=1):
                raw_repository.add_if_absent(
                    raw_document(
                        evidence.document_id,
                        f"https://news.example/report-{index}",
                        f'<script>alert("title-{index}")</script>',
                        f"distinct report body {index}",
                        discovered_at=NOW,
                    )
                )
                raw = session.scalar(
                    select(RawDocumentRow).where(RawDocumentRow.document_id == evidence.document_id)
                )
                assert raw is not None
                raw.state = DocumentState.SCORED.value
                session.add(
                    EvidenceItemRow(
                        evidence_id=evidence.evidence_id,
                        workspace_id="personal",
                        document_id=evidence.document_id,
                        cluster_id=None,
                        schema_version=evidence.schema_version,
                        payload=evidence.model_dump(mode="json"),
                    )
                )
            DecisionRunService(session, "personal").run(
                value, position_snapshot_id=snapshot.snapshot_id, now=NOW
            )
            asyncio.run(
                AnalysisSynthesisService(
                    session,
                    "personal",
                    MockSynthesisProvider(output()),
                    model_version="mock-v1",
                ).synthesize_pending(now=NOW)
            )
            service = ReportService(session, "personal")
            assert asyncio.run(service.generate_pending(now=NOW)) == (1, 0)
            saved = session.scalar(select(ReportRow))
            assert saved is not None
            first, html = service.get(saved.report_id) or (None, None)
            assert first is not None and html is not None
            assert len(first.sources) == 2
            assert "https://news.example/report-1" in html
            assert "<script>alert" not in html
            assert "&lt;script&gt;alert" in html
            assert first.advisory_only is True
            states = session.scalars(select(RawDocumentRow.state)).all()
            assert set(states) == {DocumentState.PUBLISHED.value}

            second = first.model_copy(
                update={
                    "report_id": "report-history-2",
                    "template_version": "report-html-2.0.0",
                    "analysis": first.analysis.model_copy(update={"confidence": Decimal("0.1")}),
                }
            )
            session.add(
                ReportRow(
                    report_id=second.report_id,
                    workspace_id="personal",
                    analysis_run_id=saved.analysis_run_id,
                    pipeline_version=second.pipeline_version,
                    rule_version=second.rule_version,
                    prompt_version=second.prompt_version,
                    template_version=second.template_version,
                    media_type=saved.media_type,
                    content_sha256=saved.content_sha256,
                    rendered_content=saved.rendered_content,
                    generated_at=second.generated_at,
                    input_snapshot=saved.input_snapshot,
                    schema_version=second.schema_version,
                    payload=second.model_dump(mode="json"),
                )
            )
            session.flush()
            difference = service.diff(first.report_id, second.report_id)
            assert difference.decision_changed is False
            assert difference.confidence_change < 0

        with (
            pytest.raises(ImmutableReportError, match="cannot be updated"),
            db.session() as session,
        ):
            row = session.scalar(select(ReportRow).limit(1))
            assert row is not None
            row.rendered_content = "mutated"
            session.flush()
    finally:
        db.dispose()
