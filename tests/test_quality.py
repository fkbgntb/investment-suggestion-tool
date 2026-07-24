from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.domain.analysis import ReportDifference
from app.domain.enums import SuggestionLabel
from app.services.manual_pipeline import ManualPipelineOutcome
from app.services.quality import (
    build_shadow_record,
    collect_quality_metrics,
    record_shadow_audit,
    save_shadow_record,
)
from tests.domain_factories import report

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def crawl_outcome(
    *,
    failed: int = 0,
    report_outcome: str | None = None,
    report_reason: str | None = None,
    report_id: str | None = None,
) -> ManualPipelineOutcome:
    return ManualPipelineOutcome(
        source_count=2,
        failed_source_count=failed,
        new_document_count=3,
        normalized_count=3,
        duplicate_count=1,
        quarantined_count=0,
        relevant_count=1,
        review_count=1,
        irrelevant_count=1,
        extraction_count=1,
        extraction_review_count=0,
        scored_count=1,
        report_outcome=report_outcome,
        report_reason=report_reason,
        report_id=report_id,
    )


def test_quality_metrics_are_bounded_and_do_not_invent_costs() -> None:
    session = MagicMock()
    session.scalar.side_effect = [10, 4, 1, 8, 2, 5, 1, 4, 1]
    session.execute.return_value.one.return_value = (120, 40)

    metrics = collect_quality_metrics(session, "personal", now=NOW)

    assert metrics.irrelevant_ratio == Decimal("0.2500")
    assert metrics.duplicate_ratio == Decimal("0.2500")
    assert metrics.source_failure_ratio == Decimal("0.2000")
    assert metrics.ai_schema_failure_ratio == Decimal("0.2500")
    assert metrics.analysis_input_tokens == 120
    assert metrics.analysis_output_tokens == 40
    assert metrics.estimated_cost_cny is None


def test_shadow_record_tracks_changes_and_is_saved_outside_database(tmp_path) -> None:
    previous = report()
    current = report().model_copy(
        update={
            "report_id": "report-2",
            "decision": report().decision.model_copy(
                update={
                    "decision_id": "decision-2",
                    "label": SuggestionLabel.OBSERVE,
                    "allowed_labels": (SuggestionLabel.OBSERVE,),
                }
            ),
            "analysis": report().analysis.model_copy(
                update={
                    "analysis_id": "analysis-2",
                    "suggested_action": SuggestionLabel.OBSERVE,
                    "allowed_actions": (SuggestionLabel.OBSERVE,),
                    "degraded": True,
                }
            ),
        }
    )
    session = MagicMock()
    session.scalar.side_effect = [0] * 9
    session.execute.return_value.one.return_value = (0, 0)
    metrics = collect_quality_metrics(session, "personal", now=NOW)
    difference = ReportDifference(
        older_report_id=previous.report_id,
        newer_report_id=current.report_id,
        decision_changed=True,
        older_label=SuggestionLabel.HOLD,
        newer_label=SuggestionLabel.OBSERVE,
        confidence_change=Decimal("-0.1"),
        added_evidence_ids=("evidence-2",),
    )

    record = build_shadow_record(
        position_id="position-007300",
        report=current,
        previous_report=previous,
        difference=difference,
        crawl=crawl_outcome(failed=1),
        metrics=metrics,
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=1),
    )
    path = save_shadow_record(record, tmp_path)

    assert record.status == "DEGRADED"
    assert record.decision_changed is True
    assert "decision label changed" in record.change_reasons
    assert path.parent == tmp_path / "shadow-runs"
    assert json.loads(path.read_text("utf-8"))["advisory_only"] is True
    assert (path.parent / "latest.json").read_bytes() == path.read_bytes()
    assert save_shadow_record(record, tmp_path) == path
    changed = record.model_copy(update={"status": "SUCCEEDED"})
    with pytest.raises(ValueError, match="cannot be overwritten"):
        save_shadow_record(changed, tmp_path)


def test_first_shadow_record_and_audit_are_advisory_only(monkeypatch) -> None:
    session = MagicMock()
    session.scalar.side_effect = [0] * 9
    session.execute.return_value.one.return_value = (0, 0)
    metrics = collect_quality_metrics(session, "personal", now=NOW)
    record = build_shadow_record(
        position_id="position-007300",
        report=report(),
        previous_report=None,
        difference=None,
        crawl=crawl_outcome(),
        metrics=metrics,
        started_at=NOW,
        finished_at=NOW,
    )
    captured: dict[str, object] = {}

    class FakeAuditRepository:
        def __init__(self, *args):
            pass

        def record(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("app.services.quality.AuditRepository", FakeAuditRepository)
    record_shadow_audit(session, "personal", record)

    assert record.status == "SUCCEEDED"
    assert record.change_reasons == ("initial shadow record",)
    assert record.advisory_only is True
    assert captured["details"] == {
        "shadow_run_id": record.shadow_run_id,
        "decision_label": "HOLD",
        "decision_changed": False,
        "failed_source_count": 0,
        "advisory_only": True,
    }


def test_shadow_record_reuses_latest_report_when_trigger_skips() -> None:
    current = report()
    session = MagicMock()
    session.scalar.side_effect = [0] * 9
    session.execute.return_value.one.return_value = (0, 0)
    metrics = collect_quality_metrics(session, "personal", now=NOW)
    record = build_shadow_record(
        position_id="position-007300",
        report=current,
        previous_report=current,
        difference=None,
        crawl=crawl_outcome(
            report_outcome="SKIPPED_NO_NEW_EVIDENCE",
            report_reason="no new effective evidence",
        ),
        metrics=metrics,
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=1),
    )

    assert record.report_id == current.report_id
    assert record.decision_changed is False
    assert record.change_reasons == (
        "no new report: SKIPPED_NO_NEW_EVIDENCE",
        "no new effective evidence",
    )
