from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.domain.collection import SourceHealthSnapshot
from app.domain.enums import SourceHealthStatus
from app.services.scheduler import (
    DurableJobScheduler,
    SchedulerRunOutcome,
    WindowCollectionResult,
    floor_to_interval,
    is_source_stale,
    plan_windows,
)
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.models import ScheduledTaskRow, SchedulerStateRow
from app.storage.repositories import SchedulerStateRepository, WorkspaceRepository


def database(tmp_path: Path) -> Database:
    url = f"sqlite:///{(tmp_path / 'scheduler.sqlite3').as_posix()}"
    upgrade_database(url)
    db = Database(url)
    with db.session() as session:
        WorkspaceRepository(session).create("personal", "Personal")
    return db


def test_window_planning_is_utc_aligned_and_backfill_is_bounded() -> None:
    now = datetime(2026, 7, 20, 10, 37, tzinfo=UTC)
    assert floor_to_interval(now) == datetime(2026, 7, 20, 9, tzinfo=UTC)
    windows = plan_windows(datetime(2026, 7, 18, tzinfo=UTC), now, maximum_windows=3)
    assert windows == (
        (datetime(2026, 7, 20, 0, tzinfo=UTC), datetime(2026, 7, 20, 3, tzinfo=UTC)),
        (datetime(2026, 7, 20, 3, tzinfo=UTC), datetime(2026, 7, 20, 6, tzinfo=UTC)),
        (datetime(2026, 7, 20, 6, tzinfo=UTC), datetime(2026, 7, 20, 9, tzinfo=UTC)),
    )


def test_database_lease_allows_only_one_owner(tmp_path: Path) -> None:
    db = database(tmp_path)
    now = datetime(2026, 7, 20, 9, tzinfo=UTC)
    try:
        with db.session() as session:
            acquired = SchedulerStateRepository(session, "personal").acquire(
                "crawl-sources",
                owner="owner-1",
                now=now,
                lease_until=now + timedelta(hours=1),
                next_due_at=now,
            )
            assert acquired is not None
        with db.session() as session:
            blocked = SchedulerStateRepository(session, "personal").acquire(
                "crawl-sources",
                owner="owner-2",
                now=now,
                lease_until=now + timedelta(hours=1),
                next_due_at=now,
            )
            assert blocked is None
    finally:
        db.dispose()


def test_scheduler_persists_restart_state_tasks_and_daily_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        db = database(tmp_path)
        calls: list[tuple[datetime, datetime]] = []
        cleanups: list[datetime] = []

        async def runner(since: datetime, until: datetime) -> WindowCollectionResult:
            calls.append((since, until))
            return WindowCollectionResult(created_count=2, source_count=2)

        def cleanup(now: datetime) -> int:
            cleanups.append(now)
            return 3

        now = datetime(2026, 7, 20, 9, 5, tzinfo=UTC)
        try:
            first = await DurableJobScheduler(db, "personal").run_due(
                now=now,
                runner=runner,
                cleanup=cleanup,
            )
            assert first == SchedulerRunOutcome(
                status="SUCCEEDED",
                window_count=1,
                created_count=2,
                processing_tasks=1,
                daily_summary_task=True,
                purged_body_count=3,
            )
            restarted = await DurableJobScheduler(db, "personal").run_due(
                now=now + timedelta(minutes=10),
                runner=runner,
                cleanup=cleanup,
            )
            assert restarted.status == "NOT_DUE"
            assert len(calls) == 1
            assert len(cleanups) == 1
            with db.session() as session:
                assert session.scalar(select(func.count()).select_from(ScheduledTaskRow)) == 2
                state = session.scalar(select(SchedulerStateRow))
                assert state is not None
                assert state.next_due_at == datetime(2026, 7, 20, 12, tzinfo=UTC)
        finally:
            db.dispose()

    asyncio.run(scenario())


def test_no_new_documents_means_no_processing_or_ai_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        db = database(tmp_path)

        async def runner(_since: datetime, _until: datetime) -> WindowCollectionResult:
            return WindowCollectionResult(created_count=0, source_count=2, failed_source_count=1)

        try:
            outcome = await DurableJobScheduler(db, "personal").run_due(
                now=datetime(2026, 7, 20, 9, 1, tzinfo=UTC),
                runner=runner,
                cleanup=lambda _now: 0,
            )
            assert outcome.processing_tasks == 0
            assert outcome.failed_source_count == 1
            with db.session() as session:
                task_types = tuple(session.scalars(select(ScheduledTaskRow.task_type)))
                assert task_types == ("daily-summary",)
        finally:
            db.dispose()

    asyncio.run(scenario())


def test_staleness_uses_eight_hour_threshold_and_ignores_disabled() -> None:
    now = datetime(2026, 7, 20, 12, tzinfo=UTC)
    stale = SourceHealthSnapshot(
        source_id="source-1",
        status=SourceHealthStatus.DEGRADED,
        consecutive_failures=1,
        last_success_at=now - timedelta(hours=9),
    )
    disabled = stale.model_copy(update={"status": SourceHealthStatus.DISABLED})
    assert is_source_stale(stale, now) is True
    assert is_source_stale(disabled, now) is False
