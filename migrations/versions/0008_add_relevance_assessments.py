"""add explainable relevance assessments

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-20 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "relevance_assessments",
        sa.Column("assessment_id", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column("score", sa.String(length=16), nullable=False),
        sa.Column("rule_version", sa.String(length=120), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=64), nullable=False),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "document_id"],
            ["normalized_documents.workspace_id", "normalized_documents.document_id"],
            name="fk_relevance_assessments_workspace_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("assessment_id", name=op.f("pk_relevance_assessments")),
        sa.UniqueConstraint(
            "workspace_id",
            "document_id",
            "rule_version",
            "taxonomy_version",
            name="uq_relevance_assessments_workspace_document_versions",
        ),
    )
    op.create_index(
        op.f("ix_relevance_assessments_workspace_id"), "relevance_assessments", ["workspace_id"]
    )
    op.create_index(
        "ix_relevance_assessments_workspace_label",
        "relevance_assessments",
        ["workspace_id", "label"],
    )
    op.create_table(
        "human_relevance_labels",
        sa.Column("label_id", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column("labeled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "document_id"],
            ["normalized_documents.workspace_id", "normalized_documents.document_id"],
            name="fk_human_relevance_labels_workspace_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("label_id", name=op.f("pk_human_relevance_labels")),
        sa.UniqueConstraint(
            "workspace_id", "label_id", name="uq_human_relevance_labels_workspace_label"
        ),
    )
    op.create_index(
        op.f("ix_human_relevance_labels_workspace_id"), "human_relevance_labels", ["workspace_id"]
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_human_relevance_labels_workspace_id"), table_name="human_relevance_labels"
    )
    op.drop_table("human_relevance_labels")
    op.drop_index("ix_relevance_assessments_workspace_label", table_name="relevance_assessments")
    op.drop_index(op.f("ix_relevance_assessments_workspace_id"), table_name="relevance_assessments")
    op.drop_table("relevance_assessments")
