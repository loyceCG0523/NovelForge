"""async sample analysis

Revision ID: 20260709_0007
Revises: 20260709_0006
Create Date: 2026-07-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260709_0007"
down_revision: str | None = "20260709_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为样本分析增加异步任务和文件对象元信息。"""
    op.add_column("sample_analyses", sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("sample_analyses", sa.Column("source_file_name", sa.String(length=255), nullable=False, server_default=""))
    op.add_column("sample_analyses", sa.Column("source_object_key", sa.String(length=512), nullable=False, server_default=""))
    op.add_column("sample_analyses", sa.Column("source_file_size", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("sample_analyses", sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("sample_analyses", sa.Column("analyzed_chunk_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("sample_analyses", sa.Column("error_message", sa.Text(), nullable=False, server_default=""))
    op.create_foreign_key(
        "fk_sample_analyses_task_id_generation_tasks",
        "sample_analyses",
        "generation_tasks",
        ["task_id"],
        ["id"],
    )


def downgrade() -> None:
    """回滚样本分析异步任务字段。"""
    op.drop_constraint("fk_sample_analyses_task_id_generation_tasks", "sample_analyses", type_="foreignkey")
    op.drop_column("sample_analyses", "error_message")
    op.drop_column("sample_analyses", "analyzed_chunk_count")
    op.drop_column("sample_analyses", "chunk_count")
    op.drop_column("sample_analyses", "source_file_size")
    op.drop_column("sample_analyses", "source_object_key")
    op.drop_column("sample_analyses", "source_file_name")
    op.drop_column("sample_analyses", "task_id")
