"""用户接口的数据契约。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer


def sanitize_preferences(preferences: dict) -> dict:
    """返回给前端前隐藏敏感配置。"""
    safe_preferences = dict(preferences or {})
    llm = dict(safe_preferences.get("llm") or {})
    api_key = llm.pop("api_key", "")
    if api_key:
        llm["api_key_configured"] = True
    safe_preferences["llm"] = llm
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
