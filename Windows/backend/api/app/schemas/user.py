"""用户接口的数据契约。"""

from uuid import UUID
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.core.desktop_secrets import reveal_preference_secret


def mask_api_key(api_key: str) -> str:
    """只暴露密钥首尾少量字符，中间统一使用掩码。"""
    api_key = str(api_key or "")
    if not api_key:
        return ""
    if len(api_key) <= 4:
        return "••••"
    if len(api_key) <= 8:
        return f"{api_key[:2]}••••{api_key[-2:]}"
    return f"{api_key[:6]}••••••••{api_key[-4:]}"


def sanitize_preferences(preferences: dict) -> dict:
    """返回给前端前隐藏敏感配置。"""
    safe_preferences = dict(preferences or {})
    llm = dict(safe_preferences.get("llm") or {})
    api_key = llm.pop("api_key", "")
    if api_key:
        llm["api_key_configured"] = True
        llm["api_key_masked"] = mask_api_key(reveal_preference_secret(api_key))
    safe_preferences["llm"] = llm
    review_llm = dict(safe_preferences.get("review_llm") or {})
    review_api_key = review_llm.pop("api_key", "")
    if review_api_key:
        review_llm["api_key_configured"] = True
        review_llm["api_key_masked"] = mask_api_key(reveal_preference_secret(review_api_key))
    safe_preferences["review_llm"] = review_llm
    embedding = dict(safe_preferences.get("embedding") or {})
    embedding_api_key = embedding.pop("api_key", "")
    if embedding_api_key:
        embedding["api_key_configured"] = True
        embedding["api_key_masked"] = mask_api_key(reveal_preference_secret(embedding_api_key))
    safe_preferences["embedding"] = embedding
    web_search = dict(safe_preferences.get("web_search") or {})
    tavily_api_key = web_search.pop("tavily_api_key", "")
    if tavily_api_key:
        web_search["tavily_api_key_configured"] = True
        web_search["tavily_api_key_masked"] = mask_api_key(reveal_preference_secret(tavily_api_key))
    safe_preferences["web_search"] = web_search
    return safe_preferences


class UserCreate(BaseModel):
    """创建基础用户记录时提交的数据。"""

    email: str
    display_name: str


class UserRead(BaseModel):
    """返回给前端的用户摘要。"""

    id: UUID
    email: str
    display_name: str
    plan: str
    preferences: dict

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("preferences")
    def serialize_preferences(self, preferences: dict) -> dict:
        """序列化用户偏好时隐藏 API Key。"""
        return sanitize_preferences(preferences)


class UserPreferencesUpdate(BaseModel):
    """更新用户个性化偏好时提交的数据。"""

    preferences: dict = Field(default_factory=dict)


class ModelApiTestRequest(BaseModel):
    """测试正文或独立审校模型连接时使用的临时配置。"""

    target: Literal["writer", "reviewer"]
    base_url: str = Field(default="", max_length=1000)
    model: str = Field(default="", max_length=200)
    api_key: str = Field(default="", max_length=2000)
    context_window_tokens: int = Field(default=128_000, ge=4_096)


class ModelApiTestResult(BaseModel):
    """模型连接测试结果；永不返回 API Key。"""

    ok: bool
    target: Literal["writer", "reviewer"]
    model: str
    latency_ms: int
    message: str


class EmbeddingApiTestRequest(BaseModel):
    """测试远程 Embedding API 时使用的临时配置。"""

    endpoint: str = Field(default="", max_length=1500)
    model: str = Field(default="qwen3.7-text-embedding", max_length=200)
    api_key: str = Field(default="", max_length=2000)


class EmbeddingApiTestResult(BaseModel):
    """向量连接测试结果；永不返回 API Key。"""

    ok: bool
    model: str
    dimensions: int
    latency_ms: int
    message: str


class UserProfileUpdate(BaseModel):
    """更新当前用户的账号展示资料。"""

    display_name: str = Field(min_length=1, max_length=80)
    avatar_url: str = Field(default="", max_length=300000)
