"""add user password hash

Revision ID: 20260630_0002
Revises: 20260630_0001
Create Date: 2026-06-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260630_0002"
down_revision: str | None = "20260630_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为用户表补充密码哈希字段，支持正式登录注册。"""
    op.add_column(
        "users",
        sa.Column("password_hash", sa.String(length=255), server_default="", nullable=False),
    )
    op.alter_column("users", "password_hash", server_default=None)


def downgrade() -> None:
    """回滚密码哈希字段。"""
    op.drop_column("users", "password_hash")
