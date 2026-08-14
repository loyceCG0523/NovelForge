"""allow standalone sample tasks

Revision ID: 20260711_0009
Revises: 20260711_0008
Create Date: 2026-07-11
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260711_0009"
down_revision: str | None = "20260711_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """允许样本分析任务不绑定具体作品。"""
    op.alter_column("generation_tasks", "novel_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)


def downgrade() -> None:
    """恢复所有任务必须绑定作品的旧约束。"""
    op.alter_column("generation_tasks", "novel_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
