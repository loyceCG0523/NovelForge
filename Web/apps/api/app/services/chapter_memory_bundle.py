"""单次模型调用同时抽取章节结构化记忆与故事时间线。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.event_chapter_plan import EventChapterPlan
from app.models.memory_item import MemoryItem
from app.models.novel import Novel
from app.models.timeline_entry import TimelineEntry
from app.services.llm_client import LLMClient, LLMConfig
from app.services.memory_extractor import (
    AUTO_MEMORY_SOURCES,
    build_fallback_memory,
    normalize_memory_items,
)
from app.services.timeline_service import (
    AUTO_TIMELINE_SOURCE,
    _normalize_entries,
    get_timeline_context,
)


def build_chapter_memory_bundle_prompt(
    novel: Novel,
    chapter: Chapter,
    previous_timeline: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """一次发送完整章节，要求同时返回记忆和时间线。"""
    brief = novel.brief or {}
    payload = {
        "novel": {
            "title": novel.title,
            "genre": novel.genre,
            "era": brief.get("story_era") or "",
            "location": brief.get("story_location") or "",
            "characters": brief.get("characters") or [],
        },
        "previous_timeline": previous_timeline,
        "chapter": {
            "chapter_index": chapter.chapter_index,
            "title": chapter.title,
            "summary": chapter.summary,
            "content": chapter.content,
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 NovelForge 的章节事实抽取器。一次完成结构化记忆和故事时间线抽取。"
                "只记录正文已经明确表达或可直接推断的事实，不得补写剧情，不得润色正文。"
                "人物记忆只保存稳定档案；一次性动作写入 event、relationship 或 timeline。"
                "相同事实只保留最完整的一条。只输出 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                "输出格式：\n"
                '{"memories":[{"memory_type":"character|relationship|location|item|event|timeline|world_rule",'
                '"entity_name":"稳定实体名","payload":{"summary":"事实摘要","profile":{},'
                '"evidence":"正文依据","status":"当前状态","importance":"low|medium|high"}}],'
                '"timeline_entries":[{"story_day":1,"start_time":"上午","end_time":"",'
                '"time_expression":"第二天上午","location":"地点","summary":"发生了什么",'
                '"participants":["人物"],"certainty":"confirmed|inferred|ambiguous",'
                '"evidence":"正文依据"}]}\n\n'
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
        },
    ]


def extract_chapter_memory_bundle(
    *,
    db: Session,
    novel: Novel,
    chapter: Chapter,
    llm_config: LLMConfig | None,
    story_event_id=None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """只调用一次模型，并把两个结果标准化为可持久化记录。"""
    if llm_config is None:
        return build_fallback_memory(chapter), []

    previous_timeline = [
        item
        for item in get_timeline_context(db, novel, limit=30)
        if item.get("chapter_id") != str(chapter.id)
    ]
    _, parsed = LLMClient(llm_config).complete_json(
        build_chapter_memory_bundle_prompt(novel, chapter, previous_timeline)
    )
    memories = normalize_memory_items(parsed.get("memories"), chapter.chapter_index)
    timeline = _normalize_entries(
        parsed.get("timeline_entries") or parsed.get("entries"),
        novel,
        chapter,
        story_event_id,
    )
    return memories, timeline


def sync_chapter_memory_bundle(
    *,
    db: Session,
    novel: Novel,
    chapter: Chapter,
    llm_config: LLMConfig | None,
    story_event_id=None,
) -> dict[str, Any]:
    """抽取成功后原子替换本章自动记忆与时间线，失败时保留旧数据。"""
    if story_event_id is None:
        plan = db.scalar(
            select(EventChapterPlan)
            .where(
                EventChapterPlan.novel_id == novel.id,
                EventChapterPlan.chapter_id == chapter.id,
            )
            .order_by(EventChapterPlan.updated_at.desc())
            .limit(1)
        )
        story_event_id = plan.story_event_id if plan else None

    memory_records, timeline_records = extract_chapter_memory_bundle(
        db=db,
        novel=novel,
        chapter=chapter,
        llm_config=llm_config,
        story_event_id=story_event_id,
    )

    existing_memories = db.scalars(
        select(MemoryItem).where(
            MemoryItem.novel_id == novel.id,
            MemoryItem.chapter_index_start == chapter.chapter_index,
        )
    ).all()
    for item in existing_memories:
        if (item.payload or {}).get("source") in AUTO_MEMORY_SOURCES:
            db.delete(item)

    existing_timeline = db.scalars(
        select(TimelineEntry).where(TimelineEntry.chapter_id == chapter.id)
    ).all()
    for entry in existing_timeline:
        if entry.source == AUTO_TIMELINE_SOURCE:
            db.delete(entry)

    memory_items = [MemoryItem(novel_id=novel.id, **record) for record in memory_records]
    timeline_entries = [TimelineEntry(**record) for record in timeline_records]
    db.add_all([*memory_items, *timeline_entries])
    db.commit()
    return {
        "memory_count": len(memory_items),
        "timeline_count": len(timeline_entries),
        "total_count": len(memory_items) + len(timeline_entries),
    }
