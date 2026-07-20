"""Store immutable rendered report snapshots.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reports", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "template_version",
                sa.String(length=120),
                nullable=False,
                server_default="legacy-template",
            )
        )
        batch_op.add_column(
            sa.Column(
                "media_type",
                sa.String(length=100),
                nullable=False,
                server_default="text/html; charset=utf-8",
            )
        )
        batch_op.add_column(
            sa.Column(
                "content_sha256",
                sa.String(length=64),
                nullable=False,
                server_default="0" * 64,
            )
        )
        batch_op.add_column(
            sa.Column("rendered_content", sa.Text(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column(
                "generated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        batch_op.create_unique_constraint(
            "uq_reports_workspace_analysis_template",
            ["workspace_id", "analysis_run_id", "template_version"],
        )


def downgrade() -> None:
    with op.batch_alter_table("reports", schema=None) as batch_op:
        batch_op.drop_constraint("uq_reports_workspace_analysis_template", type_="unique")
        batch_op.drop_column("generated_at")
        batch_op.drop_column("rendered_content")
        batch_op.drop_column("content_sha256")
        batch_op.drop_column("media_type")
        batch_op.drop_column("template_version")
