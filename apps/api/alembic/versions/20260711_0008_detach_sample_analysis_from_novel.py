"""detach sample analysis from novel

Revision ID: 20260711_0008
Revises: 20260709_0007
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260711_0008"
down_revision: str | None = "20260709_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """允许样本分析脱离源作品保留为独立样本库资产。"""
    op.add_column("sample_analyses", sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute(
        """
        UPDATE sample_analyses
        SET owner_id = novels.owner_id
        FROM novels
        WHERE sample_analyses.novel_id = novels.id
        """
    )
    op.alter_column("sample_analyses", "owner_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.create_foreign_key(
        "fk_sample_analyses_owner_id_users",
        "sample_analyses",
        "users",
        ["owner_id"],
        ["id"],
    )
    op.create_index(op.f("ix_sample_analyses_owner_id"), "sample_analyses", ["owner_id"], unique=False)
    op.alter_column("sample_analyses", "novel_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)


def downgrade() -> None:
    """恢复样本分析必须绑定作品的旧约束。"""
    op.alter_column("sample_analyses", "novel_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.drop_index(op.f("ix_sample_analyses_owner_id"), table_name="sample_analyses")
    op.drop_constraint("fk_sample_analyses_owner_id_users", "sample_analyses", type_="foreignkey")
    op.drop_column("sample_analyses", "owner_id")
