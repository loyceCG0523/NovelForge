"""用户基础接口。

产品登录注册主要使用 auth.py；这里保留轻量用户读写能力，后续可演进为后台管理接口。
"""

import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import (
    EmbeddingApiTestRequest,
    EmbeddingApiTestResult,
    ModelApiTestRequest,
    ModelApiTestResult,
    UserCreate,
    UserPreferencesUpdate,
    UserProfileUpdate,
    UserRead,
)
from app.services.embedding_client import (
    EmbeddingClient,
    build_test_embedding_config,
)
from app.services.llm_client import LLMClient, build_test_llm_config


router = APIRouter(prefix="/api/users", tags=["users"])


def merge_preferences(current: dict, incoming: dict) -> dict:
    """合并用户偏好，避免空 API Key 覆盖已保存密钥。"""
    merged = dict(current or {})
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = {**merged[key], **value}
            secret_fields = {
                "llm": "api_key",
                "review_llm": "api_key",
                "embedding": "api_key",
                "web_search": "tavily_api_key",
            }
            secret_field = secret_fields.get(key)
            if secret_field and not value.get(secret_field):
                nested[secret_field] = merged[key].get(secret_field, "")
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

    当前阶段主要用于保存 LLM 连接配置。生产环境应将 API Key 加密存储或迁移到
    专门的密钥管理服务。
    """
    current_user.preferences = merge_preferences(current_user.preferences, payload.preferences)
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/me/model-api/test", response_model=ModelApiTestResult)
def test_my_model_api(
    payload: ModelApiTestRequest,
    current_user: User = Depends(get_current_user),
) -> ModelApiTestResult:
    """使用表单临时值测试模型连接，不保存配置，也不返回或记录 API Key。"""
    try:
        config = build_test_llm_config(
            current_user.preferences,
            target=payload.target,
            base_url=payload.base_url,
            model=payload.model,
            api_key=payload.api_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    started_at = time.monotonic()
    try:
        content = LLMClient(config).complete_text(
            [
                {
                    "role": "system",
                    "content": "你正在执行 API 连接测试。请严格只回复 OK。",
                },
                {"role": "user", "content": "回复 OK"},
            ],
        )
        if not str(content or "").strip():
            raise RuntimeError("模型返回了空响应")
    except Exception as exc:
        return ModelApiTestResult(
            ok=False,
            target=payload.target,
            model=config.model,
            latency_ms=max(1, round((time.monotonic() - started_at) * 1000)),
            message=str(exc),
        )

    return ModelApiTestResult(
        ok=True,
        target=payload.target,
        model=config.model,
        latency_ms=max(1, round((time.monotonic() - started_at) * 1000)),
        message="连接成功，模型已返回有效响应。",
    )


@router.post("/me/embedding-api/test", response_model=EmbeddingApiTestResult)
def test_my_embedding_api(
    payload: EmbeddingApiTestRequest,
    current_user: User = Depends(get_current_user),
) -> EmbeddingApiTestResult:
    """使用临时表单值测试向量模型，不保存配置或暴露密钥。"""
    try:
        config = build_test_embedding_config(
            current_user.preferences,
            endpoint=payload.endpoint,
            model=payload.model,
            api_key=payload.api_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    started_at = time.monotonic()
    try:
        vectors = EmbeddingClient(config).embed(
            ["用于测试小说表达检索的短文本"],
            text_type="query",
            instruct="Retrieve Chinese fiction passages.",
        )
        if not vectors or len(vectors[0]) != config.dimensions:
            raise RuntimeError("向量模型返回结果为空或维度不正确")
    except Exception as exc:
        return EmbeddingApiTestResult(
            ok=False,
            model=config.model,
            dimensions=config.dimensions,
            latency_ms=max(1, round((time.monotonic() - started_at) * 1000)),
            message=str(exc),
        )

    return EmbeddingApiTestResult(
        ok=True,
        model=config.model,
        dimensions=config.dimensions,
        latency_ms=max(1, round((time.monotonic() - started_at) * 1000)),
        message="连接成功，向量模型已返回有效结果。",
    )


@router.patch("/me/profile", response_model=UserRead)
def update_my_profile(
    payload: UserProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """更新当前用户展示资料。

    头像当前以 URL 形式保存在 preferences.profile.avatar_url 中，避免为轻量设置引入
    额外文件存储流程；后续如接入头像上传，可迁移到对象存储并继续复用该字段。
    """
    display_name = payload.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="Display name cannot be empty")

    current_user.display_name = display_name
    current_user.preferences = merge_preferences(
        current_user.preferences,
        {"profile": {"avatar_url": payload.avatar_url.strip()}},
    )
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
