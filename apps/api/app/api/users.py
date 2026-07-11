"""用户基础接口。

产品登录注册主要使用 auth.py；这里保留轻量用户读写能力，后续可演进为后台管理接口。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import UserCreate, UserPreferencesUpdate, UserRead


router = APIRouter(prefix="/api/users", tags=["users"])


def merge_preferences(current: dict, incoming: dict) -> dict:
    """合并用户偏好，避免空 API Key 覆盖已保存密钥。"""
    merged = dict(current or {})
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = {**merged[key], **value}
            if key == "llm" and not value.get("api_key"):
                nested["api_key"] = merged[key].get("api_key", "")
            merged[key] = nested
        else:
            merged[key] = value
    return merged


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: Session = Depends(get_db)) -> User:
    """创建基础用户记录；不包含密码，当前主要用于开发调试。"""
    existing_user = db.scalar(select(User).where(User.email == payload.email))
    if existing_user is not None:
        raise HTTPException(status_code=409, detail="Email already exists")

    user = User(email=payload.email, display_name=payload.display_name)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.patch("/me/preferences", response_model=UserRead)
def update_my_preferences(
    payload: UserPreferencesUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """更新当前用户偏好。

    当前阶段用于保存 LLM 连接配置、创作偏好和自动化策略。生产环境应将 API Key
    加密存储或迁移到专门的密钥管理服务。
    """
    current_user.preferences = merge_preferences(current_user.preferences, payload.preferences)
    db.commit()
    db.refresh(current_user)
    return current_user


@router.get("/{user_id}", response_model=UserRead)
def get_user(user_id: UUID, db: Session = Depends(get_db)) -> User:
    """按用户 ID 读取用户摘要。"""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user
