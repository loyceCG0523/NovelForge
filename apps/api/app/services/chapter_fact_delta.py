"""章节完成后同步写入的轻量事实增量。

它不调用模型，只把正文模型已经返回的章节摘要和推进边界转换成短期可检索记忆。
后台 Memory Worker 完成完整抽取后，会原子替换同章的轻量记录。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.memory_item import MemoryItem
from app.models.novel import Novel


FACT_DELTA_SOURCE = "chapter_fact_delta"


def build_chapter_fact_delta(chapter: Chapter, chapter_progress: dict[str, Any] | None = None) -> dict[str, Any]:
    progress = chapter_progress if isinstance(chapter_progress, dict) else {}
    summary = str(
        progress.get("actual_summary")
        or chapter.summary
        or (chapter.content or "")[:240]
    ).strip()
    completed_beats = [
        str(item).strip()
        for item in progress.get("completed_beats", [])
        if str(item).strip()
    ][:8]
    unresolved = [
        str(item).strip()
        for item in progress.get("unresolved_beats", [])
        if str(item).strip()
    ][:8]
    return {
        "memory_type": "chapter_summary",
        "entity_name": f"第 {chapter.chapter_index} 章即时事实",
        "chapter_index_start": chapter.chapter_index,
        "chapter_index_end": chapter.chapter_index,
        "payload": {
            "source": FACT_DELTA_SOURCE,
            "auto_generated": True,
            "lightweight": True,
            "summary": summary,
            "chapter_title": chapter.title or "未命名章节",
            "completed_beats": completed_beats,
            "unresolved_beats": unresolved,
            "actual_ending_state": progress.get("actual_ending_state") or {},
            "consumed_next_beats": progress.get("consumed_next_beats") or [],
            "importance": "high",
        },
    }


def sync_chapter_fact_delta(
    db: Session,
    *,
    novel: Novel,
    chapter: Chapter,
    chapter_progress: dict[str, Any] | None = None,
) -> MemoryItem | None:
    """幂等替换同章轻量事实；没有摘要时不写空记录。"""
    record = build_chapter_fact_delta(chapter, chapter_progress)
    if not str((record["payload"] or {}).get("summary") or "").strip():
        return None
    existing = db.scalars(
        select(MemoryItem).where(
            MemoryItem.novel_id == novel.id,
            MemoryItem.chapter_index_start == chapter.chapter_index,
        )
    ).all()
    for item in existing:
        if (item.payload or {}).get("source") == FACT_DELTA_SOURCE:
            db.delete(item)
    memory = MemoryItem(novel_id=novel.id, **record)
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return memory
