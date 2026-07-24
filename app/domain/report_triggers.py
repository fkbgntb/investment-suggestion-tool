"""Auditable outcomes for automatic report decisions."""

from __future__ import annotations

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, Identifier
from app.domain.enums import ReportTriggerStatus


class ReportTriggerOutcome(DomainModel):
    trigger_id: Identifier
    task_id: Identifier
    status: ReportTriggerStatus
    reason: str = Field(min_length=1, max_length=500)
    fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    considered_evidence_count: int = Field(default=0, ge=0, le=1000)
    new_evidence_count: int = Field(default=0, ge=0, le=1000)
    actionable_evidence_count: int = Field(default=0, ge=0, le=1000)
    report_id: Identifier | None = None
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def generated_outcomes_require_a_report(self) -> ReportTriggerOutcome:
        if self.status is ReportTriggerStatus.GENERATED and self.report_id is None:
            raise ValueError("a generated trigger outcome must reference its report")
        if self.status is not ReportTriggerStatus.GENERATED and self.report_id is not None:
            raise ValueError("a skipped or failed trigger outcome cannot reference a report")
        if self.new_evidence_count > self.considered_evidence_count:
            raise ValueError("new evidence cannot exceed considered evidence")
        if self.actionable_evidence_count > self.considered_evidence_count:
            raise ValueError("actionable evidence cannot exceed considered evidence")
        return self
