"""认证接口的数据契约。"""

from pydantic import BaseModel, Field

from app.schemas.user import UserRead


class RegisterRequest(BaseModel):
    """注册请求体。"""

    email: str
    display_name: str
    password: str = Field(min_length=8, max_length=72)


class LoginRequest(BaseModel):
    """登录请求体。"""

    email: str
    password: str = Field(min_length=8, max_length=72)


class TokenRead(BaseModel):
    """登录/注册成功后返回给前端的令牌与用户信息。"""

    access_token: str
    token_type: str = "bearer"
    user: UserRead
