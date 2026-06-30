from uuid import UUID

from pydantic import BaseModel, ConfigDict


class UserCreate(BaseModel):
    email: str
    display_name: str


class UserRead(BaseModel):
    id: UUID
    email: str
    display_name: str
    plan: str
    preferences: dict

    model_config = ConfigDict(from_attributes=True)
