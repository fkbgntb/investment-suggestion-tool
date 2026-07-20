"""Persist validated AI synthesis results and usage.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_results",
        sa.Column("analysis_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_run_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_name", sa.String(length=128), nullable=False),
        sa.Column("model_version", sa.String(length=120), nullable=False),
        sa.Column("prompt_version", sa.String(length=120), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "analysis_run_id"],
            ["analysis_runs.workspace_id", "analysis_runs.analysis_run_id"],
            name="fk_analysis_results_workspace_analysis",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.workspace_id"],
            name="fk_analysis_results_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("analysis_id", name="pk_analysis_results"),
        sa.UniqueConstraint(
            "workspace_id",
            "analysis_run_id",
            "model_version",
            "prompt_version",
            name="uq_analysis_results_workspace_run_versions",
        ),
    )
    with op.batch_alter_table("analysis_results", schema=None) as batch_op:
        batch_op.create_index(
            "ix_analysis_results_workspace_completed",
            ["workspace_id", "completed_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_analysis_results_workspace_id"), ["workspace_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("analysis_results", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_analysis_results_workspace_id"))
        batch_op.drop_index("ix_analysis_results_workspace_completed")
    op.drop_table("analysis_results")
