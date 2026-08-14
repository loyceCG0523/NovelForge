"""协同样本平台接口的数据契约。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    visibility: str = "private"
    publication_status: str = "private"
    reuse_policy: str = "reference_only"
    rights_declared: bool = False
    version: int = 1
    published_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class SampleAnnotationIndexStats(BaseModel):
    """样本库卡片使用的真实人工标注索引统计。"""

    status: str = "unannotated"
    annotation_count: int = 0
    trusted_count: int = 0
    indexed_count: int = 0
    pending_review_count: int = 0
    pending_index_count: int = 0
    quarantined_count: int = 0


class SampleAnalysisLibraryRead(SampleAnalysisRead):
    """样本库视图：额外带上样本归属作品名，便于在作品管理里选择复用。"""

    source_novel_title: str = ""
    annotation_index: SampleAnnotationIndexStats = Field(
        default_factory=SampleAnnotationIndexStats
    )


class SamplePublicationUpdate(BaseModel):
    visibility: str
    reuse_policy: str = "reference_only"
    rights_declared: bool = False
    version: int = Field(ge=1)

    @field_validator("visibility")
    @classmethod
    def validate_visibility(cls, value: str) -> str:
        if value not in {"private", "public"}:
            raise ValueError("visibility 只能是 private 或 public")
        return value

    @field_validator("reuse_policy")
    @classmethod
    def validate_reuse_policy(cls, value: str) -> str:
        if value not in {"reference_only", "excerpt_reuse"}:
            raise ValueError("reuse_policy 不受支持")
        return value


class SampleTextSegmentRead(BaseModel):
    id: UUID
    sequence_no: int
    title: str = ""
    unit_type: str = "chapter"
    start_offset: int
    end_offset: int
    content: str
    content_hash: str


class SampleChapterIndexRead(BaseModel):
    id: UUID
    sequence_no: int
    title: str
    unit_type: str
    annotation_count: int = 0


class SampleAnnotationCreate(BaseModel):
    segment_id: UUID
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    quote_text: str = Field(min_length=2, max_length=1000)
    categories: list[str] = Field(min_length=1, max_length=5)
    note: str = Field(default="", max_length=500)


class SampleAnnotationUpdate(BaseModel):
    version: int = Field(ge=1)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    quote_text: str = Field(min_length=2, max_length=1000)
    categories: list[str] = Field(min_length=1, max_length=5)
    note: str = Field(default="", max_length=500)


class SampleAnnotationNoteDraftRequest(BaseModel):
    segment_id: UUID
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    quote_text: str = Field(min_length=2, max_length=1000)
    categories: list[str] = Field(min_length=1, max_length=5)


class SampleAnnotationNoteDraftRead(BaseModel):
    note: str
    provider: str
    model: str
    used_web_search: bool = False
    source_count: int = 0


class SampleAnnotationVoteWrite(BaseModel):
    value: int = Field(ge=-1, le=1)


class SampleAnnotationReportWrite(BaseModel):
    reason: str = Field(min_length=1, max_length=40)
    detail: str = Field(default="", max_length=500)


class SampleAnnotationRead(BaseModel):
    id: UUID
    sample_analysis_id: UUID
    segment_id: UUID
    creator_id: UUID
    creator_name: str = ""
    start_offset: int
    end_offset: int
    quote_text: str
    categories: list[str]
    note: str
    status: str
    trust_score: float
    version: int
    upvotes: int = 0
    downvotes: int = 0
    report_count: int = 0
    current_user_vote: int = 0
    can_edit: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SampleAnnotationNoteBatchFailureRead(BaseModel):
    annotation_id: UUID
    message: str


class SampleAnnotationNoteBatchRead(BaseModel):
    generated_count: int = 0
    skipped_existing_count: int = 0
    skipped_changed_count: int = 0
    failed_count: int = 0
    remaining_count: int = 0
    annotations: list[SampleAnnotationRead] = Field(default_factory=list)
    failures: list[SampleAnnotationNoteBatchFailureRead] = Field(default_factory=list)


class SampleAnnotationCategoryRead(BaseModel):
    key: str
    label: str


class SampleCollaborationEventRead(BaseModel):
    sequence_no: int
    event_type: str
    payload: dict
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
