"""样本分析接口的数据契约。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SampleAnalysisRead(BaseModel):
    """返回给前端的样本分析结构化报告。"""

    id: UUID
    novel_id: UUID | None
    task_id: UUID | None
    status: str
    sample_title: str
    source_author: str
    source_genre: str
    source_file_name: str
    source_file_size: int
    source_word_count: int
    chapter_count: int
    chunk_count: int
    analyzed_chunk_count: int
    error_message: str
    summary: str
    metrics: dict
    report: dict

    model_config = ConfigDict(from_attributes=True)


class SampleAnalysisLibraryRead(SampleAnalysisRead):
    """样本库视图：额外带上样本归属作品名，便于在作品管理里选择复用。"""

    source_novel_title: str = ""
