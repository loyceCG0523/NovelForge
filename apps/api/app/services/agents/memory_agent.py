"""MemoryAgent：负责结构化记忆的同步、合并和清理。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.novel import Novel
from app.services.memory_extractor import purge_chapter_memories
from app.services.timeline_service import purge_chapter_timeline_entries


AGENT_NAME = "MemoryAgent"


def purge_memory_for_chapter(db: Session, novel: Novel, chapter: Chapter) -> int:
    """删除章节时同步清理该章来源的结构化记忆。"""
    timeline_count = purge_chapter_timeline_entries(db=db, chapter=chapter)
    memory_count = purge_chapter_memories(db=db, novel_id=novel.id, chapter_index=chapter.chapter_index)
    return timeline_count + memory_count
