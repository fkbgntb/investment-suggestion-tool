"""Print non-sensitive local pipeline counts for troubleshooting."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from app.config import Settings
from app.storage.database import Database
from app.storage.models import (
    AIExtractionRunRow,
    EvidenceItemRow,
    EvidenceScoreRow,
    NormalizedDocumentRow,
    RawDocumentRow,
    RelevanceAssessmentRow,
    ReportRow,
    ScheduledTaskRow,
    SourceRow,
)


def main() -> int:
    settings = Settings()
    database = Database(settings.database_url)
    try:
        with database.session() as session:
            task_counts = session.execute(
                select(
                    ScheduledTaskRow.task_type,
                    ScheduledTaskRow.status,
                    func.count(),
                )
                .where(ScheduledTaskRow.workspace_id == settings.portfolio_workspace_id)
                .group_by(ScheduledTaskRow.task_type, ScheduledTaskRow.status)
                .order_by(ScheduledTaskRow.task_type, ScheduledTaskRow.status)
            ).all()
            counts = {
                "documents": session.scalar(
                    select(func.count())
                    .select_from(RawDocumentRow)
                    .where(RawDocumentRow.workspace_id == settings.portfolio_workspace_id)
                ),
                "evidence": session.scalar(
                    select(func.count())
                    .select_from(EvidenceItemRow)
                    .where(EvidenceItemRow.workspace_id == settings.portfolio_workspace_id)
                ),
                "scores": session.scalar(
                    select(func.count())
                    .select_from(EvidenceScoreRow)
                    .where(EvidenceScoreRow.workspace_id == settings.portfolio_workspace_id)
                ),
                "reports": session.scalar(
                    select(func.count())
                    .select_from(ReportRow)
                    .where(ReportRow.workspace_id == settings.portfolio_workspace_id)
                ),
            }
            latest_tasks = session.execute(
                select(
                    ScheduledTaskRow.task_type,
                    ScheduledTaskRow.status,
                    ScheduledTaskRow.finished_at,
                )
                .where(ScheduledTaskRow.workspace_id == settings.portfolio_workspace_id)
                .order_by(ScheduledTaskRow.created_at.desc())
                .limit(8)
            ).all()
            latest_report = session.scalar(
                select(ReportRow)
                .where(ReportRow.workspace_id == settings.portfolio_workspace_id)
                .order_by(ReportRow.generated_at.desc())
                .limit(1)
            )
            source_documents = session.execute(
                select(
                    SourceRow.source_id,
                    RawDocumentRow.state,
                    func.count(),
                )
                .join(
                    RawDocumentRow,
                    (RawDocumentRow.workspace_id == SourceRow.workspace_id)
                    & (RawDocumentRow.source_id == SourceRow.source_id),
                )
                .where(SourceRow.workspace_id == settings.portfolio_workspace_id)
                .group_by(SourceRow.source_id, RawDocumentRow.state)
                .order_by(SourceRow.source_id, RawDocumentRow.state)
            ).all()
            official_assessments = session.execute(
                select(
                    RawDocumentRow.source_id,
                    RawDocumentRow.title,
                    RelevanceAssessmentRow.label,
                    RelevanceAssessmentRow.score,
                )
                .join(
                    RelevanceAssessmentRow,
                    (RelevanceAssessmentRow.workspace_id == RawDocumentRow.workspace_id)
                    & (RelevanceAssessmentRow.document_id == RawDocumentRow.document_id),
                )
                .where(
                    RawDocumentRow.workspace_id == settings.portfolio_workspace_id,
                    RawDocumentRow.source_id != "alpha-vantage-news",
                )
                .order_by(RawDocumentRow.source_id)
            ).all()
            official_previews = session.execute(
                select(
                    RawDocumentRow.source_id,
                    RawDocumentRow.raw_body,
                )
                .where(
                    RawDocumentRow.workspace_id == settings.portfolio_workspace_id,
                    RawDocumentRow.source_id != "alpha-vantage-news",
                )
                .order_by(RawDocumentRow.source_id)
            ).all()
            today = datetime.now(UTC).date()
            extraction_runs = session.scalars(
                select(AIExtractionRunRow).where(
                    AIExtractionRunRow.workspace_id == settings.portfolio_workspace_id
                )
            ).all()
            extraction_today = [
                row for row in extraction_runs if row.completed_at.astimezone(UTC).date() == today
            ]
            extraction_status = session.execute(
                select(
                    RawDocumentRow.source_id,
                    AIExtractionRunRow.status,
                    AIExtractionRunRow.error_code,
                )
                .join(
                    AIExtractionRunRow,
                    (AIExtractionRunRow.workspace_id == RawDocumentRow.workspace_id)
                    & (AIExtractionRunRow.document_id == RawDocumentRow.document_id),
                )
                .where(
                    RawDocumentRow.workspace_id == settings.portfolio_workspace_id,
                    RawDocumentRow.source_id != "alpha-vantage-news",
                )
                .order_by(RawDocumentRow.source_id)
            ).all()
            micron_normalized = session.scalar(
                select(NormalizedDocumentRow).where(
                    NormalizedDocumentRow.workspace_id == settings.portfolio_workspace_id,
                    NormalizedDocumentRow.source_id == "micron-ir-news",
                )
            )
    finally:
        database.dispose()

    print("task_counts:")
    for task_type, status, count in task_counts:
        print(f"  {task_type}: {status}={count}")
    for name, count in counts.items():
        print(f"{name}: {count}")
    print("latest_tasks:")
    for task_type, status, finished_at in latest_tasks:
        print(f"  {task_type}: {status}; finished_at={finished_at}")
    print("source_documents:")
    for source_id, state, count in source_documents:
        print(f"  {source_id}: {state}={count}")
    print("official_assessments:")
    for source_id, title, label, score in official_assessments:
        print(f"  {source_id}: {label}/{score}; title={title[:120]}")
    print(f"ai_extraction_calls_today: {len(extraction_today)}")
    print("official_extraction_status:")
    for source_id, status, error_code in extraction_status:
        print(f"  {source_id}: {status}; error={error_code}")
    print("official_document_previews:")
    for source_id, body in official_previews:
        compact = " ".join((body or "").split())
        print(f"  {source_id}: {compact[:400]}")
    if latest_report is not None:
        decision = latest_report.payload.get("decision", {})
        evidence_ids = latest_report.input_snapshot.get("evidence_ids", [])
        print(
            "latest_report: "
            f"action={decision.get('label', 'UNKNOWN')}; "
            f"evidence_ids={len(evidence_ids)}"
        )
    if micron_normalized is not None:
        normalized_body = str(micron_normalized.payload.get("body", ""))
        print(f"micron_normalized_preview: {' '.join(normalized_body.split())[:1_500]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
