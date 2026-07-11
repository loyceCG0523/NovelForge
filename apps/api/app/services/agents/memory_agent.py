"""MemoryAgent：负责结构化记忆的同步、合并和清理。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.memory_item import MemoryItem
from app.models.novel import Novel
from app.services.llm_client import LLMConfig
from app.services.memory_extractor import purge_chapter_memories, sync_chapter_memories


AGENT_NAME = "MemoryAgent"


def sync_memory_after_chapter(
    db: Session,
    novel: Novel,
    chapter: Chapter,
    llm_config: LLMConfig | None,
) -> list[MemoryItem]:
    """章节完成后同步结构化记忆。"""
    return sync_chapter_memories(db=db, novel=novel, chapter=chapter, llm_config=llm_config)


def purge_memory_for_chapter(db: Session, novel: Novel, chapter: Chapter) -> int:
    """删除章节时同步清理该章来源的结构化记忆。"""
    return purge_chapter_memories(db=db, novel_id=novel.id, chapter_index=chapter.chapter_index)
