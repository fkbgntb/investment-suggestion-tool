"""Record deterministic rule and evidence scoring versions on analysis runs.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("analysis_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "rule_version",
                sa.String(length=120),
                nullable=False,
                server_default="not-decided",
            )
        )
        batch_op.add_column(
            sa.Column(
                "scoring_version",
                sa.String(length=120),
                nullable=False,
                server_default="unknown",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("analysis_runs", schema=None) as batch_op:
        batch_op.drop_column("scoring_version")
        batch_op.drop_column("rule_version")
