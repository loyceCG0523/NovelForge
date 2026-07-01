"""用户接口的数据契约。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


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
