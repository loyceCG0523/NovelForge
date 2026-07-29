"""add persistent graph checkpoints

Revision ID: 20260720_0015
Revises: 20260717_0014
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260720_0015"
down_revision: str | None = "20260717_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generation_graph_checkpoints",
        sa.Column("novel_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("graph_name", sa.String(length=100), nullable=False),
        sa.Column("node_name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["novel_id"], ["novels.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["generation_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "graph_name", name="uq_generation_graph_checkpoint_task_graph"),
    )
    for column in ("novel_id", "task_id", "graph_name", "status"):
        op.create_index(
            op.f(f"ix_generation_graph_checkpoints_{column}"),
            "generation_graph_checkpoints",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for column in ("status", "graph_name", "task_id", "novel_id"):
        op.drop_index(
            op.f(f"ix_generation_graph_checkpoints_{column}"),
            table_name="generation_graph_checkpoints",
        )
    op.drop_table("generation_graph_checkpoints")
