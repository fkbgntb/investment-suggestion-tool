"""add isolated normalized documents

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-20 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "normalized_documents",
        sa.Column("normalized_document_id", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("canonical_url", sa.String(length=2048), nullable=False),
        sa.Column("normalized_title", sa.String(length=1000), nullable=False),
        sa.Column("normalized_body", sa.Text(), nullable=False),
        sa.Column("original_hash", sa.String(length=64), nullable=False),
        sa.Column("normalized_hash", sa.String(length=64), nullable=False),
        sa.Column("duplicate_of_document_id", sa.String(length=128), nullable=True),
        sa.Column("normalized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "document_id"],
            ["raw_documents.workspace_id", "raw_documents.document_id"],
            name="fk_normalized_documents_workspace_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("normalized_document_id", name=op.f("pk_normalized_documents")),
        sa.UniqueConstraint(
            "workspace_id",
            "document_id",
            name="uq_normalized_documents_workspace_document",
        ),
    )
    op.create_index(
        op.f("ix_normalized_documents_workspace_id"),
        "normalized_documents",
        ["workspace_id"],
    )
    op.create_index(
        "ix_normalized_documents_workspace_hash",
        "normalized_documents",
        ["workspace_id", "normalized_hash"],
    )
    op.create_index(
        "ix_normalized_documents_workspace_url",
        "normalized_documents",
        ["workspace_id", "canonical_url"],
    )


def downgrade() -> None:
    op.drop_index("ix_normalized_documents_workspace_url", table_name="normalized_documents")
    op.drop_index("ix_normalized_documents_workspace_hash", table_name="normalized_documents")
    op.drop_index(op.f("ix_normalized_documents_workspace_id"), table_name="normalized_documents")
    op.drop_table("normalized_documents")
