"""add bounded AI evidence extraction runs

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-20 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_extraction_runs",
        sa.Column("extraction_run_id", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_name", sa.String(length=128), nullable=False),
        sa.Column("model_version", sa.String(length=120), nullable=False),
        sa.Column("prompt_version", sa.String(length=120), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "document_id"],
            ["normalized_documents.workspace_id", "normalized_documents.document_id"],
            name="fk_ai_extraction_runs_workspace_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("extraction_run_id", name=op.f("pk_ai_extraction_runs")),
        sa.UniqueConstraint(
            "workspace_id",
            "document_id",
            "provider_name",
            "model_version",
            "prompt_version",
            name="uq_ai_extraction_runs_workspace_document_versions",
        ),
    )
    op.create_index(
        op.f("ix_ai_extraction_runs_workspace_id"), "ai_extraction_runs", ["workspace_id"]
    )
    op.create_index(
        "ix_ai_extraction_runs_workspace_status",
        "ai_extraction_runs",
        ["workspace_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_extraction_runs_workspace_status", table_name="ai_extraction_runs")
    op.drop_index(op.f("ix_ai_extraction_runs_workspace_id"), table_name="ai_extraction_runs")
    op.drop_table("ai_extraction_runs")
