"""Consume scheduled report tasks and record an explicit generate-or-skip outcome."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.base import Money
from app.domain.enums import ReportTriggerStatus
from app.domain.report_triggers import ReportTriggerOutcome
from app.services.analysis_workflow import AnalysisWorkflowService
from app.services.evidence_selection import EffectiveEvidenceSelector, EvidenceSelection
from app.services.portfolio import PortfolioService
from app.storage.models import ScheduledTaskRow
from app.storage.repositories import AuditRepository, TaskQueueRepository


@dataclass(frozen=True)
class ReportTriggerBatch:
    generated: int = 0
    skipped: int = 0
    failed: int = 0


class ScheduledReportTriggerService:
    def __init__(
        self,
        session: Session,
        workspace_id: str,
        settings: Settings,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.settings = settings
        self.tasks = TaskQueueRepository(session, workspace_id)
        self.audit = AuditRepository(session, workspace_id)

    async def consume_due(self, *, now: datetime) -> ReportTriggerBatch:
        if now.tzinfo is None:
            raise ValueError("report trigger time must include a timezone")
        normalized_now = now.astimezone(UTC)
        generated = skipped = failed = 0
        for task_type in ("process-new-documents", "daily-summary"):
            tasks = self.tasks.list_due(task_type, now=normalized_now)
            for task in tasks:
                try:
                    outcome = await self._consume(task, now=normalized_now)
                except Exception as error:
                    outcome = self._outcome(
                        task=task,
                        now=normalized_now,
                        status=ReportTriggerStatus.FAILED,
                        reason=f"自动报告执行失败：{type(error).__name__}",
                        fingerprint=self._fallback_fingerprint(task),
                    )
                    self.tasks.mark_failed(
                        task.task_id,
                        finished_at=normalized_now,
                        result=outcome.model_dump(mode="json"),
                    )
                    failed += 1
                    continue
                self.tasks.mark_succeeded(
                    task.task_id,
                    finished_at=normalized_now,
                    result=outcome.model_dump(mode="json"),
                )
                if outcome.status is ReportTriggerStatus.GENERATED:
                    generated += 1
                else:
                    skipped += 1
        return ReportTriggerBatch(generated=generated, skipped=skipped, failed=failed)

    async def _consume(
        self,
        task: ScheduledTaskRow,
        *,
        now: datetime,
    ) -> ReportTriggerOutcome:
        portfolio = PortfolioService(self.session, self.workspace_id)
        positions = portfolio.list_positions()
        reference_value = self.settings.portfolio_reference_value
        if len(positions) != 1 or reference_value is None:
            return self._outcome(
                task=task,
                now=now,
                status=ReportTriggerStatus.SKIPPED_POSITION_INCOMPLETE,
                reason="自动分析要求恰好一条本地持仓和已配置的组合参考总额",
                fingerprint=self._fallback_fingerprint(task),
            )
        position = positions[0]
        selection = EffectiveEvidenceSelector(self.session, self.workspace_id).select(
            now=now,
            position=position,
            report_date=now.date(),
        )
        if not selection.new_evidence_ids:
            status = (
                ReportTriggerStatus.SKIPPED_DATA_EXPIRED
                if not selection.evidence and selection.expired_evidence_count
                else ReportTriggerStatus.SKIPPED_NO_NEW_EVIDENCE
            )
            reason = (
                "历史证据已超过对应影响周期，未重复用于新报告"
                if status is ReportTriggerStatus.SKIPPED_DATA_EXPIRED
                else "与上一份报告相比没有新增的有效证据"
            )
            return self._from_selection(task, selection, now=now, status=status, reason=reason)

        new_actionable = set(selection.new_evidence_ids) & set(selection.actionable_evidence_ids)
        if task.task_type == "process-new-documents" and not new_actionable:
            return self._from_selection(
                task,
                selection,
                now=now,
                status=ReportTriggerStatus.SKIPPED_ONLY_AGGREGATOR,
                reason="新增内容只有聚合或低等级证据，留待当日观察报告",
            )

        _, _, report = await AnalysisWorkflowService(
            self.session,
            self.workspace_id,
            self.settings,
        ).run(
            position_id=position.position_id,
            portfolio_value=Money(amount=reference_value, currency="CNY"),
            now=now,
            evidence_selection=selection,
            trigger_fingerprint=selection.fingerprint,
        )
        reason = (
            "当日只有聚合消息，已生成不含明确加减仓动作的观察报告"
            if selection.aggregator_only
            else "发现新增的有效证据，已完成确定性决策与报告生成"
        )
        outcome = self._from_selection(
            task,
            selection,
            now=now,
            status=ReportTriggerStatus.GENERATED,
            reason=reason,
            report_id=report.report_id,
        )
        self.audit.record(
            event_type="scheduled_report_trigger",
            actor="local_scheduler",
            target_type="report",
            target_id=report.report_id,
            outcome="generated",
            details={
                "task_type": task.task_type,
                "evidence_count": len(selection.evidence),
                "new_evidence_count": len(selection.new_evidence_ids),
                "actionable_evidence_count": len(selection.actionable_evidence_ids),
            },
            occurred_at=now,
        )
        return outcome

    def _from_selection(
        self,
        task: ScheduledTaskRow,
        selection: EvidenceSelection,
        *,
        now: datetime,
        status: ReportTriggerStatus,
        reason: str,
        report_id: str | None = None,
    ) -> ReportTriggerOutcome:
        return self._outcome(
            task=task,
            now=now,
            status=status,
            reason=reason,
            fingerprint=selection.fingerprint,
            considered_evidence_count=len(selection.evidence),
            new_evidence_count=len(selection.new_evidence_ids),
            actionable_evidence_count=len(selection.actionable_evidence_ids),
            report_id=report_id,
        )

    @staticmethod
    def _outcome(
        *,
        task: ScheduledTaskRow,
        now: datetime,
        status: ReportTriggerStatus,
        reason: str,
        fingerprint: str,
        considered_evidence_count: int = 0,
        new_evidence_count: int = 0,
        actionable_evidence_count: int = 0,
        report_id: str | None = None,
    ) -> ReportTriggerOutcome:
        trigger_id = str(uuid5(NAMESPACE_URL, f"{task.task_id}:{fingerprint}:{status.value}"))
        return ReportTriggerOutcome(
            trigger_id=trigger_id,
            task_id=task.task_id,
            status=status,
            reason=reason,
            fingerprint=fingerprint,
            considered_evidence_count=considered_evidence_count,
            new_evidence_count=new_evidence_count,
            actionable_evidence_count=actionable_evidence_count,
            report_id=report_id,
            completed_at=now,
        )

    @staticmethod
    def _fallback_fingerprint(task: ScheduledTaskRow) -> str:
        return hashlib.sha256(
            f"{task.workspace_id}:{task.task_id}:{task.payload_sha256}".encode()
        ).hexdigest()
