"""用户表模型。"""

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class User(IdMixin, TimestampMixin, Base):
    """平台用户，保存登录信息、套餐和个性化偏好。"""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    plan: Mapped[str] = mapped_column(String(40), default="free")
    preferences: Mapped[dict] = mapped_column(JSONB, default=dict)
