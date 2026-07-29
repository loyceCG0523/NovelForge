"""单章 Reviewer 建议 -> Writer 段落补丁闭环。

Reviewer 只检查单章内部逻辑、语法表达和明显 AI 化写法，不判断跨章连续性，
也不直接修改正文。Writer 只根据建议返回最小段落补丁，系统校验后应用一次。
"""

from __future__ import annotations

import json
import math
import re
import time
from difflib import SequenceMatcher
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.chapter_revision_patch import ChapterRevisionPatch
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue
from app.services.chapter_quality_contract import (
    CHAPTER_REVIEW_ISSUE_TYPES,
    QUALITY_DIMENSIONS,
    build_review_quality_contract,
)
from app.services.chapter_word_guard import count_chapter_words
from app.services.event_revision_service import (
    apply_paragraph_patches,
    split_chapter_paragraphs,
    text_hash,
)
from app.services.llm_client import LLMClient, LLMConfig
from app.services.meme_rag import build_final_meme_usage
from app.services.punctuation_style_checker import check_punctuation_style
from app.services.task_events import emit_task_event


CHAPTER_REVIEW_SOURCE = "chapter_review_cycle"
ALLOWED_ISSUE_TYPES = CHAPTER_REVIEW_ISSUE_TYPES
ALLOWED_SEVERITIES = {"low", "medium", "high"}
MAX_CHAPTER_SUGGESTIONS = 20
MAX_REVIEW_COVERAGE_ATTEMPTS = 2
MAX_PATCH_COHERENCE_ATTEMPTS = 2
MAX_CHAPTER_REVIEW_OUTPUT_TOKENS = 10000
MAX_CHAPTER_PATCH_OUTPUT_TOKENS = 20000
CHAPTER_PATCH_CONTEXT_RADIUS = 2
STYLE_PATCH_MIN_LENGTH_RETENTION = 0.68
STYLE_PATCH_MIN_SEQUENCE_RETENTION = 0.72
STYLE_PATCH_MIN_SEMANTIC_CHARS = 12
STYLE_PRESERVATION_MARKERS = (
    "破折号",
    "标点",
    "短句",
    "句号",
    "逗号",
    "分号",
    "语序",
    "衔接",
    "节奏",
)
SEMANTIC_PATCH_ISSUE_TYPES = ALLOWED_ISSUE_TYPES - {
    "ai_style",
    "prose_rhythm",
    "grammar",
}
EXPECTED_AUDIT_DIMENSIONS = tuple(item["key"] for item in QUALITY_DIMENSIONS)
PARAGRAPH_REFERENCE_PATTERN = re.compile(
    r"第\s*(\d+)\s*(?:[-—~～至到]\s*(\d+)\s*)?段"
)


class ChapterReviewRequestError(RuntimeError):
    """保留流式审校失败时的请求预算和接收进度。"""

    def __init__(self, message: str, telemetry: dict[str, Any]) -> None:
        super().__init__(message)
        self.telemetry = telemetry


def build_dynamic_chapter_review_config(
    base_config: LLMConfig,
    messages: list[dict[str, str]],
    *,
    max_output_tokens: int = MAX_CHAPTER_REVIEW_OUTPUT_TOKENS,
) -> tuple[LLMConfig, dict[str, int]]:
    """按单章审校规模设置流活动超时、总超时和重试次数。"""
    prompt_chars = sum(len(str(message.get("content") or "")) for message in messages)
    estimated_input_tokens = max(1, prompt_chars)
    base_first_token_timeout = max(
        90,
        min(300, int(60 + estimated_input_tokens / 200)),
    )
    base_total_timeout = max(
        240,
        min(
            900,
            int(base_first_token_timeout + max_output_tokens / 15),
        ),
    )
    activity_timeout = math.ceil(
        max(float(base_config.timeout_seconds), base_first_token_timeout * 1.5)
    )
    total_timeout = math.ceil(
        max(
            float(base_config.total_timeout_seconds or 0),
            base_total_timeout * 1.5,
        )
    )
    max_retries = (
        0
        if estimated_input_tokens > 30000
        else min(base_config.max_retries, 1)
    )
    telemetry = {
        "prompt_chars": prompt_chars,
        "estimated_input_tokens": estimated_input_tokens,
        "activity_timeout_seconds": activity_timeout,
        # 保留旧字段，避免历史前端和任务记录读取失败。
        "first_token_timeout_seconds": activity_timeout,
        "total_timeout_seconds": total_timeout,
        "max_output_tokens": max_output_tokens,
        "max_retries": max_retries,
    }
    return (
        replace(
            base_config,
            timeout_seconds=float(activity_timeout),
            total_timeout_seconds=float(total_timeout),
            max_retries=max_retries,
        ),
        telemetry,
    )


def execute_streaming_chapter_review_request(
    base_config: LLMConfig,
    messages: list[dict[str, str]],
    *,
    max_output_tokens: int = MAX_CHAPTER_REVIEW_OUTPUT_TOKENS,
    on_activity: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """流式接收审校 JSON，完整接收后再统一解析和校验。"""
    request_config, telemetry = build_dynamic_chapter_review_config(
        base_config,
        messages,
        max_output_tokens=max_output_tokens,
    )
    started_at = time.monotonic()
    first_delta_at: float | None = None
    output_chars = 0

    def on_raw_delta(delta: str) -> None:
        nonlocal first_delta_at, output_chars
        if first_delta_at is None:
            first_delta_at = time.monotonic()
        output_chars += len(delta)

    client = LLMClient(request_config)
    try:
        raw_response, parsed = client.complete_json(
            messages,
            max_tokens=max_output_tokens,
            stream=True,
            on_raw_delta=on_raw_delta,
            on_activity=on_activity,
        )
    except Exception as exc:
        telemetry.update(
            {
                "streaming": True,
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
                "first_delta_seconds": (
                    round(first_delta_at - started_at, 3)
                    if first_delta_at is not None
                    else None
                ),
                "output_chars": output_chars,
                "transport": client.last_request_telemetry,
                "error": str(exc),
            }
        )
        raise ChapterReviewRequestError(str(exc), telemetry) from exc

    telemetry.update(
        {
            "streaming": True,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
            "first_delta_seconds": (
                round(first_delta_at - started_at, 3)
                if first_delta_at is not None
                else None
            ),
            "output_chars": output_chars,
            "transport": client.last_request_telemetry,
        }
    )
    return raw_response, parsed, telemetry


def extract_referenced_paragraph_indexes(text: str, paragraph_count: int) -> list[int]:
    """提取建议文本中明确出现的“第 N 段”及小范围段落区间。"""
    indexes: set[int] = set()
    for match in PARAGRAPH_REFERENCE_PATTERN.finditer(text or ""):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        lower, upper = sorted((start, end))
        if upper - lower > 20:
            continue
        indexes.update(index for index in range(lower, upper + 1) if 1 <= index <= paragraph_count)
    return sorted(indexes)


def _compact_chapter_review_context(chapter: Chapter) -> dict[str, Any]:
    """只提取本章审校真正需要的人物、世界、风格和章节目标，避免塞入整份快照。"""
    snapshot = chapter.context_snapshot or {}
    novel = snapshot.get("novel") if isinstance(snapshot.get("novel"), dict) else {}
    brief = novel.get("brief") if isinstance(novel.get("brief"), dict) else {}
    story_bible_wrapper = (
        snapshot.get("story_bible")
        if isinstance(snapshot.get("story_bible"), dict)
        else {}
    )
    story_bible = (
        story_bible_wrapper.get("content")
        if isinstance(story_bible_wrapper.get("content"), dict)
        else story_bible_wrapper
    )
    target = snapshot.get("target") if isinstance(snapshot.get("target"), dict) else {}
    task_input = (
        target.get("task_input")
        if isinstance(target.get("task_input"), dict)
        else {}
    )
    chapter_plan = (
        task_input.get("chapter_plan")
        if isinstance(task_input.get("chapter_plan"), dict)
        else {}
    )
    story_event = (
        task_input.get("story_event")
        if isinstance(task_input.get("story_event"), dict)
        else {}
    )
    bible_characters = (
        story_bible.get("main_characters")
        if isinstance(story_bible.get("main_characters"), list)
        else []
    )
    brief_characters = (
        brief.get("characters")
        if isinstance(brief.get("characters"), list)
        else []
    )

    def compact_character(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        return {
            key: raw.get(key)
            for key in (
                "name",
                "gender",
                "age",
                "occupation",
                "identity",
                "personality",
                "goal",
                "detailed_setting",
                "is_protagonist",
            )
            if raw.get(key) not in (None, "", [], {})
        }

    character_facts: list[dict[str, Any]] = []
    seen_characters: set[str] = set()
    # StoryBible 已吸收并锁定 brief.characters；只有历史快照缺失时才回退到 brief。
    character_source = bible_characters or brief_characters
    for raw in character_source:
        character = compact_character(raw)
        if not character:
            continue
        identity = str(character.get("name") or character.get("identity") or character)
        if identity in seen_characters:
            continue
        seen_characters.add(identity)
        character_facts.append(character)
        if len(character_facts) >= 12:
            break

    meme_pack = (
        snapshot.get("meme_reference_pack")
        if isinstance(snapshot.get("meme_reference_pack"), dict)
        else {}
    )
    meme_references = [
        {
            key: item.get(key)
            for key in (
                "entry_id",
                "phrase",
                "meaning",
                "suitable_scenes",
                "popularity_period",
                "scene_fit_score",
                "scene_fit",
            )
            if item.get(key) not in (None, "", [], {})
        }
        for item in (meme_pack.get("references") or [])[:5]
        if isinstance(item, dict)
    ]

    return {
        "work": {
            key: novel.get(key)
            for key in ("title", "genre", "premise")
            if novel.get(key) not in (None, "", [], {})
        },
        "chapter_plan": {
            key: chapter_plan.get(key)
            for key in ("title", "function", "core_event", "ending_hook", "participants")
            if chapter_plan.get(key) not in (None, "", [], {})
        },
        "story_event": {
            key: story_event.get(key)
            for key in (
                "event_title",
                "event_goal",
                "summary",
                "event_type",
                "core_conflict",
                "next_event_hook",
            )
            if story_event.get(key) not in (None, "", [], {})
        },
        "character_facts": character_facts,
        "world_rules": {
            key: (story_bible.get("world_rules") or {}).get(key)
            for key in ("background", "story_era", "story_location", "rules", "constraints")
            if isinstance(story_bible.get("world_rules"), dict)
            and (story_bible.get("world_rules") or {}).get(key) not in (None, "", [], {})
        },
        "style_rules": {
            key: (story_bible.get("style_rules") or {}).get(key)
            for key in (
                "style_reference",
                "anti_ai_rules",
                "forbidden_content",
                "tone_pacing_contract",
            )
            if isinstance(story_bible.get("style_rules"), dict)
            and (story_bible.get("style_rules") or {}).get(key) not in (None, "", [], {})
        },
        "meme_reference_pack": {
            "references": meme_references,
            "usage_plan": (snapshot.get("chapter_progress") or {}).get("meme_usage_plan", []),
        },
    }


def get_expected_audit_dimensions(chapter: Chapter) -> tuple[str, ...]:
    """只在正文最终确实采纳热梗时要求 meme_fit 审校。"""
    snapshot = chapter.context_snapshot or {}
    reference_pack = (
        snapshot.get("meme_reference_pack")
        if isinstance(snapshot.get("meme_reference_pack"), dict)
        else {}
    )
    chapter_progress = (
        snapshot.get("chapter_progress")
        if isinstance(snapshot.get("chapter_progress"), dict)
        else {}
    )
    usage = build_final_meme_usage(
        reference_pack,
        chapter_progress,
        chapter.content or "",
    )
    if usage.get("adopted_count", 0) > 0:
        return EXPECTED_AUDIT_DIMENSIONS
    return tuple(
        dimension
        for dimension in EXPECTED_AUDIT_DIMENSIONS
        if dimension != "meme_fit"
    )


def build_chapter_review_prompt(chapter: Chapter) -> list[dict[str, str]]:
    """把完整章节按稳定段落编号交给 Reviewer。"""
    paragraphs = split_chapter_paragraphs(chapter.content)
    chapter_contract = _compact_chapter_review_context(chapter)
    quality_contract = build_review_quality_contract(chapter.chapter_index)
    expected_dimensions = set(get_expected_audit_dimensions(chapter))
    quality_contract["audit_order"] = [
        item
        for item in quality_contract["audit_order"]
        if item.get("dimension") in expected_dimensions
    ]
    tone_pacing_contract = (
        chapter_contract.get("style_rules") or {}
    ).get("tone_pacing_contract") or {}
    if tone_pacing_contract:
        quality_contract["tone_pacing_contract"] = tone_pacing_contract
    payload = {
        "chapter_index": chapter.chapter_index,
        "title": chapter.title,
        "word_count": chapter.word_count,
        "chapter_contract": chapter_contract,
        "quality_contract": quality_contract,
        "paragraphs": [
            {"paragraph_index": index, "text": paragraph}
            for index, paragraph in enumerate(paragraphs, start=1)
        ],
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 NovelForge 单章 Reviewer；只能检查当前章节内部，不得评价跨章连续性、事件完成度或下一章走向。"
                "chapter_contract 仅用于核对本章人物、现实背景、目标和钩子，不得虚构正文外问题。"
                "按 quality_contract.audit_order 逐项返回 audit_results，覆盖因果/转场、行为与对白现实性、"
                "状态/视角、人物与事实、细节功能、AI语言和章末钩子；不能只查语法。"
                "若 tone_pacing_contract 非空，还要检查逐项手续、采购、规则或重复低落是否挤占喜剧推进，"
                "以及是否至少有两个由人物行动自然形成的喜剧节拍；问题归入 detail_relevance 或 prose_rhythm。"
                "破折号须定位为 ai_style：保留原意，改用准确标点、连接词、动作、语气或停顿；不得只机械删除或换一种破折号。"
                "meme_fit 必须单独核对每条已用热梗的真实含义、人物关系、情绪、required_setup、speech_act、response 和 plot_consequence；"
                "不能因原词已经出现就判通过。生硬时优先重建候选规定的微场景；若无法在不破坏人物和剧情的前提下成立，应明确建议删除，禁止库外热梗。"
                "只提可定位、可执行且保留原剧情功能的问题；不得重写正文，不把偏好当错误。"
                "每维给简短结论、代表段号和原文依据；只输出 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                "审校完整章节；无实际问题时 suggestions=[]。先 high/medium 后 low，最多返回 20 条；"
                "文字流畅不能替代行为、状态、常识和细节功能检查。\n"
                "输出格式：\n"
                '{"summary":"简短结论","audit_results":[{"dimension":"quality_contract.audit_order中的维度",'
                '"status":"pass|issue","paragraph_indexes":[1],"evidence":"对应段落的简短原文依据",'
                '"conclusion":"该维度为何通过或存在什么问题"}],'
                '"suggestions":[{"issue_type":"chapter_logic|scene_continuity|behavior_realism|'
                'dialogue_realism|state_continuity|viewpoint_knowledge|character_consistency|factual_plausibility|detail_relevance|meme_fit|'
                'prose_rhythm|grammar|ai_style|ending_hook",'
                '"severity":"low|medium|high","paragraph_indexes":[1],'
                '"problem":"问题是什么","evidence":"原文依据","reader_impact":"为什么会让读者困惑或出戏",'
                '"repair_scope":"应保留的剧情功能","suggestion":"明确修改建议"}]}\n\n'
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
        },
    ]


def _raw_suggestion_count(parsed: dict[str, Any]) -> int:
    raw = parsed.get("suggestions")
    if not isinstance(raw, list):
        raw = parsed.get("issues")
    return len(raw) if isinstance(raw, list) else 0


def build_review_coverage_report(
    parsed: dict[str, Any],
    paragraph_count: int,
    expected_dimensions: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """验证 Reviewer 是否逐项完成质量清单，而不是用空数组草率交差。"""
    raw_audits = parsed.get("audit_results")
    if not isinstance(raw_audits, list):
        raw_audits = parsed.get("audits")
    if not isinstance(raw_audits, list):
        raw_audits = []

    normalized: list[dict[str, Any]] = []
    covered: set[str] = set()
    expected_dimensions = expected_dimensions or EXPECTED_AUDIT_DIMENSIONS
    expected = set(expected_dimensions)
    for raw in raw_audits:
        if not isinstance(raw, dict):
            continue
        dimension = str(raw.get("dimension") or "").strip()
        if dimension not in expected or dimension in covered:
            continue
        status = str(raw.get("status") or raw.get("result") or "").strip().lower()
        if status not in {"pass", "issue"}:
            continue
        indexes = raw.get("paragraph_indexes")
        if not isinstance(indexes, list):
            indexes = [raw.get("paragraph_index")] if raw.get("paragraph_index") is not None else []
        paragraph_indexes = sorted(
            {
                int(index)
                for index in indexes
                if str(index).isdigit() and 1 <= int(index) <= paragraph_count
            }
        )
        evidence = str(raw.get("evidence") or "").strip()
        conclusion = str(raw.get("conclusion") or raw.get("summary") or "").strip()
        if not paragraph_indexes or not evidence or not conclusion:
            continue
        covered.add(dimension)
        normalized.append(
            {
                "dimension": dimension,
                "status": status,
                "paragraph_indexes": paragraph_indexes,
                "evidence": evidence,
                "conclusion": conclusion,
            }
        )

    missing = [dimension for dimension in expected_dimensions if dimension not in covered]
    return {
        "valid": not missing,
        "expected_count": len(expected_dimensions),
        "covered_count": len(covered),
        "coverage_percent": round((len(covered) / len(expected_dimensions)) * 100),
        "missing_dimensions": missing,
        "audit_results": normalized,
        "raw_audit_count": len(raw_audits),
    }


def build_chapter_review_coverage_retry_prompt(
    chapter: Chapter,
    missing_dimensions: list[str],
) -> list[dict[str, str]]:
    """覆盖不完整时只允许补做一次完整审校，避免无上限重试。"""
    expected_dimensions = get_expected_audit_dimensions(chapter)
    return [
        *build_chapter_review_prompt(chapter),
        {
            "role": "user",
            "content": (
                "上一次回答没有完成逐项检查，不能据此判定本章无问题。"
                f"缺失或缺少有效依据的维度：{', '.join(missing_dimensions) or '未知'}。"
                f"请重新返回完整 JSON；audit_results 必须恰好覆盖这 {len(expected_dimensions)} 个维度："
                f"{', '.join(expected_dimensions)}。"
                "每项必须包含 pass|issue、至少一个真实段落编号、简短原文依据和结论。"
                "同时保留所有实际发现的 suggestions，最多 20 条。"
            ),
        },
    ]


def build_rule_based_chapter_suggestions(chapter: Chapter) -> list[dict[str, Any]]:
    """把确定性中文节奏规则接入当前 Reviewer→Writer 闭环。"""
    suggestions: list[dict[str, Any]] = []
    for record in check_punctuation_style(chapter):
        payload = record.get("payload") or {}
        paragraph_index = payload.get("paragraph_index")
        if not str(paragraph_index or "").isdigit():
            continue
        suggestions.append(
            {
                "suggestion_index": len(suggestions) + 1,
                "issue_type": "prose_rhythm",
                "severity": record.get("severity") or "low",
                "paragraph_indexes": [int(paragraph_index)],
                "problem": str(record.get("message") or "连续短句造成机械切分。"),
                "evidence": str(payload.get("evidence") or ""),
                "reader_impact": "停顿密度与语义关系不匹配，容易形成机械罗列感。",
                "repair_scope": "保留原有信息和必要强调，只调整句间组织。",
                "suggestion": str(payload.get("suggestion") or "按语义合并短句。"),
                "source": "punctuation_style_checker",
            }
        )
    return suggestions


def merge_chapter_suggestions(
    rule_suggestions: list[dict[str, Any]],
    reviewer_suggestions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """规则结果优先，模型建议补充；同类型同段落不重复占用上限。"""
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[int, ...]]] = set()
    for suggestion in [*rule_suggestions, *reviewer_suggestions]:
        key = (
            str(suggestion.get("issue_type") or ""),
            tuple(suggestion.get("paragraph_indexes") or []),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append({**suggestion, "suggestion_index": len(merged) + 1})
        if len(merged) >= MAX_CHAPTER_SUGGESTIONS:
            break
    return merged


def normalize_chapter_suggestions(parsed: dict[str, Any], paragraph_count: int) -> list[dict[str, Any]]:
    """清洗 Reviewer 建议，拒绝无法定位的泛化意见。"""
    raw_suggestions = parsed.get("suggestions")
    if not isinstance(raw_suggestions, list):
        raw_suggestions = parsed.get("issues")
    if not isinstance(raw_suggestions, list):
        return []

    suggestions: list[dict[str, Any]] = []
    for raw in raw_suggestions:
        if not isinstance(raw, dict):
            continue
        problem = str(raw.get("problem") or raw.get("message") or "").strip()
        evidence = str(raw.get("evidence") or "").strip()
        suggestion = str(raw.get("suggestion") or "").strip()
        indexes = raw.get("paragraph_indexes")
        if not isinstance(indexes, list):
            indexes = [raw.get("paragraph_index")] if raw.get("paragraph_index") is not None else []
        paragraph_indexes = sorted(
            {
                int(index)
                for index in indexes
                if str(index).isdigit() and 1 <= int(index) <= paragraph_count
            }
            | set(
                extract_referenced_paragraph_indexes(
                    "\n".join((problem, evidence, suggestion)),
                    paragraph_count,
                )
            )
        )
        if not paragraph_indexes or not problem or not suggestion:
            continue
        issue_type = str(raw.get("issue_type") or "grammar").strip()
        severity = str(raw.get("severity") or "medium").strip().lower()
        suggestions.append(
            {
                "suggestion_index": len(suggestions) + 1,
                "issue_type": issue_type if issue_type in ALLOWED_ISSUE_TYPES else "grammar",
                "severity": severity if severity in ALLOWED_SEVERITIES else "medium",
                "paragraph_indexes": paragraph_indexes,
                "problem": problem,
                "evidence": evidence,
                "reader_impact": str(raw.get("reader_impact") or "").strip(),
                "repair_scope": str(raw.get("repair_scope") or "").strip(),
                "suggestion": suggestion,
                "source": "llm_reviewer",
            }
        )
        if len(suggestions) >= MAX_CHAPTER_SUGGESTIONS:
            break
    return suggestions


def _chapter_patch_scope(
    suggestions: list[dict[str, Any]],
    paragraph_count: int,
    retry_paragraph_indexes: set[int] | None = None,
) -> tuple[set[int], set[int]]:
    """返回 Reviewer 目标段，以及目标段前后各两段组成的可修订窗口。"""
    target_indexes = {
        int(paragraph_index)
        for suggestion in suggestions
        for paragraph_index in (suggestion.get("paragraph_indexes") or [])
        if str(paragraph_index).isdigit()
        and 1 <= int(paragraph_index) <= paragraph_count
    }
    if retry_paragraph_indexes is not None:
        target_indexes &= retry_paragraph_indexes
    editable_indexes = {
        index
        for target_index in target_indexes
        for index in range(
            max(1, target_index - CHAPTER_PATCH_CONTEXT_RADIUS),
            min(paragraph_count, target_index + CHAPTER_PATCH_CONTEXT_RADIUS) + 1,
        )
    }
    return target_indexes, editable_indexes


def build_chapter_patch_prompt(
    chapter: Chapter,
    suggestions: list[dict[str, Any]],
    retry_paragraph_indexes: list[int] | None = None,
    validation_errors: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """让正文 Writer 只针对 Reviewer 建议返回最小段落替换。"""
    paragraphs = split_chapter_paragraphs(chapter.content)
    target_indexes, editable_indexes = _chapter_patch_scope(
        suggestions,
        len(paragraphs),
        set(retry_paragraph_indexes) if retry_paragraph_indexes is not None else None,
    )
    payload = {
        "chapter_index": chapter.chapter_index,
        "title": chapter.title,
        "chapter_contract": _compact_chapter_review_context(chapter),
        "target_suggestions": suggestions,
        "retry_paragraph_indexes": retry_paragraph_indexes or [],
        "target_paragraph_indexes": sorted(target_indexes),
        "editable_paragraph_indexes": sorted(editable_indexes),
        "context_radius": CHAPTER_PATCH_CONTEXT_RADIUS,
        "previous_validation_errors": [
            {
                "code": item.get("code", ""),
                "paragraph_index": item.get("paragraph_index"),
                "message": item.get("message", ""),
                "validation": item.get("validation", {}),
            }
            for item in (validation_errors or [])
        ],
        "paragraphs": [
            {
                "paragraph_index": index,
                "scope": "target" if index in target_indexes else "context_neighbor",
                "text": paragraph,
            }
            for index, paragraph in enumerate(paragraphs, start=1)
            if index in editable_indexes
        ],
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 NovelForge 局部修订 Writer，只执行 Reviewer 建议；不得重写整章、改变剧情/人物/事实或越界修改。"
                "保留 repair_scope 和必要因果；转场、动机、对白、道具问题可联改被点名段落，不能生硬补解释。"
                "语言类问题只改标点、连接、语序和必要动作，保留原段解释、事实、关键对白；不得拿局部 evidence 替换整段，"
                "异常缩短或丢失关键表述的补丁会被拒绝。"
                "目标段提供前后各 2 段：邻段默认不动；仅为消除重复、冲突或承接断裂而改，并在 reason 说明。"
                "输出前连续阅读修改后的整个窗口，确认提问—回答、动作—反应、指代和说话人仍能逐句承接。"
                "重复内容应合并后 delete 冗余段；无效细节应压缩，不得换成随机环境/品牌/数字，也不新增刻板比喻、万能拟人、空泛总结或假钩子。"
                "处理破折号问题时保留原意，用准确标点、连接词、动作、语气或停顿；不得只删符号或换另一种破折号。"
                "热梗不自然时，必须按候选的真实含义、人物关系、情绪及微场景要求调整；"
                "若不改变人物和剧情仍无法成立，应删除该热梗，禁止为了数量硬留，也禁止库外热梗。"
                "replace 只返回完整单段；delete 仅删已合并冗余。按 paragraph_index 绑定原文，不要返回 old_text。"
                "同段建议合并，无需修改不返回；new_text 不得含多段。只输出 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                "根据 target_suggestions 做一次定向修改，只返回最小必要段落补丁。\n"
                "输出格式：\n"
                '{"patches":[{"paragraph_index":3,"operation":"replace",'
                '"new_text":"替换后的完整段落",'
                '"reason":"对应的建议及修改理由"},'
                '{"paragraph_index":4,"operation":"delete",'
                '"new_text":"","reason":"内容已合并到第3段，删除重复段"}]}\n\n'
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
        },
    ]


def _semantic_patch_suggestions(
    suggestions: list[dict[str, Any]],
    paragraph_index: int,
) -> list[dict[str, Any]]:
    """返回会改变剧情、行为或对话含义的相关建议。"""
    relevant: list[dict[str, Any]] = []
    for suggestion in suggestions:
        if str(suggestion.get("issue_type") or "") not in SEMANTIC_PATCH_ISSUE_TYPES:
            continue
        target_indexes = {
            int(index)
            for index in (suggestion.get("paragraph_indexes") or [])
            if str(index).isdigit()
        }
        if any(
            abs(paragraph_index - target_index) <= CHAPTER_PATCH_CONTEXT_RADIUS
            for target_index in target_indexes
        ):
            relevant.append(suggestion)
    return relevant


def build_chapter_patch_coherence_prompt(
    chapter: Chapter,
    suggestions: list[dict[str, Any]],
    patches: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """只复检语义补丁的局部修改前后窗口。"""
    paragraphs = split_chapter_paragraphs(chapter.content)
    semantic_patches = [
        patch
        for patch in patches
        if _semantic_patch_suggestions(suggestions, patch["paragraph_index"])
    ]
    patch_by_index = {patch["paragraph_index"]: patch for patch in semantic_patches}
    window_indexes = sorted(
        {
            index
            for patch in semantic_patches
            for index in range(
                max(1, patch["paragraph_index"] - CHAPTER_PATCH_CONTEXT_RADIUS),
                min(
                    len(paragraphs),
                    patch["paragraph_index"] + CHAPTER_PATCH_CONTEXT_RADIUS,
                )
                + 1,
            )
        }
    )
    relevant_suggestions = [
        suggestion
        for suggestion in suggestions
        if any(
            _semantic_patch_suggestions([suggestion], patch["paragraph_index"])
            for patch in semantic_patches
        )
    ]
    before = [
        {"paragraph_index": index, "text": paragraphs[index - 1]}
        for index in window_indexes
    ]
    after = []
    for index in window_indexes:
        patch = patch_by_index.get(index)
        after.append(
            {
                "paragraph_index": index,
                "text": (
                    ""
                    if patch and patch.get("operation") == "delete"
                    else patch["new_text"]
                    if patch
                    else paragraphs[index - 1]
                ),
                "operation": patch.get("operation", "unchanged") if patch else "unchanged",
            }
        )
    payload = {
        "chapter_index": chapter.chapter_index,
        "target_suggestions": relevant_suggestions,
        "patch_indexes": sorted(patch_by_index),
        "before_window": before,
        "after_window": after,
    }
    return [
        {
            "role": "system",
            "content": (
                "你是局部补丁连续性守卫，只判断 after_window 能否在不阅读章外内容时顺畅承接。"
                "逐个 patch_index 检查：原问题是否被修复；repair_scope 是否保留；"
                "提问—回答、动作—反应、因果、指代、说话人和人物意图是否连续；"
                "未修改邻段是否仍有明确前提。不要提出润色偏好，也不要改写正文。只输出 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                '格式：{"checks":[{"paragraph_index":1,"status":"pass|fail",'
                '"broken_links":["具体断裂点"],"reason":"简短依据"}]}\n'
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
        },
    ]


def normalize_chapter_patch_coherence_checks(
    parsed: dict[str, Any],
    patches: list[dict[str, Any]],
    suggestions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按段隔离不连贯的语义补丁；纯语言补丁不增加模型复检。"""
    raw_checks = parsed.get("checks")
    if not isinstance(raw_checks, list):
        raw_checks = []
    checks_by_index = {
        int(item["paragraph_index"]): item
        for item in raw_checks
        if isinstance(item, dict) and str(item.get("paragraph_index") or "").isdigit()
    }
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for patch in patches:
        paragraph_index = patch["paragraph_index"]
        if not _semantic_patch_suggestions(suggestions, paragraph_index):
            patch["coherence_validation"] = {"status": "skipped_style_only"}
            accepted.append(patch)
            continue
        check = checks_by_index.get(paragraph_index) or {}
        broken_links = [
            str(item).strip()
            for item in (check.get("broken_links") or [])
            if str(item).strip()
        ]
        status = str(check.get("status") or "").strip().lower()
        validation = {
            "status": status or "missing",
            "broken_links": broken_links,
            "reason": str(check.get("reason") or "").strip(),
        }
        patch["coherence_validation"] = validation
        if status == "pass" and not broken_links:
            accepted.append(patch)
            continue
        rejected.append(
            {
                "code": "neighbor_coherence",
                "message": (
                    f"第 {paragraph_index} 段修改后与前后文无法稳定承接："
                    + ("；".join(broken_links) or validation["reason"] or "复检结果缺失")
                ),
                "paragraph_index": paragraph_index,
                "validation": validation,
            }
        )
    return accepted, rejected


def validate_chapter_patch_coherence(
    chapter: Chapter,
    suggestions: list[dict[str, Any]],
    patches: list[dict[str, Any]],
    reviewer_config: LLMConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """批量执行小窗口语义复检；格式异常时重试一次，最终失败则保留原文。"""
    semantic_patches = [
        patch
        for patch in patches
        if _semantic_patch_suggestions(suggestions, patch["paragraph_index"])
    ]
    if not semantic_patches:
        accepted, rejected = normalize_chapter_patch_coherence_checks(
            {},
            patches,
            suggestions,
        )
        return accepted, rejected, {"status": "skipped_style_only"}
    semantic_indexes = {
        patch["paragraph_index"] for patch in semantic_patches
    }
    base_messages = build_chapter_patch_coherence_prompt(
        chapter,
        suggestions,
        patches,
    )
    attempts: list[dict[str, Any]] = []
    last_error = ""
    # 小窗口复检最多发起两次完整请求；关闭请求内部重试，控制额外耗时与费用。
    coherence_config = replace(reviewer_config, max_retries=0)
    for attempt in range(1, MAX_PATCH_COHERENCE_ATTEMPTS + 1):
        messages = base_messages
        if attempt > 1:
            messages = [
                *base_messages,
                {
                    "role": "user",
                    "content": (
                        "上一次返回为空、不是合法 JSON 或缺少目标段落检查。"
                        "请只返回完整 JSON，不要解释："
                        '{"checks":[{"paragraph_index":1,"status":"pass|fail",'
                        '"broken_links":[],"reason":"简短依据"}]}'
                    ),
                },
            ]
        raw = ""
        telemetry: dict[str, Any] = {}
        try:
            raw, parsed, telemetry = execute_streaming_chapter_review_request(
                coherence_config,
                messages,
                max_output_tokens=1800,
            )
            raw_checks = parsed.get("checks")
            valid_check_indexes = {
                int(item["paragraph_index"])
                for item in (raw_checks if isinstance(raw_checks, list) else [])
                if isinstance(item, dict)
                and str(item.get("paragraph_index") or "").isdigit()
                and str(item.get("status") or "").strip().lower() in {"pass", "fail"}
            }
            missing_indexes = semantic_indexes - valid_check_indexes
            if missing_indexes:
                raise ValueError(
                    "复检 JSON 缺少目标段落检查："
                    + "、".join(str(index) for index in sorted(missing_indexes))
                )
        except Exception as exc:
            last_error = str(exc)
            attempt_record: dict[str, Any] = {
                "attempt": attempt,
                "status": "failed",
                "error": last_error,
            }
            if isinstance(exc, ChapterReviewRequestError):
                attempt_record["request_telemetry"] = exc.telemetry
            elif telemetry:
                attempt_record["request_telemetry"] = telemetry
            if raw:
                attempt_record["raw_response"] = raw[:2_000]
                attempt_record["raw_response_truncated"] = len(raw) > 2_000
            attempts.append(attempt_record)
            continue

        accepted, rejected = normalize_chapter_patch_coherence_checks(
            parsed,
            patches,
            suggestions,
        )
        attempts.append(
            {
                "attempt": attempt,
                "status": "completed",
                "raw_response_chars": len(raw),
                "request_telemetry": telemetry,
                "rejected_count": len(rejected),
            }
        )
        return accepted, rejected, {
            "status": "completed",
            "attempt": attempt,
            "attempts": attempts,
            "raw_response": raw[:20_000],
            "raw_response_truncated": len(raw) > 20_000,
            "request_telemetry": telemetry,
            "rejected_count": len(rejected),
        }

    accepted = [
        patch for patch in patches if patch["paragraph_index"] not in semantic_indexes
    ]
    rejected = [
        {
            "code": "coherence_check_failed",
            "message": (
                f"第 {index} 段局部连续性复检连续两次未返回有效结果，"
                f"已保留原文：{last_error}"
            ),
            "paragraph_index": index,
        }
        for index in sorted(semantic_indexes)
    ]
    return accepted, rejected, {
        "status": "failed",
        "attempts": attempts,
        "error": last_error,
    }


def normalize_chapter_writer_patches(
    parsed: dict[str, Any],
    chapter: Chapter,
    suggestions: list[dict[str, Any]],
    *,
    retry_paragraph_indexes: set[int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[int]]:
    """逐条清洗 Writer 补丁，原文由服务端按段落编号注入。

    Writer 返回的 old_text 仅用于审计，不参与匹配，避免模型摘录证据片段或替换标点
    导致整批补丁失败。无效补丁被隔离，其余补丁仍可继续应用。
    """
    raw_patches = parsed.get("patches")
    if not isinstance(raw_patches, list):
        return [], [{"code": "invalid_payload", "message": "正文模型未返回 patches 数组"}], []

    paragraphs = split_chapter_paragraphs(chapter.content)
    target_indexes, allowed_indexes = _chapter_patch_scope(
        suggestions,
        len(paragraphs),
        retry_paragraph_indexes,
    )

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    canonicalized_old_text_indexes: list[int] = []
    seen: set[int] = set()

    def reject(
        *,
        code: str,
        message: str,
        paragraph_index: int | None = None,
        raw_patch: Any = None,
    ) -> None:
        rejected.append(
            {
                "code": code,
                "message": message,
                "paragraph_index": paragraph_index,
                "raw_patch": raw_patch,
            }
        )

    for raw_patch in raw_patches:
        if not isinstance(raw_patch, dict):
            reject(code="invalid_patch", message="补丁不是 JSON 对象", raw_patch=raw_patch)
            continue
        raw_index = raw_patch.get("paragraph_index")
        if not str(raw_index or "").isdigit():
            reject(code="invalid_index", message="缺少有效 paragraph_index", raw_patch=raw_patch)
            continue
        paragraph_index = int(raw_index)
        raw_chapter_index = raw_patch.get("chapter_index")
        chapter_index_canonicalized = (
            raw_chapter_index is not None
            and (
                not str(raw_chapter_index).isdigit()
                or int(raw_chapter_index) != chapter.chapter_index
            )
        )
        if paragraph_index < 1 or paragraph_index > len(paragraphs):
            reject(
                code="out_of_range",
                message=f"第 {paragraph_index} 段不存在",
                paragraph_index=paragraph_index,
                raw_patch=raw_patch,
            )
            continue
        if paragraph_index not in allowed_indexes:
            reject(
                code="out_of_scope",
                message=(
                    f"第 {paragraph_index} 段不在 Reviewer 目标段前后 "
                    f"{CHAPTER_PATCH_CONTEXT_RADIUS} 段的修订窗口内"
                ),
                paragraph_index=paragraph_index,
                raw_patch=raw_patch,
            )
            continue
        if paragraph_index in seen:
            reject(
                code="duplicate",
                message=f"第 {paragraph_index} 段存在重复补丁",
                paragraph_index=paragraph_index,
                raw_patch=raw_patch,
            )
            continue

        expected = paragraphs[paragraph_index - 1]
        model_old_text = str(raw_patch.get("old_text") or "").strip()
        if model_old_text and model_old_text != expected:
            canonicalized_old_text_indexes.append(paragraph_index)
        operation = str(raw_patch.get("operation") or "replace").strip().lower()
        if operation not in {"replace", "delete"}:
            reject(
                code="invalid_operation",
                message=f"第 {paragraph_index} 段 operation 必须是 replace 或 delete",
                paragraph_index=paragraph_index,
                raw_patch=raw_patch,
            )
            continue
        new_text = str(raw_patch.get("new_text") or "").strip()
        if operation == "replace" and not new_text:
            reject(
                code="empty_new_text",
                message=f"第 {paragraph_index} 段缺少 new_text",
                paragraph_index=paragraph_index,
                raw_patch=raw_patch,
            )
            continue
        if operation == "delete" and new_text:
            reject(
                code="delete_with_text",
                message=f"第 {paragraph_index} 段删除补丁不应包含 new_text",
                paragraph_index=paragraph_index,
                raw_patch=raw_patch,
            )
            continue
        if operation == "replace" and new_text == expected:
            reject(
                code="no_change",
                message=f"第 {paragraph_index} 段没有发生变化",
                paragraph_index=paragraph_index,
                raw_patch=raw_patch,
            )
            continue
        if operation == "replace" and "\n\n" in new_text:
            reject(
                code="multiple_paragraphs",
                message=f"第 {paragraph_index} 段补丁包含多个段落",
                paragraph_index=paragraph_index,
                raw_patch=raw_patch,
            )
            continue

        preservation_policy = _style_patch_preservation_policy(
            suggestions,
            paragraph_index,
        )
        preservation_validation = None
        if operation == "replace" and preservation_policy is not None:
            preservation_validation = _style_patch_retention_report(expected, new_text)
            if not preservation_validation["valid"]:
                reject(
                    code="information_loss",
                    message=(
                        f"第 {paragraph_index} 段仅需修正{preservation_policy}，"
                        "但补丁删减了过多原有信息；必须保留完整解释、关键对白和因果内容"
                    ),
                    paragraph_index=paragraph_index,
                    raw_patch=raw_patch,
                )
                rejected[-1]["validation"] = preservation_validation
                continue

        reason = str(raw_patch.get("reason") or "").strip()
        if paragraph_index not in target_indexes and len(reason) < 4:
            reject(
                code="missing_neighbor_reason",
                message=f"第 {paragraph_index} 段属于邻段，必须说明重复或冲突处理理由",
                paragraph_index=paragraph_index,
                raw_patch=raw_patch,
            )
            continue

        accepted.append(
            {
                "chapter": chapter,
                "chapter_index": chapter.chapter_index,
                "chapter_binding": {
                    "source": "server",
                    "model_value": raw_chapter_index,
                    "canonicalized": chapter_index_canonicalized,
                },
                "paragraph_index": paragraph_index,
                "paragraph_id": f"ch{chapter.chapter_index}-p{paragraph_index}",
                "base_content_hash": text_hash(chapter.content or ""),
                "old_text_hash": text_hash(expected),
                "old_text": expected,
                "new_text": new_text,
                "operation": operation,
                "scope": "target" if paragraph_index in target_indexes else "context_neighbor",
                "reason": reason or "单章审校定向修改",
                "information_retention": preservation_validation or {},
            }
        )
        seen.add(paragraph_index)

    return accepted, rejected, sorted(set(canonicalized_old_text_indexes))


def _style_patch_preservation_policy(
    suggestions: list[dict[str, Any]],
    paragraph_index: int,
) -> str | None:
    """识别只能改语言形式、不能删减原段信息的局部建议。"""
    for suggestion in suggestions:
        target_indexes = {
            int(index)
            for index in (suggestion.get("paragraph_indexes") or [])
            if str(index).isdigit()
        }
        if not any(
            abs(paragraph_index - target_index) <= CHAPTER_PATCH_CONTEXT_RADIUS
            for target_index in target_indexes
        ):
            continue
        source = str(suggestion.get("source") or "")
        issue_type = str(suggestion.get("issue_type") or "")
        description = " ".join(
            str(suggestion.get(key) or "")
            for key in (
                "problem",
                "evidence",
                "suggestion",
                "repair_scope",
            )
        )
        if source == "punctuation_style_checker":
            return "标点或短句组织"
        if issue_type in {"ai_style", "prose_rhythm", "grammar"} and any(
            marker in description for marker in STYLE_PRESERVATION_MARKERS
        ):
            return "标点、语序或语言节奏"
    return None


def _style_patch_retention_report(old_text: str, new_text: str) -> dict[str, Any]:
    """低成本判断局部语言补丁是否误删原段信息。"""
    old_semantic = _semantic_characters(old_text)
    new_semantic = _semantic_characters(new_text)
    if len(old_semantic) < STYLE_PATCH_MIN_SEMANTIC_CHARS:
        return {
            "valid": True,
            "skipped": "short_paragraph",
            "old_semantic_chars": len(old_semantic),
            "new_semantic_chars": len(new_semantic),
        }
    length_retention = len(new_semantic) / max(1, len(old_semantic))
    matching_chars = sum(
        block.size
        for block in SequenceMatcher(
            None,
            old_semantic,
            new_semantic,
            autojunk=False,
        ).get_matching_blocks()
    )
    sequence_retention = matching_chars / max(1, len(old_semantic))
    valid = (
        length_retention >= STYLE_PATCH_MIN_LENGTH_RETENTION
        and sequence_retention >= STYLE_PATCH_MIN_SEQUENCE_RETENTION
    )
    return {
        "valid": valid,
        "old_semantic_chars": len(old_semantic),
        "new_semantic_chars": len(new_semantic),
        "length_retention": round(length_retention, 4),
        "sequence_retention": round(sequence_retention, 4),
        "min_length_retention": STYLE_PATCH_MIN_LENGTH_RETENTION,
        "min_sequence_retention": STYLE_PATCH_MIN_SEQUENCE_RETENTION,
    }


def _semantic_characters(text: str) -> str:
    return "".join(
        re.findall(r"[\u3400-\u9fffA-Za-z0-9]", str(text or "").casefold())
    )


def apply_chapter_patches_with_isolation(
    chapter: Chapter,
    patches: list[dict[str, Any]],
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """优先整批应用；字数等守卫失败时逐条隔离，保留仍然安全的补丁。"""
    try:
        content, validation = apply_paragraph_patches(chapter, patches)
        return content, validation, patches, []
    except ValueError as batch_error:
        message = str(batch_error)
        if "已被其他操作修改" in message or "版本已变化" in message:
            raise

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    # 先应用缩短或增量较小的修改，避免一个过长补丁挤掉其他有效修复。
    candidates = sorted(
        patches,
        key=lambda patch: (
            len(patch["new_text"]) - len(patch["old_text"]),
            patch["paragraph_index"],
        ),
    )
    for patch in candidates:
        try:
            # 每次都基于原始段落索引校验当前候选集合。这样即使前一个
            # 补丁删除了邻段，后续补丁也不会因段落位移误改其它内容。
            apply_paragraph_patches(
                chapter,
                [*accepted, patch],
            )
        except ValueError as exc:
            rejected.append(
                {
                    "code": "application_guard",
                    "message": str(exc),
                    "paragraph_index": patch["paragraph_index"],
                }
            )
            continue
        accepted.append(patch)

    if not accepted:
        return chapter.content or "", None, [], rejected
    # 用原始章节和最终保留补丁重算一次完整校验报告。
    content, validation = apply_paragraph_patches(
        chapter,
        sorted(accepted, key=lambda patch: patch["paragraph_index"]),
    )
    return (
        content,
        validation,
        sorted(accepted, key=lambda patch: patch["paragraph_index"]),
        rejected,
    )


def count_rejected_patches(items: list[dict[str, Any]]) -> int:
    indexed = {
        int(item["paragraph_index"])
        for item in items
        if item.get("paragraph_index") is not None
    }
    return len(indexed) + sum(
        item.get("paragraph_index") is None for item in items
    )


def _persist_suggestions(
    db: Session,
    novel: Novel,
    chapter: Chapter,
    task: GenerationTask,
    suggestions: list[dict[str, Any]],
) -> list[ReviewIssue]:
    """保存本轮明确建议，并把同来源旧开放记录标记为已替代。"""
    old_issues = db.scalars(
        select(ReviewIssue).where(
            ReviewIssue.novel_id == novel.id,
            ReviewIssue.chapter_id == chapter.id,
            ReviewIssue.status.in_(["open", "system_deferred"]),
        )
    ).all()
    for issue in old_issues:
        if (issue.payload or {}).get("source") == CHAPTER_REVIEW_SOURCE:
            issue.status = "superseded"

    records = [
        ReviewIssue(
            novel_id=novel.id,
            chapter_id=chapter.id,
            issue_type=suggestion["issue_type"],
            severity=suggestion["severity"],
            status="open",
            message=suggestion["problem"],
            payload={
                "source": CHAPTER_REVIEW_SOURCE,
                "task_id": str(task.id),
                "cycle": 1,
                "suggestion_index": suggestion["suggestion_index"],
                "paragraph_indexes": suggestion["paragraph_indexes"],
                "evidence": suggestion["evidence"],
                "reader_impact": suggestion.get("reader_impact", ""),
                "repair_scope": suggestion.get("repair_scope", ""),
                "suggestion": suggestion["suggestion"],
                "review_source": suggestion.get("source", "llm_reviewer"),
                "auto_generated": True,
                "requires_user_action": False,
            },
        )
        for suggestion in suggestions
    ]
    db.add_all(records)
    db.commit()
    for record in records:
        db.refresh(record)
    return records


def _mark_unapplied_issues(
    db: Session,
    issues: list[ReviewIssue],
    *,
    error: str = "",
) -> None:
    for issue in issues:
        issue.status = "system_deferred"
        issue.payload = {
            **(issue.payload or {}),
            "auto_repair": {
                "status": "deferred",
                "reason": "writer_patch_failed" if error else "writer_returned_no_patch",
                "message": error or "正文模型未为该建议生成可应用的段落补丁。",
            },
        }
    db.commit()


def review_and_revise_chapter_once(
    *,
    db: Session,
    novel: Novel,
    chapter: Chapter,
    task: GenerationTask,
    reviewer_config: LLMConfig | None,
    writer_config: LLMConfig | None,
    story_event_id: UUID | None = None,
    progress: int = 0,
) -> dict[str, Any]:
    """执行且只执行一次 Reviewer -> Writer 局部补丁闭环。"""
    result: dict[str, Any] = {
        "status": "skipped",
        "cycle_count": 0,
        "suggestions": [],
        "issue_count": 0,
        "patch_count": 0,
        "patch_ids": [],
        "resolved_count": 0,
        "deferred_count": 0,
        "discarded_patch_indexes": [],
        "rejected_patches": [],
        "rejected_patch_count": 0,
        "writer_attempts": [],
        "writer_retry_count": 0,
        "canonicalized_old_text_indexes": [],
        "application_rejections": [],
        "review_attempts": [],
        "review_coverage": {},
        "final_coherence_validation": {},
        "error": "",
    }
    review_progress = min(76, max(1, progress + 1))
    revision_progress = min(77, max(2, progress + 2))
    emit_task_event(
        db,
        task,
        event_type="chapter_review",
        step_key=f"chapter_{chapter.chapter_index}_review",
        status="running",
        title=f"正在审校第 {chapter.chapter_index} 章",
        message="逐项检查章内因果、行为对白、状态常识、细节效率、语言节奏和章末钩子",
        progress=review_progress,
        chapter_id=chapter.id,
        chapter_index=chapter.chapter_index,
    )
    if reviewer_config is None:
        result["status"] = "failed"
        result["error"] = "未配置可用的审校模型 API"
        emit_task_event(
            db,
            task,
            event_type="chapter_review",
            step_key=f"chapter_{chapter.chapter_index}_review",
            status="failed",
            title=f"第 {chapter.chapter_index} 章审校未执行",
            message=result["error"],
            progress=review_progress,
            chapter_id=chapter.id,
            chapter_index=chapter.chapter_index,
            payload={
                "review_attempts": result.get("review_attempts", []),
                "review_coverage": result.get("review_coverage", {}),
            },
        )
        emit_task_event(
            db,
            task,
            event_type="chapter_revision",
            step_key=f"chapter_{chapter.chapter_index}_revision",
            status="failed",
            title=f"第 {chapter.chapter_index} 章定向修改未执行",
            message="审校阶段未完成，没有可执行的修改建议",
            progress=revision_progress,
            chapter_id=chapter.id,
            chapter_index=chapter.chapter_index,
        )
        return result

    try:
        rule_suggestions = build_rule_based_chapter_suggestions(chapter)
        paragraph_count = len(split_chapter_paragraphs(chapter.content))
        expected_dimensions = get_expected_audit_dimensions(chapter)
        reviewer_suggestions: list[dict[str, Any]] = []
        coverage: dict[str, Any] = {}
        for attempt in range(1, MAX_REVIEW_COVERAGE_ATTEMPTS + 1):
            messages = (
                build_chapter_review_prompt(chapter)
                if attempt == 1
                else build_chapter_review_coverage_retry_prompt(
                    chapter,
                    coverage.get("missing_dimensions", []),
                )
            )
            request_config, request_budget = build_dynamic_chapter_review_config(
                reviewer_config,
                messages,
            )
            emit_task_event(
                db,
                task,
                event_type="chapter_review",
                step_key=f"chapter_{chapter.chapter_index}_review",
                status="running",
                title=f"正在流式审校第 {chapter.chapter_index} 章",
                message=(
                    f"第 {attempt} 次检查；输入约 {request_budget['prompt_chars']} 字符，"
                    f"无流活动超时 {int(request_config.timeout_seconds)} 秒，"
                    f"总超时 {int(request_config.total_timeout_seconds or 0)} 秒"
                ),
                progress=review_progress,
                chapter_id=chapter.id,
                chapter_index=chapter.chapter_index,
                payload={"request_budget": request_budget, "review_attempt": attempt},
            )
            last_activity_notice_at = 0.0

            def report_review_activity(activity: dict[str, Any]) -> None:
                nonlocal last_activity_notice_at
                now = time.monotonic()
                if now - last_activity_notice_at < 10:
                    return
                last_activity_notice_at = now
                reasoning_chars = int(activity.get("reasoning_chars") or 0)
                output_chars = int(activity.get("output_chars") or 0)
                emit_task_event(
                    db,
                    task,
                    event_type="chapter_review",
                    step_key=f"chapter_{chapter.chapter_index}_review",
                    status="running",
                    title=f"深度思考模型正在审校第 {chapter.chapter_index} 章",
                    message=(
                        f"连接持续活跃；已接收思考 {reasoning_chars} 字符、"
                        f"最终输出 {output_chars} 字符"
                    ),
                    progress=review_progress,
                    chapter_id=chapter.id,
                    chapter_index=chapter.chapter_index,
                    payload={
                        "stream_activity": activity,
                        "request_budget": request_budget,
                        "review_attempt": attempt,
                    },
                )
            try:
                raw_response, parsed, request_telemetry = (
                    execute_streaming_chapter_review_request(
                        reviewer_config,
                        messages,
                        on_activity=report_review_activity,
                    )
                )
            except ChapterReviewRequestError as exc:
                result["review_attempts"].append(
                    {
                        "attempt": attempt,
                        "status": "failed",
                        **exc.telemetry,
                    }
                )
                raise
            normalized_attempt = normalize_chapter_suggestions(parsed, paragraph_count)
            reviewer_suggestions = merge_chapter_suggestions(
                reviewer_suggestions,
                normalized_attempt,
            )
            coverage = build_review_coverage_report(
                parsed,
                paragraph_count,
                expected_dimensions,
            )
            attempt_record = {
                "attempt": attempt,
                "status": "completed",
                "raw_response_chars": len(raw_response),
                "raw_suggestion_count": _raw_suggestion_count(parsed),
                "normalized_suggestion_count": len(normalized_attempt),
                "rejected_suggestion_count": max(
                    0,
                    _raw_suggestion_count(parsed) - len(normalized_attempt),
                ),
                **{
                    key: coverage[key]
                    for key in (
                        "valid",
                        "expected_count",
                        "covered_count",
                        "coverage_percent",
                        "missing_dimensions",
                        "raw_audit_count",
                    )
                },
                **request_telemetry,
            }
            result["review_attempts"].append(attempt_record)
            if coverage["valid"]:
                break
            if attempt < MAX_REVIEW_COVERAGE_ATTEMPTS:
                emit_task_event(
                    db,
                    task,
                    event_type="chapter_review",
                    step_key=f"chapter_{chapter.chapter_index}_review",
                    status="running",
                    title=f"第 {chapter.chapter_index} 章审校覆盖不足，正在补充检查",
                    message=(
                        f"仅完成 {coverage['covered_count']}/{coverage['expected_count']} 个维度，"
                        "正在重新执行一次完整审校"
                    ),
                    progress=review_progress,
                    chapter_id=chapter.id,
                    chapter_index=chapter.chapter_index,
                    payload={"review_attempt": attempt_record},
                )
        result["review_coverage"] = coverage
        if not coverage.get("valid"):
            raise RuntimeError(
                "审校模型连续两次未完成逐项检查，"
                f"仅覆盖 {coverage.get('covered_count', 0)}/{coverage.get('expected_count', len(expected_dimensions))} 个维度；"
                "本轮不能判定为“没有问题”。"
            )
        suggestions = merge_chapter_suggestions(rule_suggestions, reviewer_suggestions)
        issues = _persist_suggestions(db, novel, chapter, task, suggestions)
        result["cycle_count"] = 1
        result["suggestions"] = suggestions
        result["issue_count"] = len(suggestions)
        emit_task_event(
            db,
            task,
            event_type="chapter_review",
            step_key=f"chapter_{chapter.chapter_index}_review",
            status="completed",
            title=f"第 {chapter.chapter_index} 章审校完成",
            message=(
                f"完成 {coverage['covered_count']}/{coverage['expected_count']} 项检查，"
                f"提出 {len(suggestions)} 条明确修改建议"
            ),
            progress=review_progress,
            chapter_id=chapter.id,
            chapter_index=chapter.chapter_index,
            payload={
                "suggestions": suggestions,
                "issue_count": len(suggestions),
                "review_attempts": result["review_attempts"],
                "review_coverage": coverage,
            },
        )
    except Exception as exc:
        db.rollback()
        result["status"] = "failed"
        result["error"] = str(exc)
        emit_task_event(
            db,
            task,
            event_type="chapter_review",
            step_key=f"chapter_{chapter.chapter_index}_review",
            status="failed",
            title=f"第 {chapter.chapter_index} 章审校失败",
            message=str(exc),
            progress=review_progress,
            chapter_id=chapter.id,
            chapter_index=chapter.chapter_index,
            payload={
                "review_attempts": result.get("review_attempts", []),
                "review_coverage": result.get("review_coverage", {}),
            },
        )
        emit_task_event(
            db,
            task,
            event_type="chapter_revision",
            step_key=f"chapter_{chapter.chapter_index}_revision",
            status="failed",
            title=f"第 {chapter.chapter_index} 章定向修改未执行",
            message="审校阶段失败，没有可执行的修改建议",
            progress=revision_progress,
            chapter_id=chapter.id,
            chapter_index=chapter.chapter_index,
        )
        return result

    if not suggestions:
        result["status"] = "clean"
        chapter.context_snapshot = {
            **(chapter.context_snapshot or {}),
            "chapter_review_cycle": {
                "status": "clean",
                "cycle_count": 1,
                "issue_count": 0,
                "patch_count": 0,
                "reviewer_model": reviewer_config.model,
            },
        }
        db.commit()
        emit_task_event(
            db,
            task,
            event_type="chapter_revision",
            step_key=f"chapter_{chapter.chapter_index}_revision",
            status="completed",
            title=f"第 {chapter.chapter_index} 章无需修改",
            message="审校未发现需要正文模型处理的章内问题",
            progress=revision_progress,
            chapter_id=chapter.id,
            chapter_index=chapter.chapter_index,
            payload={"patch_count": 0},
        )
        return result

    emit_task_event(
        db,
        task,
        event_type="chapter_revision",
        step_key=f"chapter_{chapter.chapter_index}_revision",
        status="running",
        title=f"正在定向修改第 {chapter.chapter_index} 章",
        message=f"正文模型根据 {len(suggestions)} 条建议生成最小段落补丁",
        progress=revision_progress,
        chapter_id=chapter.id,
        chapter_index=chapter.chapter_index,
        payload={"suggestions": suggestions},
    )
    if writer_config is None:
        result["status"] = "failed"
        result["error"] = "未配置可用的正文模型 API"
        result["deferred_count"] = len(issues)
        _mark_unapplied_issues(db, issues, error=result["error"])
        emit_task_event(
            db,
            task,
            event_type="chapter_revision",
            step_key=f"chapter_{chapter.chapter_index}_revision",
            status="failed",
            title=f"第 {chapter.chapter_index} 章定向修改未执行",
            message=result["error"],
            progress=revision_progress,
            chapter_id=chapter.id,
            chapter_index=chapter.chapter_index,
        )
        return result

    patch_records: list[tuple[ChapterRevisionPatch, dict[str, Any]]] = []
    try:
        patch_messages = build_chapter_patch_prompt(chapter, suggestions)
        raw_response, parsed, writer_telemetry = execute_streaming_chapter_review_request(
            writer_config,
            patch_messages,
            max_output_tokens=MAX_CHAPTER_PATCH_OUTPUT_TOKENS,
        )
        normalized_patches, rejected_patches, canonicalized_indexes = (
            normalize_chapter_writer_patches(parsed, chapter, suggestions)
        )
        normalized_patches, coherence_rejected, coherence_audit = (
            validate_chapter_patch_coherence(
                chapter,
                suggestions,
                normalized_patches,
                reviewer_config,
            )
        )
        rejected_patches.extend(coherence_rejected)
        result["writer_attempts"].append(
            {
                "attempt": 1,
                "raw_response": raw_response[:50_000],
                "raw_response_truncated": len(raw_response) > 50_000,
                "accepted_paragraph_indexes": [
                    patch["paragraph_index"] for patch in normalized_patches
                ],
                "rejected_patches": rejected_patches,
                "coherence_validation": coherence_audit,
                "canonicalized_old_text_indexes": canonicalized_indexes,
                "request_telemetry": writer_telemetry,
            }
        )

        accepted_indexes = {patch["paragraph_index"] for patch in normalized_patches}
        retryable_codes = {
            "empty_new_text",
            "no_change",
            "multiple_paragraphs",
            "information_loss",
            "neighbor_coherence",
        }
        retry_indexes = {
            int(item["paragraph_index"])
            for item in rejected_patches
            if item.get("code") in retryable_codes
            and item.get("paragraph_index") is not None
            and int(item["paragraph_index"]) not in accepted_indexes
        }
        final_rejected = [
            item
            for item in rejected_patches
            if item.get("paragraph_index") not in retry_indexes
        ]
        all_canonicalized_indexes = set(canonicalized_indexes)

        if retry_indexes:
            retry_suggestions = [
                suggestion
                for suggestion in suggestions
                if retry_indexes.intersection(suggestion["paragraph_indexes"])
            ]
            result["writer_retry_count"] = 1
            emit_task_event(
                db,
                task,
                event_type="chapter_revision",
                step_key=f"chapter_{chapter.chapter_index}_revision_retry",
                status="running",
                title=f"正在重试第 {chapter.chapter_index} 章失败补丁",
                message=(
                    "只重试第 "
                    + "、".join(str(index) for index in sorted(retry_indexes))
                    + " 段，其余有效补丁保留"
                ),
                progress=revision_progress,
                chapter_id=chapter.id,
                chapter_index=chapter.chapter_index,
                payload={"retry_paragraph_indexes": sorted(retry_indexes)},
            )
            try:
                retry_raw_response, retry_parsed, retry_telemetry = (
                    execute_streaming_chapter_review_request(
                        writer_config,
                        build_chapter_patch_prompt(
                            chapter,
                            retry_suggestions,
                            retry_paragraph_indexes=sorted(retry_indexes),
                            validation_errors=[
                                item
                                for item in rejected_patches
                                if item.get("paragraph_index") in retry_indexes
                            ],
                        ),
                        max_output_tokens=MAX_CHAPTER_PATCH_OUTPUT_TOKENS,
                    )
                )
                retry_patches, retry_rejected, retry_canonicalized = (
                    normalize_chapter_writer_patches(
                        retry_parsed,
                        chapter,
                        retry_suggestions,
                        retry_paragraph_indexes=retry_indexes,
                    )
                )
                retry_patches, retry_coherence_rejected, retry_coherence_audit = (
                    validate_chapter_patch_coherence(
                        chapter,
                        retry_suggestions,
                        retry_patches,
                        reviewer_config,
                    )
                )
                retry_rejected.extend(retry_coherence_rejected)
                retry_patches = [
                    patch
                    for patch in retry_patches
                    if patch["paragraph_index"] not in accepted_indexes
                ]
                normalized_patches.extend(retry_patches)
                accepted_indexes.update(
                    patch["paragraph_index"] for patch in retry_patches
                )
                all_canonicalized_indexes.update(retry_canonicalized)
                unresolved_retry_indexes = retry_indexes - accepted_indexes
                final_rejected.extend(
                    {
                        **item,
                        "retry_status": "not_fixed",
                    }
                    for item in rejected_patches
                    if item.get("paragraph_index") in unresolved_retry_indexes
                )
                final_rejected.extend(
                    item
                    for item in retry_rejected
                    if item.get("paragraph_index") is None
                    or item.get("paragraph_index") in unresolved_retry_indexes
                )
                result["writer_attempts"].append(
                    {
                        "attempt": 2,
                        "raw_response": retry_raw_response[:50_000],
                        "raw_response_truncated": len(retry_raw_response) > 50_000,
                        "accepted_paragraph_indexes": [
                            patch["paragraph_index"] for patch in retry_patches
                        ],
                        "rejected_patches": retry_rejected,
                        "coherence_validation": retry_coherence_audit,
                        "canonicalized_old_text_indexes": retry_canonicalized,
                        "request_telemetry": retry_telemetry,
                    }
                )
            except Exception as retry_exc:
                final_rejected.extend(
                    {
                        **item,
                        "retry_error": str(retry_exc),
                    }
                    for item in rejected_patches
                    if item.get("paragraph_index") in retry_indexes
                )
                result["writer_attempts"].append(
                    {
                        "attempt": 2,
                        "error": str(retry_exc),
                        "accepted_paragraph_indexes": [],
                        "rejected_patches": [],
                        "canonicalized_old_text_indexes": [],
                        **(
                            {"request_telemetry": retry_exc.telemetry}
                            if isinstance(retry_exc, ChapterReviewRequestError)
                            else {}
                        ),
                    }
                )

        if result["writer_retry_count"] and normalized_patches:
            normalized_patches, final_coherence_rejected, final_coherence_audit = (
                validate_chapter_patch_coherence(
                    chapter,
                    suggestions,
                    normalized_patches,
                    reviewer_config,
                )
            )
            final_rejected.extend(final_coherence_rejected)
            result["final_coherence_validation"] = final_coherence_audit

        discarded_indexes = sorted(
            {
                int(item["paragraph_index"])
                for item in final_rejected
                if item.get("code") == "out_of_scope"
                and item.get("paragraph_index") is not None
            }
        )
        result["discarded_patch_indexes"] = discarded_indexes
        result["rejected_patches"] = final_rejected
        result["rejected_patch_count"] = count_rejected_patches(final_rejected)
        result["canonicalized_old_text_indexes"] = sorted(all_canonicalized_indexes)
        if not normalized_patches:
            details = "；".join(
                str(item.get("message") or item.get("code"))
                for item in final_rejected[:3]
            )
            raise ValueError(
                "正文模型没有返回 Reviewer 建议范围内的可应用段落补丁"
                + (f"：{details}" if details else "")
            )
        original_paragraph_count = len(split_chapter_paragraphs(chapter.content))
        content, validation, normalized_patches, application_rejections = (
            apply_chapter_patches_with_isolation(chapter, normalized_patches)
        )
        if application_rejections:
            final_rejected.extend(application_rejections)
            result["rejected_patches"] = final_rejected
            result["application_rejections"] = application_rejections
            result["rejected_patch_count"] = count_rejected_patches(final_rejected)
        if not normalized_patches:
            result["status"] = "deferred"
            result["deferred_count"] = len(issues)
            chapter.context_snapshot = {
                **(chapter.context_snapshot or {}),
                "chapter_review_cycle": {
                    "status": "deferred",
                    "cycle_count": 1,
                    "issue_count": len(suggestions),
                    "patch_count": 0,
                    "rejected_patch_count": result["rejected_patch_count"],
                    "writer_retry_count": result["writer_retry_count"],
                    "reviewer_model": reviewer_config.model,
                    "writer_model": writer_config.model,
                },
            }
            db.commit()
            _mark_unapplied_issues(db, issues)
            emit_task_event(
                db,
                task,
                event_type="chapter_revision",
                step_key=f"chapter_{chapter.chapter_index}_revision",
                status="completed",
                title=f"第 {chapter.chapter_index} 章定向修改已安全跳过",
                message=(
                    "补丁均未通过字数或版本守卫；保留原文，"
                    f"{len(issues)} 条建议待后续处理"
                ),
                progress=revision_progress,
                chapter_id=chapter.id,
                chapter_index=chapter.chapter_index,
                payload={
                    "patch_count": 0,
                    "deferred_count": len(issues),
                    "rejected_patch_count": result["rejected_patch_count"],
                },
            )
            return result
        for patch in normalized_patches:
            related_issue = None
            for issue in issues:
                _, issue_window = _chapter_patch_scope(
                    [
                        {
                            "paragraph_indexes": (
                                (issue.payload or {}).get("paragraph_indexes") or []
                            )
                        }
                    ],
                    original_paragraph_count,
                )
                if patch["paragraph_index"] in issue_window:
                    related_issue = issue
                    break
            record = ChapterRevisionPatch(
                novel_id=novel.id,
                task_id=task.id,
                story_event_id=story_event_id,
                review_issue_id=related_issue.id if related_issue is not None else None,
                chapter_id=chapter.id,
                chapter_index=patch["chapter_index"],
                paragraph_index=patch["paragraph_index"],
                paragraph_id=patch["paragraph_id"],
                base_content_hash=patch["base_content_hash"],
                old_text_hash=patch["old_text_hash"],
                old_text=patch["old_text"],
                new_text=patch["new_text"],
                reason=patch["reason"],
                status="proposed",
                validation={},
            )
            db.add(record)
            patch_records.append((record, patch))
        db.commit()
        for record, _ in patch_records:
            db.refresh(record)

        chapter.content = content
        chapter.word_count = count_chapter_words(content)
        chapter.status = "done"
        chapter.context_snapshot = {
            **(chapter.context_snapshot or {}),
            "chapter_review_cycle": {
                "status": "applied",
                "cycle_count": 1,
                "issue_count": len(suggestions),
                "patch_count": len(normalized_patches),
                "rejected_patch_count": result["rejected_patch_count"],
                "writer_retry_count": result["writer_retry_count"],
                "canonicalized_old_text_indexes": result[
                    "canonicalized_old_text_indexes"
                ],
                "reviewer_model": reviewer_config.model,
                "writer_model": writer_config.model,
                "word_guard": validation["after"],
            },
        }
        applied_paragraphs = {patch["paragraph_index"] for patch in normalized_patches}
        resolved_count = 0
        for record, patch in patch_records:
            record.status = "applied"
            record.validation = {
                **(validation or {}),
                "operation": patch.get("operation", "replace"),
                "scope": patch.get("scope", "target"),
                "information_retention": patch.get("information_retention", {}),
                "coherence_validation": patch.get("coherence_validation", {}),
                "chapter_binding": patch.get("chapter_binding", {}),
            }
            record.applied_at = datetime.now(timezone.utc)
        for issue in issues:
            paragraph_indexes = set((issue.payload or {}).get("paragraph_indexes") or [])
            _, issue_window = _chapter_patch_scope(
                [{"paragraph_indexes": list(paragraph_indexes)}],
                original_paragraph_count,
            )
            affected_indexes = issue_window.intersection(applied_paragraphs)
            if affected_indexes:
                resolved_count += 1
                issue.status = "resolved"
                issue.payload = {
                    **(issue.payload or {}),
                    "auto_repair": {
                        "status": "resolved",
                        "strategy": "writer_local_window_patch",
                        "patch_count": len(affected_indexes),
                        "affected_paragraph_indexes": sorted(affected_indexes),
                    },
                }
            else:
                issue.status = "system_deferred"
                issue.payload = {
                    **(issue.payload or {}),
                    "auto_repair": {
                        "status": "deferred",
                        "reason": "writer_returned_no_patch",
                        "message": "正文模型未为该建议返回段落补丁。",
                    },
                }
        db.commit()
        result["status"] = "applied"
        result["patch_count"] = len(normalized_patches)
        result["patch_ids"] = [str(record.id) for record, _ in patch_records]
        result["resolved_count"] = resolved_count
        result["deferred_count"] = len(issues) - resolved_count
        completion_parts = [f"已应用 {len(normalized_patches)} 个段落补丁，不重写整章"]
        if result["rejected_patch_count"]:
            completion_parts.append(
                f"{result['rejected_patch_count']} 个不安全补丁已跳过"
            )
        if result["deferred_count"]:
            completion_parts.append(f"{result['deferred_count']} 条建议待处理")
        completion_message = "；".join(completion_parts)
        emit_task_event(
            db,
            task,
            event_type="chapter_revision",
            step_key=f"chapter_{chapter.chapter_index}_revision",
            status="completed",
            title=f"第 {chapter.chapter_index} 章定向修改完成",
            message=completion_message,
            progress=revision_progress,
            chapter_id=chapter.id,
            chapter_index=chapter.chapter_index,
            payload={
                "patch_count": len(normalized_patches),
                "patch_ids": result["patch_ids"],
                "discarded_patch_indexes": discarded_indexes,
                "rejected_patch_count": result["rejected_patch_count"],
                "application_rejections": result["application_rejections"],
                "writer_retry_count": result["writer_retry_count"],
                "canonicalized_old_text_indexes": result[
                    "canonicalized_old_text_indexes"
                ],
            },
        )
    except Exception as exc:
        if isinstance(exc, ChapterReviewRequestError):
            result["writer_attempts"].append(
                {
                    "attempt": 1,
                    "error": str(exc),
                    "accepted_paragraph_indexes": [],
                    "rejected_patches": [],
                    "canonicalized_old_text_indexes": [],
                    "request_telemetry": exc.telemetry,
                }
            )
        db.rollback()
        result["status"] = "failed"
        result["error"] = str(exc)
        result["deferred_count"] = len(issues)
        if patch_records:
            record_ids = [record.id for record, _ in patch_records if record.id]
            rejected = db.scalars(
                select(ChapterRevisionPatch).where(ChapterRevisionPatch.id.in_(record_ids))
            ).all()
            for record in rejected:
                record.status = "rejected"
                record.validation = {"error": str(exc)}
        refreshed_issues = db.scalars(
            select(ReviewIssue).where(ReviewIssue.id.in_([issue.id for issue in issues]))
        ).all()
        _mark_unapplied_issues(db, refreshed_issues, error=str(exc))
        emit_task_event(
            db,
            task,
            event_type="chapter_revision",
            step_key=f"chapter_{chapter.chapter_index}_revision",
            status="failed",
            title=f"第 {chapter.chapter_index} 章定向修改失败",
            message=str(exc),
            progress=revision_progress,
            chapter_id=chapter.id,
            chapter_index=chapter.chapter_index,
            payload={
                "request_telemetry": (
                    exc.telemetry
                    if isinstance(exc, ChapterReviewRequestError)
                    else {}
                ),
                "writer_attempts": result.get("writer_attempts", []),
                "rejected_patches": result.get("rejected_patches", []),
                "final_coherence_validation": result.get(
                    "final_coherence_validation",
                    {},
                ),
            },
        )
    return result
