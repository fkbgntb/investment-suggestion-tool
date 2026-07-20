"""Durable three-hour scheduler independent of any process scheduler library."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.domain.base import IdempotencyKey
from app.domain.collection import SourceHealthSnapshot
from app.domain.contracts import DispatchReceipt, TaskRequest
from app.domain.enums import SourceHealthStatus
from app.storage.database import Database
from app.storage.repositories import SchedulerStateRepository, TaskQueueRepository

JOB_NAME = "crawl-sources"


@dataclass(frozen=True)
class WindowCollectionResult:
    created_count: int
    source_count: int
    failed_source_count: int = 0


@dataclass(frozen=True)
class SchedulerRunOutcome:
    status: str
    window_count: int = 0
    created_count: int = 0
    failed_source_count: int = 0
    processing_tasks: int = 0
    daily_summary_task: bool = False
    purged_body_count: int = 0


WindowRunner = Callable[[datetime, datetime], Awaitable[WindowCollectionResult]]
CleanupRunner = Callable[[datetime], int]


def is_source_stale(
    snapshot: SourceHealthSnapshot,
    now: datetime,
    *,
    threshold_hours: int = 8,
) -> bool:
    if snapshot.status is SourceHealthStatus.DISABLED:
        return False
    if snapshot.last_success_at is None:
        return True
    return now.astimezone(UTC) - snapshot.last_success_at.astimezone(UTC) > timedelta(
        hours=threshold_hours
    )


def floor_to_interval(value: datetime, interval_hours: int = 3) -> datetime:
    normalized = value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return normalized.replace(hour=normalized.hour - normalized.hour % interval_hours)


def plan_windows(
    last_completed_at: datetime | None,
    now: datetime,
    *,
    interval_hours: int = 3,
    maximum_windows: int = 8,
) -> tuple[tuple[datetime, datetime], ...]:
    boundary = floor_to_interval(now, interval_hours)
    interval = timedelta(hours=interval_hours)
    start = (
        last_completed_at.astimezone(UTC) if last_completed_at is not None else boundary - interval
    )
    if start > boundary:
        return ()
    oldest_allowed = boundary - interval * maximum_windows
    start = max(start, oldest_allowed)
    windows: list[tuple[datetime, datetime]] = []
    while start < boundary and len(windows) < maximum_windows:
        end = min(start + interval, boundary)
        windows.append((start, end))
        start = end
    return tuple(windows)


class DatabaseTaskDispatcher:
    dispatcher_name = "database-task-queue"

    def __init__(self, database: Database, workspace_id: str) -> None:
        self.database = database
        self.workspace_id = workspace_id

    async def enqueue(self, request: TaskRequest) -> DispatchReceipt:
        with self.database.session() as session:
            created = TaskQueueRepository(session, self.workspace_id).enqueue(
                scope=request.idempotency.scope,
                key=request.idempotency.key,
                payload_sha256=request.idempotency.payload_sha256,
                task_id=request.task_id,
                task_type=request.task_type,
                payload=dict(request.payload),
                not_before=request.not_before,
            )
        return DispatchReceipt(
            task_id=request.task_id,
            accepted=True,
            duplicate=not created,
            dispatch_reference=f"database:{request.task_id}",
        )


class DurableJobScheduler:
    def __init__(
        self,
        database: Database,
        workspace_id: str,
        *,
        interval_hours: int = 3,
        lease_minutes: int = 60,
        maximum_backfill_windows: int = 8,
    ) -> None:
        self.database = database
        self.workspace_id = workspace_id
        self.interval_hours = interval_hours
        self.lease_minutes = lease_minutes
        self.maximum_backfill_windows = maximum_backfill_windows
        self.dispatcher = DatabaseTaskDispatcher(database, workspace_id)

    async def run_due(
        self,
        *,
        now: datetime,
        runner: WindowRunner,
        cleanup: CleanupRunner,
        force: bool = False,
    ) -> SchedulerRunOutcome:
        now = now.astimezone(UTC)
        boundary = floor_to_interval(now, self.interval_hours)
        owner = str(uuid4())
        with self.database.session() as session:
            repository = SchedulerStateRepository(session, self.workspace_id)
            state = repository.get_or_create(
                JOB_NAME,
                now=now,
                next_due_at=boundary,
            )
            if not force and now < state.next_due_at:
                return SchedulerRunOutcome(status="NOT_DUE")
            state = repository.acquire(
                JOB_NAME,
                owner=owner,
                now=now,
                lease_until=now + timedelta(minutes=self.lease_minutes),
                next_due_at=boundary,
            )
            if state is None:
                return SchedulerRunOutcome(status="LOCKED")
            windows = plan_windows(
                state.last_completed_at,
                now,
                interval_hours=self.interval_hours,
                maximum_windows=self.maximum_backfill_windows,
            )
            last_summary_date = state.last_summary_date
            last_cleanup_date = state.last_cleanup_date

        created_count = 0
        failed_source_count = 0
        processing_tasks = 0
        summary_queued = False
        purged_count = 0
        today = now.date().isoformat()
        completed_through = state.last_completed_at or boundary
        try:
            for since, until in windows:
                result = await runner(since, until)
                created_count += result.created_count
                failed_source_count += result.failed_source_count
                completed_through = until
                if result.created_count:
                    await self._enqueue_task(
                        "process-new-documents",
                        until,
                        {
                            "window_start": since.isoformat(),
                            "window_end": until.isoformat(),
                            "new_document_count": result.created_count,
                        },
                    )
                    processing_tasks += 1
            if last_cleanup_date != today:
                purged_count = cleanup(now)
            if last_summary_date != today:
                await self._enqueue_task(
                    "daily-summary",
                    now,
                    {"summary_date": today},
                )
                summary_queued = True
            with self.database.session() as session:
                completed = SchedulerStateRepository(session, self.workspace_id).complete(
                    JOB_NAME,
                    owner=owner,
                    completed_at=completed_through,
                    next_due_at=boundary + timedelta(hours=self.interval_hours),
                    summary_date=today if summary_queued else None,
                    cleanup_date=today if last_cleanup_date != today else None,
                )
                if not completed:
                    raise RuntimeError("scheduler lease was lost before completion")
        except Exception:
            with self.database.session() as session:
                SchedulerStateRepository(session, self.workspace_id).release(JOB_NAME, owner=owner)
            raise
        return SchedulerRunOutcome(
            status="SUCCEEDED",
            window_count=len(windows),
            created_count=created_count,
            failed_source_count=failed_source_count,
            processing_tasks=processing_tasks,
            daily_summary_task=summary_queued,
            purged_body_count=purged_count,
        )

    async def _enqueue_task(
        self,
        task_type: str,
        not_before: datetime,
        payload: dict[str, str | int],
    ) -> DispatchReceipt:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = sha256(serialized.encode()).hexdigest()
        task_id = str(uuid5(NAMESPACE_URL, f"{self.workspace_id}:{task_type}:{digest}"))
        return await self.dispatcher.enqueue(
            TaskRequest(
                task_id=task_id,
                task_type=task_type,
                payload=payload,
                idempotency=IdempotencyKey(
                    scope=f"scheduler:{task_type}",
                    key=digest,
                    payload_sha256=digest,
                ),
                not_before=not_before,
            )
        )
