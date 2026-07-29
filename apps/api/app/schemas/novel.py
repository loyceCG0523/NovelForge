"""作品接口的数据契约。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


REQUIRED_CHARACTER_FIELDS = {
    "name": "姓名",
    "gender": "性别",
    "age": "年龄",
    "occupation": "职业",
}
def validate_structured_brief(brief: dict) -> dict:
    """校验起始需求中的时代与主要人物；剧情事件由系统规划。"""
    if not str(brief.get("story_era") or "").strip():
        raise ValueError("起始需求必须填写故事发生年代")
    characters = brief.get("characters")
    if not isinstance(characters, list) or not characters:
        raise ValueError("起始需求至少需要一名结构化人物")
    for index, character in enumerate(characters, start=1):
        if not isinstance(character, dict):
            raise ValueError(f"第 {index} 名人物格式不正确")
        for key, label in REQUIRED_CHARACTER_FIELDS.items():
            if not str(character.get(key) or "").strip():
                raise ValueError(f"第 {index} 名人物缺少{label}")
        if not isinstance(character.get("is_protagonist"), bool):
            raise ValueError(f"第 {index} 名人物必须明确是否为主角")
    if not any(bool(item.get("is_protagonist")) for item in characters):
        raise ValueError("至少需要将一名人物标记为主角")

    return brief


class NovelCreate(BaseModel):
    """创建作品项目时提交的基础信息和起始需求。"""

    title: str
    genre: str = ""
    target_words: int = 300000
    premise: str = ""
    brief: dict = Field(default_factory=dict)

    _validate_brief = field_validator("brief")(validate_structured_brief)


class NovelUpdate(BaseModel):
    """更新作品项目时允许局部修改的字段。"""

    title: str | None = None
    genre: str | None = None
    status: str | None = None
    target_words: int | None = None
    premise: str | None = None
    brief: dict | None = None

    @field_validator("brief")
    @classmethod
    def validate_brief(cls, value: dict | None) -> dict | None:
        return validate_structured_brief(value) if value is not None else value


class NovelDeleteConfirm(BaseModel):
    """删除作品时提交的防误删确认码。"""

    confirmation_code: str
    expected_code: str


class NovelRead(BaseModel):
    """返回给前端的作品项目视图。"""

    id: UUID
    owner_id: UUID
    title: str
    genre: str
    status: str
    target_words: int
    current_chapter_index: int
    premise: str
    brief: dict

    model_config = ConfigDict(from_attributes=True)
