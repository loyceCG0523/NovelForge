"""drop sample module tables (feature removed)

样本模块（样本库、模型总体分析、协同标注、双通道 RAG）已整体从代码中移除，
本迁移删除其全部数据表。pgvector 扩展保留（热梗库向量检索仍在使用）。

Revision ID: 20260912_0022
Revises: 20260803_0021
Create Date: 2026-09-12
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260912_0022"
down_revision: str | None = "20260803_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 子表（投票/举报/修订/事件/信誉）→ 标注 → 分段/经验卡 → 样本主表
SAMPLE_TABLES = [
    "sample_annotation_votes",
    "sample_annotation_reports",
    "sample_annotation_revisions",
    "sample_collaboration_events",
    "sample_user_reputations",
    "sample_annotations",
    "sample_text_segments",
    "sample_passages",
    "sample_analyses",
]


def upgrade() -> None:
    for table in SAMPLE_TABLES:
        op.drop_table(table)


def downgrade() -> None:
    # 样本模块已整体下线，历史数据随表删除，不重建表结构；
    # 如需回滚，请从数据库备份恢复。
    pass
