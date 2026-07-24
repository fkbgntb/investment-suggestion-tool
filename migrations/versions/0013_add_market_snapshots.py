"""Persist deterministic, split-aware market snapshots.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_snapshots",
        sa.Column("market_snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "asset_id"],
            ["assets.workspace_id", "assets.asset_id"],
            name="fk_market_snapshots_workspace_asset",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["sources.workspace_id", "sources.source_id"],
            name="fk_market_snapshots_workspace_source",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("market_snapshot_id", name="pk_market_snapshots"),
        sa.UniqueConstraint(
            "workspace_id",
            "asset_id",
            "code",
            "source_id",
            "as_of",
            name="uq_market_snapshots_workspace_asset_code_source_date",
        ),
    )
    op.create_index(
        "ix_market_snapshots_workspace_id",
        "market_snapshots",
        ["workspace_id"],
    )
    op.create_index(
        "ix_market_snapshots_workspace_asset_date",
        "market_snapshots",
        ["workspace_id", "asset_id", "as_of"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_snapshots_workspace_asset_date",
        table_name="market_snapshots",
    )
    op.drop_index("ix_market_snapshots_workspace_id", table_name="market_snapshots")
    op.drop_table("market_snapshots")
