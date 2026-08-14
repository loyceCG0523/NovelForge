"""章节时间线抽取、同步和上下文整理。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.novel import Novel
from app.models.timeline_entry import TimelineEntry


AUTO_TIMELINE_SOURCE = "timeline_extractor"
ALLOWED_CERTAINTY = {"confirmed", "inferred", "ambiguous"}


def timeline_entry_to_context(entry: TimelineEntry) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "chapter_id": str(entry.chapter_id) if entry.chapter_id else None,
        "story_event_id": str(entry.story_event_id) if entry.story_event_id else None,
        "sequence_no": entry.sequence_no,
        "story_day": entry.story_day,
        "start_time": entry.start_time,
        "end_time": entry.end_time,
        "time_expression": entry.time_expression,
        "era": entry.era,
        "location": entry.location,
        "summary": entry.summary,
        "participants": entry.participants or [],
        "certainty": entry.certainty,
    }


def get_timeline_context(db: Session, novel: Novel, limit: int = 30) -> list[dict[str, Any]]:
    entries = list(
        db.scalars(
            select(TimelineEntry)
            .where(TimelineEntry.novel_id == novel.id)
            .order_by(TimelineEntry.sequence_no.desc(), TimelineEntry.created_at.desc())
            .limit(limit)
        ).all()
    )
    return [timeline_entry_to_context(entry) for entry in reversed(entries)]


def _safe_story_day(value: Any) -> int | None:
    try:
        day = int(value)
    except (TypeError, ValueError):
        return None
    return day if day >= 1 else None


def _normalize_entries(raw: Any, novel: Novel, chapter: Chapter, story_event_id: object | None) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    era = str((novel.brief or {}).get("story_era") or "").strip()
    records: list[dict[str, Any]] = []
    for offset, item in enumerate(raw[:20], start=1):
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue
        certainty = str(item.get("certainty") or "inferred").strip().lower()
        if certainty not in ALLOWED_CERTAINTY:
            certainty = "inferred"
        participants = item.get("participants") if isinstance(item.get("participants"), list) else []
        records.append(
            {
                "novel_id": novel.id,
                "chapter_id": chapter.id,
                "story_event_id": story_event_id,
                "sequence_no": chapter.chapter_index * 100 + offset,
                "story_day": _safe_story_day(item.get("story_day")),
                "start_time": str(item.get("start_time") or "")[:80],
                "end_time": str(item.get("end_time") or "")[:80],
                "time_expression": str(item.get("time_expression") or "")[:160],
                "era": era[:160],
                "location": str(item.get("location") or "")[:200],
                "summary": summary,
                "participants": [str(value).strip() for value in participants if str(value).strip()][:20],
                "certainty": certainty,
                "source": AUTO_TIMELINE_SOURCE,
                "payload": {
                    "evidence": str(item.get("evidence") or ""),
                    "auto_generated": True,
                    "chapter_index": chapter.chapter_index,
                },
            }
        )
    return records


def purge_chapter_timeline_entries(db: Session, chapter: Chapter) -> int:
    result = db.execute(delete(TimelineEntry).where(TimelineEntry.chapter_id == chapter.id))
    return int(result.rowcount or 0)
