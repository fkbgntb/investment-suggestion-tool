"""add source registry state and health

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-20 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_adapter_states",
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("adapter_name", sa.String(length=128), nullable=False),
        sa.Column("adapter_version", sa.String(length=64), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state_version >= 0", name=op.f("ck_source_adapter_states_state_version_nonnegative")
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["sources.workspace_id", "sources.source_id"],
            name="fk_source_adapter_states_workspace_source",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("source_id", name=op.f("pk_source_adapter_states")),
        sa.UniqueConstraint(
            "workspace_id", "source_id", name="uq_source_adapter_states_workspace_source"
        ),
    )
    op.create_index(
        op.f("ix_source_adapter_states_workspace_id"), "source_adapter_states", ["workspace_id"]
    )

    op.create_table(
        "source_health",
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("circuit_open_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name=op.f("ck_source_health_consecutive_failures_nonnegative"),
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["sources.workspace_id", "sources.source_id"],
            name="fk_source_health_workspace_source",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("source_id", name=op.f("pk_source_health")),
        sa.UniqueConstraint("workspace_id", "source_id", name="uq_source_health_workspace_source"),
    )
    op.create_index(op.f("ix_source_health_workspace_id"), "source_health", ["workspace_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_source_health_workspace_id"), table_name="source_health")
    op.drop_table("source_health")
    op.drop_index(op.f("ix_source_adapter_states_workspace_id"), table_name="source_adapter_states")
    op.drop_table("source_adapter_states")
