"""章节实际剧情进度的标准化与事件计划滚动校准。"""

from __future__ import annotations

from typing import Any


EVENT_CONTEXT_KEYS = (
    "event_type",
    "event_title",
    "event_goal",
    "core_conflict",
    "genre_alignment",
    "dramatic_escalation",
    "major_reversal",
    "reader_payoff",
    "key_characters",
    "supporting_character_roles",
    "completion_criteria",
    "next_event_hook",
    "narrative_completion",
)


def compact_story_event_for_chapter(event_plan: dict[str, Any] | None) -> dict[str, Any]:
    """章节生成只需要事件目标，不需要把所有章节计划重复塞进 Prompt。"""
    event_plan = event_plan or {}
    return {key: event_plan.get(key) for key in EVENT_CONTEXT_KEYS if event_plan.get(key) not in (None, "", [])}


def compact_next_chapter_boundary(chapter_plan: dict[str, Any] | None) -> dict[str, Any]:
    """给当前章一个轻量的下一章边界，允许自然越界后主动申报并重排。"""
    if not chapter_plan:
        return {}
    return {
        key: chapter_plan.get(key)
        for key in (
            "chapter_index",
            "title",
            "function",
            "plot_engine",
            "secondary_element_role",
            "story_time",
            "elapsed_time",
            "core_event",
            "state_change",
            "dramatic_turn",
            "reader_payoff",
            "participants",
            "interaction_contexts",
            "character_beats",
            "comedy_beats",
            "compressed_processes",
            "foreshadowing_actions",
            "ending_hook",
        )
        if chapter_plan.get(key) not in (None, "", [])
    }


def normalize_chapter_progress(
    raw_progress: Any,
    *,
    summary: str,
    content: str,
    current_plan: dict[str, Any] | None,
    next_plan: dict[str, Any] | None,
) -> dict[str, Any]:
    """清洗模型自报的实际剧情进度，拒绝改变下一章编号等不安全字段。"""
    raw = raw_progress if isinstance(raw_progress, dict) else {}
    actual_summary = _compact_text(raw.get("actual_summary") or summary, 500)
    completed_beats = _compact_text_list(raw.get("completed_beats"), limit=12, item_limit=180)
    consumed_next_beats = _compact_text_list(raw.get("consumed_next_beats"), limit=10, item_limit=180)
    ending_state = _normalize_ending_state(raw.get("ending_state"), content)
    revised_next_plan = {}
    if next_plan and consumed_next_beats:
        revised_next_plan = _normalize_revised_next_plan(raw.get("revised_next_chapter_plan"), next_plan)
    meme_usage_plan = _normalize_meme_usage_plan(raw.get("meme_usage_plan"))
    return {
        "schema_version": "chapter_progress.v1",
        "actual_summary": actual_summary,
        "completed_beats": completed_beats,
        "ending_state": ending_state,
        "consumed_next_beats": consumed_next_beats,
        "revised_next_chapter_plan": revised_next_plan,
        "current_plan": compact_next_chapter_boundary(current_plan),
        "meme_usage_plan": meme_usage_plan,
    }


def _normalize_meme_usage_plan(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        phrase = _compact_text(item.get("phrase"), 120)
        key = phrase.casefold()
        if not phrase or key in seen:
            continue
        seen.add(key)
        decision = str(item.get("decision") or "skip").strip().lower()
        result.append(
            {
                "phrase": phrase,
                "decision": "use" if decision == "use" else "skip",
                "speaker": _compact_text(item.get("speaker"), 80),
                "listener": _compact_text(item.get("listener"), 80),
                "relationship": _compact_text(item.get("relationship"), 120),
                "emotion": _compact_text(item.get("emotion"), 100),
                "speech_act": _compact_text(item.get("speech_act"), 100),
                "required_setup": _compact_text(item.get("required_setup"), 220),
                "scene_anchor": _compact_text(item.get("scene_anchor"), 180),
                "response": _compact_text(item.get("response"), 220),
                "plot_consequence": _compact_text(
                    item.get("plot_consequence"),
                    220,
                ),
                "reason": _compact_text(item.get("reason"), 180),
            }
        )
        if len(result) >= 5:
            break
    return result


def apply_revised_next_plan(
    chapter_plans: list[dict[str, Any]],
    next_plan_index: int,
    chapter_progress: dict[str, Any],
) -> list[dict[str, Any]]:
    """把模型建议合并到 Graph 内存计划；没有明确越界时保持原计划。"""
    revised = chapter_progress.get("revised_next_chapter_plan") or {}
    if not revised or next_plan_index < 0 or next_plan_index >= len(chapter_plans):
        return list(chapter_plans)
    updated = [dict(item) for item in chapter_plans]
    updated[next_plan_index] = {**updated[next_plan_index], **revised}
    return updated


def _normalize_revised_next_plan(raw: Any, original: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    normalized = {
        "chapter_index": original.get("chapter_index"),
        "title": _compact_text(raw.get("title") or original.get("title"), 120),
        "function": _compact_text(raw.get("function") or original.get("function"), 50),
        "story_time": _compact_text(raw.get("story_time") or original.get("story_time"), 120),
        "elapsed_time": _compact_text(raw.get("elapsed_time") or original.get("elapsed_time"), 120),
        "core_event": _compact_text(raw.get("core_event") or original.get("core_event"), 800),
        "state_change": _compact_text(raw.get("state_change") or original.get("state_change"), 500),
        "character_beats": _compact_text_list(
            raw.get("character_beats") or original.get("character_beats"), limit=8, item_limit=240
        ),
        "comedy_beats": _compact_text_list(
            raw.get("comedy_beats") or original.get("comedy_beats"), limit=4, item_limit=240
        ),
        "compressed_processes": _compact_text_list(
            raw.get("compressed_processes") or original.get("compressed_processes"), limit=6, item_limit=200
        ),
        "foreshadowing_actions": _compact_text_list(
            raw.get("foreshadowing_actions") or original.get("foreshadowing_actions"), limit=8, item_limit=240
        ),
        "ending_hook": _compact_text(raw.get("ending_hook") or original.get("ending_hook"), 500),
    }
    # 如果模型只把原计划原样复述回来，就没有必要制造一次“已重排”的假记录。
    meaningful_keys = (
        "title",
        "core_event",
        "state_change",
        "character_beats",
        "comedy_beats",
        "compressed_processes",
        "foreshadowing_actions",
        "ending_hook",
    )
    if all(normalized.get(key) == original.get(key) for key in meaningful_keys):
        return {}
    return normalized


def _normalize_ending_state(raw: Any, content: str) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    characters = raw.get("characters") if isinstance(raw.get("characters"), dict) else {}
    normalized_characters = {
        _compact_text(name, 80): _compact_text(status, 240)
        for name, status in list(characters.items())[:12]
        if _compact_text(name, 80) and _compact_text(status, 240)
    }
    return {
        "story_time": _compact_text(raw.get("story_time"), 160),
        "location": _compact_text(raw.get("location"), 160),
        "characters": normalized_characters,
        "open_actions": _compact_text_list(raw.get("open_actions"), limit=8, item_limit=200),
        "final_scene": _compact_text(raw.get("final_scene") or (content or "")[-600:], 700),
    }


def _compact_text_list(value: Any, *, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = _compact_text(item, item_limit)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _compact_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip("，。；、,. ") + "…"
