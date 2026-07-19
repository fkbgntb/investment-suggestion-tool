"""Background job run contract without scheduler implementation details."""

from __future__ import annotations

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, IdempotencyKey, Identifier
from app.domain.enums import JobStatus


class JobRun(DomainModel):
    job_run_id: Identifier
    job_type: Identifier
    status: JobStatus
    idempotency: IdempotencyKey
    attempt: int = Field(default=1, ge=1, le=100)
    scheduled_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error_code: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_job_times(self) -> JobRun:
        if self.started_at is not None and self.started_at < self.scheduled_at:
            raise ValueError("job cannot start before it is scheduled")
        if self.finished_at is not None and self.started_at is None:
            raise ValueError("finished job must have a start time")
        if (
            self.finished_at is not None
            and self.started_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("job cannot finish before it starts")
        terminal_states = {
            JobStatus.SUCCEEDED,
            JobStatus.PERMANENT_FAILED,
            JobStatus.CANCELLED,
        }
        if self.status in terminal_states and self.finished_at is None:
            raise ValueError("terminal job must have a finish time")
        if self.status is JobStatus.SUCCEEDED and self.error_code is not None:
            raise ValueError("successful job cannot have an error code")
        return self
