from pydantic import BaseModel, Field

from app.schemas.user import UserRead


class RegisterRequest(BaseModel):
    email: str
    display_name: str
    password: str = Field(min_length=8, max_length=72)


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=72)


class TokenRead(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserRead
