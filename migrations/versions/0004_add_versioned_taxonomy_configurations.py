"""add versioned taxonomy configurations

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-20 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "taxonomy_configurations",
        sa.Column("configuration_id", sa.String(length=128), nullable=False),
        sa.Column("config_version", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.workspace_id"],
            name=op.f("fk_taxonomy_configurations_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("configuration_id", name=op.f("pk_taxonomy_configurations")),
        sa.UniqueConstraint(
            "workspace_id",
            "configuration_id",
            name="uq_taxonomy_configurations_workspace_configuration",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "config_version",
            name="uq_taxonomy_configurations_workspace_version",
        ),
    )
    with op.batch_alter_table("taxonomy_configurations", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_taxonomy_configurations_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_taxonomy_configurations_workspace_created",
            ["workspace_id", "created_at"],
            unique=False,
        )

    op.create_table(
        "active_taxonomy_configurations",
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.Column("configuration_id", sa.String(length=128), nullable=False),
        sa.Column("config_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "configuration_id"],
            ["taxonomy_configurations.workspace_id", "taxonomy_configurations.configuration_id"],
            name="fk_active_taxonomy_workspace_configuration",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.workspace_id"],
            name=op.f("fk_active_taxonomy_configurations_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("workspace_id", name=op.f("pk_active_taxonomy_configurations")),
    )


def downgrade() -> None:
    op.drop_table("active_taxonomy_configurations")
    with op.batch_alter_table("taxonomy_configurations", schema=None) as batch_op:
        batch_op.drop_index("ix_taxonomy_configurations_workspace_created")
        batch_op.drop_index(batch_op.f("ix_taxonomy_configurations_workspace_id"))
    op.drop_table("taxonomy_configurations")
