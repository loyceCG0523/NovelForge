"""把完整章节快照投影成更短、更强调最近剧情状态的生成输入。"""

from __future__ import annotations

from typing import Any


def compact_story_bible(story_bible: dict[str, Any] | None) -> dict[str, Any]:
    """只保留生成链路实际消费的 StoryBible 字段。

    Novel.brief 已在创建 StoryBible 时完成归一化，后续 Agent 不再同时接收两份
    近似设定。pacing_plan 由生产总控按当前阶段单独下发，也不在每章重复传输。
    """
    story_bible = story_bible or {}
    raw_content = dict(story_bible.get("content") or {})
    content = {
        key: raw_content[key]
        for key in (
            "main_characters",
            "world_rules",
            "main_plot",
            "narrative_contract",
            "style_rules",
            "generation_policy",
        )
        if raw_content.get(key) not in (None, "", [], {})
    }
    if isinstance(content.get("main_characters"), list):
        content["main_characters"] = [
            {
                key: value
                for key, value in {
                    "key": character.get("key"),
                    "name": character.get("name"),
                    "gender": character.get("gender"),
                    "age": character.get("age"),
                    "occupation": character.get("occupation"),
                    "is_protagonist": character.get("is_protagonist"),
                    "role": character.get("role"),
                    "goal": character.get("goal"),
                    "setting": (
                        character.get("detailed_setting")
                        or character.get("personality")
                        or character.get("identity")
                    ),
                }.items()
                if value not in (None, "", [], {})
            }
            for character in content["main_characters"]
            if isinstance(character, dict)
        ]
    style_rules = dict(content.get("style_rules") or {})
    style_rules.pop("sample_style_references", None)
    style_rules.pop("sample_style_rules", None)
    if style_rules:
        content["style_rules"] = style_rules
    return {
        "content": content,
        **(
            {"locked_fields": story_bible["locked_fields"]}
            if story_bible.get("locked_fields") not in (None, "", [], {})
            else {}
        ),
    }


def compact_recent_chapters(chapters: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """保留最近一章全文；更早章节只保留摘要、进度和结尾片段。"""
    chapters = chapters or []
    compacted = []
    last_position = len(chapters) - 1
    for position, chapter in enumerate(chapters):
        content = str(chapter.get("content") or "")
        compacted.append(
            {
                "chapter_index": chapter.get("chapter_index"),
                "title": chapter.get("title", ""),
                "summary": chapter.get("summary", ""),
                "word_count": chapter.get("word_count", 0),
                "chapter_progress": chapter.get("chapter_progress") or {},
                "content": content if position == last_position else content[-800:],
                "content_scope": "full_latest_chapter" if position == last_position else "ending_excerpt",
            }
        )
    return compacted


def compact_meme_reference_pack(reference_pack: dict[str, Any] | None) -> dict[str, Any]:
    """热梗只传递少量已审核候选，不把来源链接和完整检索文本塞进正文 Prompt。"""
    reference_pack = reference_pack or {}
    return {
        "status": reference_pack.get("status", "skipped"),
        "event_used_phrases": (
            reference_pack.get("event_used_phrases") or []
        )[:12],
        "usage_policy": {
            "allowed": "只在人物身份、关系、情绪和当前话题都自然吻合时使用",
            "forbidden": "为了凑数量转移话题、解释梗出处、让所有人物使用相同口吻、使用候选之外的热梗，或复用 event_used_phrases 中本事件已经出现的热梗",
        },
        "references": [
            {
                "entry_id": item.get("entry_id", ""),
                "phrase": item.get("phrase", ""),
                "meaning": item.get("meaning", ""),
                "suitable_scenes": item.get("suitable_scenes", ""),
                "scene_fit_score": item.get("scene_fit_score", 0),
                "scene_fit": item.get("scene_fit") or {},
            }
            for item in (reference_pack.get("references") or [])[:5]
        ],
    }


def compact_review_issues(issues: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """生成前只保留最近的可执行风险，移除审校内部记录和重复上下文。"""
    return [
        {
            "issue_type": item.get("issue_type", ""),
            "severity": item.get("severity", ""),
            "message": item.get("message", ""),
            "suggestion": (item.get("payload") or {}).get("suggestion", ""),
        }
        for item in (issues or [])[:10]
    ]


def compact_prompt_constraints(constraints: dict[str, Any], guidance: dict[str, Any]) -> dict[str, Any]:
    """只保留章节级覆盖项；全书设定统一从 StoryBible 读取。"""
    return {
        key: constraints.get(key)
        for key in (
            "chapter_word_range",
        )
        if constraints.get(key) not in (None, "", [], {})
    } | {
        "continuity_reminders": guidance.get("continuity_reminders") or [],
    }


def latest_chapter_progress(chapters: list[dict[str, Any]]) -> dict[str, Any]:
    if not chapters:
        return {}
    latest = chapters[-1]
    progress = latest.get("chapter_progress") or {}
    return {
        "chapter_index": latest.get("chapter_index"),
        "actual_summary": progress.get("actual_summary") or latest.get("summary", ""),
        "completed_beats": progress.get("completed_beats") or [],
        "ending_state": progress.get("ending_state") or {},
        "ending_excerpt": str(latest.get("content") or "")[-800:],
    }
