"""add workspace retention policy

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-19 22:10:36.580727
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "raw_document_retention_days",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("90"),
            )
        )
        batch_op.create_check_constraint(
            "ck_workspaces_retention_days_range",
            "raw_document_retention_days BETWEEN 1 AND 3650",
        )


def downgrade() -> None:
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.drop_constraint("ck_workspaces_retention_days_range", type_="check")
        batch_op.drop_column("raw_document_retention_days")
