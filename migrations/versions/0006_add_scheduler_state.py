"""add durable scheduler state and lease

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-20 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduler_states",
        sa.Column("scheduler_state_id", sa.String(length=128), nullable=False),
        sa.Column("job_name", sa.String(length=128), nullable=False),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_summary_date", sa.String(length=10), nullable=True),
        sa.Column("last_cleanup_date", sa.String(length=10), nullable=True),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("scheduler_state_id", name=op.f("pk_scheduler_states")),
        sa.UniqueConstraint("workspace_id", "job_name", name="uq_scheduler_states_workspace_job"),
    )
    op.create_index(op.f("ix_scheduler_states_workspace_id"), "scheduler_states", ["workspace_id"])
    op.create_index(
        "ix_scheduler_states_workspace_due",
        "scheduler_states",
        ["workspace_id", "next_due_at"],
    )
    op.create_table(
        "scheduled_tasks",
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("task_type", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id", name=op.f("pk_scheduled_tasks")),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_scheduled_tasks_workspace_key"
        ),
    )
    op.create_index(op.f("ix_scheduled_tasks_workspace_id"), "scheduled_tasks", ["workspace_id"])
    op.create_index(
        "ix_scheduled_tasks_workspace_status_due",
        "scheduled_tasks",
        ["workspace_id", "status", "not_before"],
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_tasks_workspace_status_due", table_name="scheduled_tasks")
    op.drop_index(op.f("ix_scheduled_tasks_workspace_id"), table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
    op.drop_index("ix_scheduler_states_workspace_due", table_name="scheduler_states")
    op.drop_index(op.f("ix_scheduler_states_workspace_id"), table_name="scheduler_states")
    op.drop_table("scheduler_states")
