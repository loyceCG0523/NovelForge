"""事件级联合修订服务。

单章生成阶段只检查章内问题；一个剧情事件的章节全部生成后，本服务再统一检查
跨章问题，把互相关联的问题组成有限数量的修订包，最后只做一次事件级复检。
"""

from __future__ import annotations

import json
import hashlib
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.chapter_revision_patch import ChapterRevisionPatch
from app.models.event_chapter_plan import EventChapterPlan
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.models.story_event import StoryEvent
from app.services.chapter_word_guard import build_word_guard_report, count_chapter_words, get_context_chapter_word_range
from app.services.event_quality_checker import sync_event_quality_issues
from app.services.llm_client import (
    LLMClient,
    LLMConfig,
    ReasoningBudgetExhaustedError,
)
from app.services.task_events import emit_task_event


MAX_EVENT_REPAIR_ROUNDS = 1
MAX_CONCURRENT_REPAIR_REQUESTS = 2
REPAIRABLE_SEVERITIES = {"high", "medium"}
STRUCTURAL_REPAIR_PATTERNS = (
    r"合并第?\s*\d+.*第?\s*\d+章",
    r"合并.*章",
    r"跨章(?:移动|搬运|合并)",
    r"(?:移动|搬|挪).*到第?\s*\d+章",
    r"压缩为\s*[一二两三四五六七八九十\d]+\s*章",
    r"删除.*章",
)
MAX_STRUCTURAL_WINDOWS_PER_CHAPTER = 2
MAX_STRUCTURAL_WINDOW_PARAGRAPHS = 12
MIN_STRUCTURAL_WINDOW_RETENTION = 0.65
MAX_STRUCTURAL_WINDOW_GROWTH = 1.20
PATCH_BATCH_SIZE = 3
REVISION_MAX_OUTPUT_TOKENS = 10000
MIN_WINDOW_REWRITE_TARGETS = 8
WINDOW_REWRITE_PATTERNS = (
    r"压缩",
    r"精简",
    r"删(?:除|减)",
    r"流水账",
    r"合并.*段",
)


def build_event_repair_packages(
    issues: list[dict[str, Any]],
    available_chapter_indexes: set[int],
) -> list[dict[str, Any]]:
    """把共享章节的跨章问题合并为完整依赖组件，不再机械切断问题上下文。"""
    normalized: list[dict[str, Any]] = []
    for issue in issues:
        if issue.get("severity") not in REPAIRABLE_SEVERITIES:
            continue
        affected = sorted(
            {
                int(index)
                for index in issue.get("affected_chapter_indexes", [])
                if str(index).isdigit() and int(index) in available_chapter_indexes
            }
        )
        if affected:
            normalized.append({**issue, "affected_chapter_indexes": affected})

    components: list[dict[str, Any]] = []
    for issue in normalized:
        indexes = set(issue["affected_chapter_indexes"])
        touching = [component for component in components if component["chapter_indexes"] & indexes]
        if not touching:
            components.append({"chapter_indexes": indexes, "issues": [issue]})
            continue
        merged_indexes = set(indexes)
        merged_issues = [issue]
        for component in touching:
            merged_indexes.update(component["chapter_indexes"])
            merged_issues.extend(component["issues"])
            components.remove(component)
        components.append({"chapter_indexes": merged_indexes, "issues": merged_issues})

    return [
        {
            "component_id": f"component_{index}",
            "chapter_indexes": sorted(component["chapter_indexes"]),
            "issues": [
                {**issue, "issue_id": issue.get("issue_id") or f"issue_{issue_index}"}
                for issue_index, issue in enumerate(component["issues"], start=1)
            ],
        }
        for index, component in enumerate(components, start=1)
    ]


def _event_outline(chapters: list[Chapter]) -> list[dict[str, Any]]:
    return [
        {
            "chapter_index": chapter.chapter_index,
            "title": chapter.title,
            "summary": chapter.summary,
        }
        for chapter in chapters
    ]


def _chapter_payload(chapter: Chapter, *, include_paragraphs: bool) -> dict[str, Any]:
    word_range = get_context_chapter_word_range(chapter.context_snapshot or {})
    payload: dict[str, Any] = {
        "chapter_index": chapter.chapter_index,
        "title": chapter.title,
        "summary": chapter.summary,
        "word_range": word_range,
        "word_guard": build_word_guard_report(chapter.content, word_range),
    }
    if include_paragraphs:
        payload["numbered_content"] = "\n\n".join(
            f"[P{index}] {paragraph}"
            for index, paragraph in enumerate(
                split_chapter_paragraphs(chapter.content),
                start=1,
            )
        )
    return payload


def build_event_repair_plan_prompt(
    novel: Novel,
    story_event: StoryEvent,
    chapters: list[Chapter],
    issues: list[dict[str, Any]],
    event_chapters: list[Chapter] | None = None,
) -> list[dict[str, str]]:
    """完整阅读依赖组件，先形成跨章共享修订蓝图，不直接生成正文。"""
    payload = {
        "novel": {
            "title": novel.title,
            "genre": novel.genre,
            "premise": novel.premise,
        },
        "story_event": {
            "title": story_event.title,
            "goal": story_event.goal,
            "core_conflict": story_event.core_conflict,
        },
        "target_issues": issues,
        "event_chapter_outline": _event_outline(event_chapters or chapters),
        "affected_chapters": [
            _chapter_payload(chapter, include_paragraphs=True)
            for chapter in chapters
        ],
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 NovelForge 的事件级修订规划 Agent。"
                "完整阅读同一依赖组件中的所有问题章节，制定统一、无冲突的跨章修订蓝图。"
                "此阶段不得生成正文，不得返回 old_text/new_text。"
                "每条问题只能规划一次；必须给出统一后的剧情事实，并逐章列出最小修改目标段落。"
                "必须保持现有章节数量、顺序和章节边界；禁止合并章节、跨章搬运内容或删除整章。"
                "遇到章节功能重复时，为每章分配不同的剧情功能，在本章内部压缩重复流程，"
                "并用行动、冲突、信息或关系变化替换，不能把流程简单移到相邻章节。"
                "必须读取每章 word_guard：章节低于最低字数时，压缩的是流水账信息和重复动作，"
                "不是总字数；必须用有效剧情、对话、冲突或关系推进补足，不能让字数偏差扩大。"
                "不得把人物弱点泛化为常识、职业能力或安全意识缺失。"
                "只输出 JSON，不要输出 Markdown。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请先生成共享修订蓝图。输出格式：\n"
                '{"repair_plan":[{"issue_ids":["issue_1"],'
                '"canonical_facts":["修订后必须统一保持的事实"],'
                '"chapter_actions":[{"chapter_index":1,'
                '"paragraph_indexes":[3,4],"instruction":"本章如何最小修改"}]}]}\n'
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
        },
    ]


def normalize_repair_plan(
    parsed: dict[str, Any],
    chapters_by_index: dict[int, Chapter],
) -> dict[str, Any]:
    raw_plan = parsed.get("repair_plan")
    if not isinstance(raw_plan, list):
        raise ValueError("事件级修订规划没有返回 repair_plan 数组")
    actions_by_chapter: dict[int, list[dict[str, Any]]] = {}
    canonical_facts: list[str] = []
    for raw_item in raw_plan:
        if not isinstance(raw_item, dict):
            continue
        facts = [
            str(item).strip()
            for item in raw_item.get("canonical_facts", [])
            if str(item).strip()
        ]
        canonical_facts.extend(facts)
        issue_ids = [
            str(item).strip()
            for item in raw_item.get("issue_ids", [])
            if str(item).strip()
        ]
        for raw_action in raw_item.get("chapter_actions", []):
            if not isinstance(raw_action, dict):
                continue
            try:
                chapter_index = int(raw_action.get("chapter_index"))
            except (TypeError, ValueError):
                continue
            chapter = chapters_by_index.get(chapter_index)
            if chapter is None:
                continue
            paragraph_count = len(split_chapter_paragraphs(chapter.content))
            paragraph_indexes = sorted(
                {
                    int(index)
                    for index in raw_action.get("paragraph_indexes", [])
                    if str(index).isdigit()
                    and 1 <= int(index) <= paragraph_count
                }
            )
            instruction = str(raw_action.get("instruction") or "").strip()
            if not paragraph_indexes or not instruction:
                continue
            actions_by_chapter.setdefault(chapter_index, []).append(
                {
                    "issue_ids": issue_ids,
                    "canonical_facts": facts,
                    "paragraph_indexes": paragraph_indexes,
                    "instruction": instruction,
                }
            )
    if not actions_by_chapter:
        raise ValueError("事件级修订规划没有产生可执行的章节动作")
    return {
        "canonical_facts": list(dict.fromkeys(canonical_facts)),
        "actions_by_chapter": actions_by_chapter,
    }


def repair_package_requires_structural_replan(package: dict[str, Any]) -> bool:
    """识别单段补丁无法完成的跨章合并、搬运或章节压缩要求。"""
    texts: list[str] = []
    for issue in package.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        texts.extend(
            str(issue.get(key) or "")
            for key in ("message", "suggestion", "evidence")
        )
    repair_plan = package.get("repair_plan") or {}
    for actions in (repair_plan.get("actions_by_chapter") or {}).values():
        for action in actions or []:
            if isinstance(action, dict):
                texts.append(str(action.get("instruction") or ""))
                instruction = str(action.get("instruction") or "")
                paragraph_indexes = {
                    int(index)
                    for index in action.get("paragraph_indexes", [])
                    if str(index).isdigit()
                }
                if (
                    len(paragraph_indexes) >= MIN_WINDOW_REWRITE_TARGETS
                    and any(
                        re.search(pattern, instruction, flags=re.I)
                        for pattern in WINDOW_REWRITE_PATTERNS
                    )
                ):
                    return True
    combined = "\n".join(texts)
    return any(re.search(pattern, combined) for pattern in STRUCTURAL_REPAIR_PATTERNS)


def build_structural_repair_plan_prompt(
    novel: Novel,
    story_event: StoryEvent,
    chapters: list[Chapter],
    issues: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """把不可执行的跨章要求重新规划为章内窗口改写。"""
    payload = {
        "novel": {
            "title": novel.title,
            "genre": novel.genre,
            "premise": novel.premise,
        },
        "story_event": {
            "title": story_event.title,
            "goal": story_event.goal,
            "core_conflict": story_event.core_conflict,
        },
        "target_issues": issues,
        "chapters": [
            _chapter_payload(chapter, include_paragraphs=True)
            for chapter in chapters
        ],
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 NovelForge 结构性局部修订规划 Agent，只输出 JSON。"
                "原方案包含合并章节、跨章搬运或大段删除，当前必须改写成可执行的章内修订。"
                "保持章节数量、顺序、开头两段、结尾两段及既有章节边界不变；"
                "为每章分配唯一剧情功能，使相邻章节不再重复处理同一流程。"
                "每章最多选择两个连续窗口，每个窗口最多12段。"
                "窗口内先压缩重复说明，再用能改变局势的行动、冲突、信息或关系后果补足，"
                "不得靠环境、总结或职业流程填字。不得要求跨章移动、合并或删除章节。"
            ),
        },
        {
            "role": "user",
            "content": (
                "返回："
                '{"chapter_roles":[{"chapter_index":1,"unique_function":"本章唯一功能",'
                '"entry_state":"必须承接的开头状态","exit_state":"必须保持的结尾状态",'
                '"preserve_facts":["不能丢失的事实"]}],'
                '"window_actions":[{"chapter_index":1,"start_paragraph":3,"end_paragraph":8,'
                '"instruction":"如何压缩重复内容并替换成新的剧情推进",'
                '"preserve_facts":["本窗口必须保留的事实"]}]}\n'
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
        },
    ]


def normalize_structural_repair_plan(
    parsed: dict[str, Any],
    chapters_by_index: dict[int, Chapter],
) -> dict[str, Any]:
    """校验结构重排只使用安全的章内连续窗口。"""
    roles: dict[int, dict[str, Any]] = {}
    for raw in parsed.get("chapter_roles") or []:
        if not isinstance(raw, dict) or not str(raw.get("chapter_index") or "").isdigit():
            continue
        chapter_index = int(raw["chapter_index"])
        if chapter_index not in chapters_by_index:
            continue
        unique_function = str(raw.get("unique_function") or "").strip()
        if not unique_function:
            continue
        roles[chapter_index] = {
            "chapter_index": chapter_index,
            "unique_function": unique_function[:300],
            "entry_state": str(raw.get("entry_state") or "").strip()[:300],
            "exit_state": str(raw.get("exit_state") or "").strip()[:300],
            "preserve_facts": [
                str(item).strip()[:300]
                for item in raw.get("preserve_facts") or []
                if str(item).strip()
            ][:12],
        }

    windows_by_chapter: dict[int, list[dict[str, Any]]] = {}
    for raw in parsed.get("window_actions") or []:
        if not isinstance(raw, dict):
            continue
        try:
            chapter_index = int(raw.get("chapter_index"))
            start = int(raw.get("start_paragraph"))
            end = int(raw.get("end_paragraph"))
        except (TypeError, ValueError):
            continue
        chapter = chapters_by_index.get(chapter_index)
        if chapter is None or chapter_index not in roles:
            continue
        paragraph_count = len(split_chapter_paragraphs(chapter.content))
        if not (3 <= start <= end <= paragraph_count - 2):
            continue
        if end - start + 1 > MAX_STRUCTURAL_WINDOW_PARAGRAPHS:
            continue
        instruction = str(raw.get("instruction") or "").strip()
        if not instruction:
            continue
        windows_by_chapter.setdefault(chapter_index, []).append(
            {
                "chapter_index": chapter_index,
                "start_paragraph": start,
                "end_paragraph": end,
                "instruction": instruction[:800],
                "preserve_facts": [
                    str(item).strip()[:300]
                    for item in raw.get("preserve_facts") or []
                    if str(item).strip()
                ][:12],
            }
        )

    for chapter_index, windows in windows_by_chapter.items():
        windows.sort(key=lambda item: item["start_paragraph"])
        if len(windows) > MAX_STRUCTURAL_WINDOWS_PER_CHAPTER:
            raise ValueError(f"第 {chapter_index} 章结构窗口超过上限")
        for previous, current in zip(windows, windows[1:]):
            if current["start_paragraph"] <= previous["end_paragraph"]:
                raise ValueError(f"第 {chapter_index} 章结构窗口互相重叠")
    missing = sorted(set(roles) - set(windows_by_chapter))
    if not roles or missing:
        raise ValueError(
            "结构重排没有为每个章节提供安全窗口"
            + (f"：{missing}" if missing else "")
        )
    return {
        "chapter_roles": roles,
        "windows_by_chapter": windows_by_chapter,
        "strategy": "local_window_rewrite",
    }


def build_structural_window_patch_prompt(
    novel: Novel,
    story_event: StoryEvent,
    chapters: list[Chapter],
    plan: dict[str, Any],
) -> list[dict[str, str]]:
    """一次生成同一结构组件内的所有窗口补丁。"""
    windows = []
    chapters_by_index = {chapter.chapter_index: chapter for chapter in chapters}
    for chapter_index, actions in plan["windows_by_chapter"].items():
        paragraphs = split_chapter_paragraphs(chapters_by_index[chapter_index].content)
        for action in actions:
            start = action["start_paragraph"]
            end = action["end_paragraph"]
            windows.append(
                {
                    **action,
                    "old_paragraphs": paragraphs[start - 1 : end],
                    "previous_two": paragraphs[max(0, start - 3) : start - 1],
                    "next_two": paragraphs[end : end + 2],
                    "chapter_word_guard": build_word_guard_report(
                        chapters_by_index[chapter_index].content,
                        get_context_chapter_word_range(
                            chapters_by_index[chapter_index].context_snapshot or {}
                        ),
                    ),
                }
            )
    payload = {
        "novel": {"title": novel.title, "genre": novel.genre},
        "story_event": {
            "title": story_event.title,
            "goal": story_event.goal,
            "core_conflict": story_event.core_conflict,
        },
        "chapter_roles": list(plan["chapter_roles"].values()),
        "windows": windows,
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 NovelForge 结构窗口修订 Agent，只输出 JSON。"
                "严格按 chapter_roles 改写每个窗口，让相邻章节承担不同功能。"
                "只返回窗口编号和 new_paragraphs，原文由服务端绑定，不要复述 old_paragraphs。"
                "保留人物、时间、地点、道具、关键对白信息及窗口前后因果。"
                "压缩重复流程后必须用行动、冲突、信息变化或关系后果承接，不能用环境和总结填充。"
                "必须遵守 chapter_word_guard；章节过短时，压缩的是无效流程而非总字数，"
                "新窗口总字数不得低于旧窗口，并应用有效剧情或人物互动提高叙事密度。"
                "不得修改窗口外内容，不得跨章搬运，不得改变章节开头和结尾。"
            ),
        },
        {
            "role": "user",
            "content": (
                "返回："
                '{"window_patches":[{"chapter_index":1,"start_paragraph":3,"end_paragraph":8,'
                '"new_paragraphs":["改写后的完整段落"],'
                '"reason":"如何消除功能重复并保持衔接"}],'
                '"chapter_summaries":[{"chapter_index":1,"summary":"修订后的准确摘要"}]}\n'
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
        },
    ]


def build_chapter_patch_prompt(
    novel: Novel,
    story_event: StoryEvent,
    chapter: Chapter,
    component_chapters: list[Chapter],
    repair_plan: dict[str, Any],
) -> list[dict[str, str]]:
    """正文模型读取完整目标章和共享蓝图，只生成本章的最小段落补丁。"""
    actions = repair_plan["actions_by_chapter"].get(chapter.chapter_index, [])
    payload = {
        "novel": {
            "title": novel.title,
            "genre": novel.genre,
            "premise": novel.premise,
        },
        "story_event": {
            "title": story_event.title,
            "goal": story_event.goal,
            "core_conflict": story_event.core_conflict,
        },
        "component_outline": _event_outline(component_chapters),
        "canonical_facts": repair_plan.get("canonical_facts", []),
        "chapter_actions": actions,
        "chapter": _chapter_payload(chapter, include_paragraphs=True),
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 NovelForge 正文定向修改 Agent。"
                "事件级规划已经完整分析跨章问题，你只执行当前章节的 chapter_actions。"
                "必须遵守 canonical_facts，不得重写整章，不得修改未点名段落。"
                "每个补丁只返回目标 paragraph_index 和替换后的完整单段 new_text；"
                "原文由服务端按段落编号绑定，不要复述 old_text。"
                "不得合并、拆分或新增段落；修改后必须保持章节 word_range。"
                "若 word_guard 显示章节过短，只能压缩重复信息和无效动作，"
                "同时用有效剧情、对话、冲突或关系变化补足，不得继续缩短总字数。"
                "不得用天气、灯光、家具、服装、食物、品牌或重复生活动作填充补丁；"
                "环境与具体细节必须服务人物行动、冲突、线索、空间理解或必要氛围，否则应压缩。"
                "只输出 JSON，不要输出 Markdown。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请执行当前章节动作，只返回最小必要段落补丁。输出格式：\n"
                '{"patches":[{"chapter_index":1,"paragraph_index":3,'
                '"new_text":"替换后的完整段落",'
                '"reason":"对应修订蓝图中的动作"}]}\n'
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
        },
    ]


def estimate_revision_request(messages: list[dict[str, str]]) -> dict[str, int]:
    prompt_chars = sum(len(str(message.get("content") or "")) for message in messages)
    # 中文与 JSON 的 tokenizer 差异较大，按 1 字符≈1 token 做保守预算。
    estimated_input_tokens = max(1, prompt_chars)
    return {
        "prompt_chars": prompt_chars,
        "estimated_input_tokens": estimated_input_tokens,
    }


def build_dynamic_revision_config(
    base_config: LLMConfig,
    messages: list[dict[str, str]],
    *,
    max_output_tokens: int,
) -> tuple[LLMConfig, dict[str, int]]:
    metrics = estimate_revision_request(messages)
    base_first_token_timeout = max(
        90,
        min(
            300,
            int(60 + metrics["estimated_input_tokens"] / 200),
        ),
    )
    base_total_timeout = max(
        180,
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
        if metrics["estimated_input_tokens"] > 30000
        else min(base_config.max_retries, 1)
    )
    metrics.update(
        {
            "activity_timeout_seconds": activity_timeout,
            "first_token_timeout_seconds": activity_timeout,
            "total_timeout_seconds": total_timeout,
            "max_output_tokens": max_output_tokens,
            "max_retries": max_retries,
        }
    )
    return (
        replace(
            base_config,
            timeout_seconds=float(activity_timeout),
            total_timeout_seconds=float(total_timeout),
            max_retries=max_retries,
        ),
        metrics,
    )


class RevisionRequestError(RuntimeError):
    def __init__(self, message: str, telemetry: dict[str, Any]) -> None:
        super().__init__(message)
        self.telemetry = telemetry


def execute_streaming_revision_request(
    base_config: LLMConfig,
    messages: list[dict[str, str]],
    *,
    max_output_tokens: int,
    on_activity: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    config, telemetry = build_dynamic_revision_config(
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

    client = LLMClient(config)
    try:
        _, parsed = client.complete_json(
            messages,
            max_tokens=max_output_tokens,
            stream=True,
            on_raw_delta=on_raw_delta,
            on_activity=on_activity,
        )
    except Exception as exc:
        cause = exc
        cause_chain: list[str] = []
        while cause is not None:
            cause_chain.append(type(cause).__name__)
            cause = cause.__cause__
        telemetry.update(
            {
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
                "first_delta_seconds": (
                    round(first_delta_at - started_at, 3)
                    if first_delta_at is not None
                    else None
                ),
                "output_chars": output_chars,
                "exception_chain": cause_chain,
                "failure_kind": (
                    "reasoning_budget_exhausted"
                    if isinstance(exc, ReasoningBudgetExhaustedError)
                    else "request_failed"
                ),
                "transport": dict(client.last_request_telemetry),
            }
        )
        raise RevisionRequestError(str(exc), telemetry) from exc
    telemetry.update(
        {
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
            "first_delta_seconds": (
                round(first_delta_at - started_at, 3)
                if first_delta_at is not None
                else None
            ),
            "output_chars": output_chars,
            "streaming": True,
            "transport": dict(client.last_request_telemetry),
        }
    )
    return parsed, telemetry


def execute_patch_request_with_budget_retry(
    base_config: LLMConfig,
    messages: list[dict[str, str]],
    *,
    initial_max_output_tokens: int = REVISION_MAX_OUTPUT_TOKENS,
    expanded_max_output_tokens: int = REVISION_MAX_OUTPUT_TOKENS,
    on_activity: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """补丁仅在思考耗尽时扩容重试当前请求，其他错误不做昂贵重跑。"""
    attempts: list[dict[str, Any]] = []

    def activity_callback(max_output_tokens: int) -> Callable[[dict[str, Any]], None] | None:
        if on_activity is None:
            return None

        def forward(activity: dict[str, Any]) -> None:
            on_activity({**activity, "max_output_tokens": max_output_tokens})

        return forward

    try:
        parsed, telemetry = execute_streaming_revision_request(
            base_config,
            messages,
            max_output_tokens=initial_max_output_tokens,
            on_activity=activity_callback(initial_max_output_tokens),
        )
        attempts.append(telemetry)
        return parsed, {
            **telemetry,
            "budget_retry": False,
            "budget_attempts": attempts,
        }
    except RevisionRequestError as exc:
        attempts.append(dict(exc.telemetry))
        if (
            exc.telemetry.get("failure_kind") != "reasoning_budget_exhausted"
            or expanded_max_output_tokens <= initial_max_output_tokens
        ):
            raise

    try:
        parsed, telemetry = execute_streaming_revision_request(
            base_config,
            messages,
            max_output_tokens=expanded_max_output_tokens,
            on_activity=activity_callback(expanded_max_output_tokens),
        )
    except RevisionRequestError as exc:
        attempts.append(dict(exc.telemetry))
        combined = {
            **exc.telemetry,
            "budget_retry": True,
            "initial_max_output_tokens": initial_max_output_tokens,
            "expanded_max_output_tokens": expanded_max_output_tokens,
            "budget_attempts": attempts,
        }
        raise RevisionRequestError(
            f"补丁扩容到 {expanded_max_output_tokens} tokens 后仍失败：{exc}",
            combined,
        ) from exc
    attempts.append(telemetry)
    return parsed, {
        **telemetry,
        "budget_retry": True,
        "initial_max_output_tokens": initial_max_output_tokens,
        "expanded_max_output_tokens": expanded_max_output_tokens,
        "budget_attempts": attempts,
    }


def text_hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def split_chapter_paragraphs(content: str | None) -> list[str]:
    """按存储格式读取稳定段落；补丁应用前不重新排版，避免段落编号漂移。"""
    return [item.strip() for item in re.split(r"\r?\n\s*\r?\n+", content or "") if item.strip()]


def build_patch_action_batches(
    actions: list[dict[str, Any]],
    *,
    batch_size: int = PATCH_BATCH_SIZE,
) -> list[list[dict[str, Any]]]:
    """把补丁目标拆成小批次，避免思考和正文共同挤占输出额度。"""
    batches: list[list[dict[str, Any]]] = []
    current_batch: list[dict[str, Any]] = []
    current_count = 0
    for action in actions:
        paragraph_indexes = [
            int(index)
            for index in action.get("paragraph_indexes", [])
            if str(index).isdigit()
        ]
        for offset in range(0, len(paragraph_indexes), batch_size):
            index_chunk = paragraph_indexes[offset:offset + batch_size]
            if current_batch and current_count + len(index_chunk) > batch_size:
                batches.append(current_batch)
                current_batch = []
                current_count = 0
            current_batch.append({**action, "paragraph_indexes": index_chunk})
            current_count += len(index_chunk)
            if current_count >= batch_size:
                batches.append(current_batch)
                current_batch = []
                current_count = 0
    if current_batch:
        batches.append(current_batch)
    return batches


def build_contiguous_window_repair_plan(
    chapter: Chapter,
    actions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """把连续大范围的压缩任务改为至多两个原子窗口，避免零散删改。"""
    combined_instruction = "\n".join(
        str(action.get("instruction") or "")
        for action in actions
    )
    if not any(
        re.search(pattern, combined_instruction, flags=re.I)
        for pattern in WINDOW_REWRITE_PATTERNS
    ):
        return None

    paragraph_count = len(split_chapter_paragraphs(chapter.content))
    indexes = sorted(
        {
            int(index)
            for action in actions
            for index in action.get("paragraph_indexes", [])
            if str(index).isdigit() and 3 <= int(index) <= paragraph_count - 2
        }
    )
    if len(indexes) < MIN_WINDOW_REWRITE_TARGETS:
        return None

    runs: list[list[int]] = []
    for index in indexes:
        if not runs or index != runs[-1][-1] + 1:
            runs.append([index])
        else:
            runs[-1].append(index)
    if max((len(run) for run in runs), default=0) < MIN_WINDOW_REWRITE_TARGETS:
        return None

    windows: list[dict[str, Any]] = []
    for run in runs:
        if len(run) < MIN_WINDOW_REWRITE_TARGETS:
            continue
        for offset in range(0, len(run), MAX_STRUCTURAL_WINDOW_PARAGRAPHS):
            chunk = run[offset:offset + MAX_STRUCTURAL_WINDOW_PARAGRAPHS]
            overlapping = [
                action
                for action in actions
                if set(chunk)
                & {
                    int(index)
                    for index in action.get("paragraph_indexes", [])
                    if str(index).isdigit()
                }
            ]
            windows.append(
                {
                    "chapter_index": chapter.chapter_index,
                    "start_paragraph": chunk[0],
                    "end_paragraph": chunk[-1],
                    "instruction": "；".join(
                        dict.fromkeys(
                            str(action.get("instruction") or "").strip()
                            for action in overlapping
                            if str(action.get("instruction") or "").strip()
                        )
                    )[:1200],
                    "preserve_facts": list(
                        dict.fromkeys(
                            str(fact).strip()
                            for action in overlapping
                            for fact in action.get("canonical_facts", [])
                            if str(fact).strip()
                        )
                    )[:12],
                }
            )
    if not windows or len(windows) > MAX_STRUCTURAL_WINDOWS_PER_CHAPTER:
        return None

    word_range = get_context_chapter_word_range(chapter.context_snapshot or {})
    return {
        "chapter_roles": {
            chapter.chapter_index: {
                "chapter_index": chapter.chapter_index,
                "unique_function": combined_instruction[:300],
                "entry_state": "保持窗口前两段形成的进入状态",
                "exit_state": "保持窗口后两段承接的退出状态",
                "preserve_facts": list(
                    dict.fromkeys(
                        str(fact).strip()
                        for action in actions
                        for fact in action.get("canonical_facts", [])
                        if str(fact).strip()
                    )
                )[:12],
                "word_guard": build_word_guard_report(chapter.content, word_range),
            }
        },
        "windows_by_chapter": {chapter.chapter_index: windows},
        "strategy": "local_window_rewrite",
    }


def normalize_paragraph_patches(
    parsed: dict[str, Any],
    chapters_by_index: dict[int, Chapter],
    allowed_targets: set[tuple[int, int]] | None = None,
    expected_base_content_hashes: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """校验模型补丁只指向存在的唯一段落，并拒绝整章或空文本替换。"""
    raw_patches = parsed.get("patches")
    if not isinstance(raw_patches, list):
        raise ValueError("事件级局部修订没有返回 patches 数组")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for item in raw_patches:
        if not isinstance(item, dict):
            continue
        if not str(item.get("chapter_index", "")).isdigit() or not str(item.get("paragraph_index", "")).isdigit():
            continue
        chapter_index = int(item["chapter_index"])
        paragraph_index = int(item["paragraph_index"])
        chapter = chapters_by_index.get(chapter_index)
        paragraphs = split_chapter_paragraphs(chapter.content if chapter else "")
        if chapter is None or paragraph_index < 1 or paragraph_index > len(paragraphs):
            continue
        if (
            expected_base_content_hashes is not None
            and expected_base_content_hashes.get(chapter_index)
            != text_hash(chapter.content or "")
        ):
            raise ValueError(f"第 {chapter_index} 章已被其他操作修改，拒绝应用旧补丁")
        key = (chapter_index, paragraph_index)
        if allowed_targets is not None and key not in allowed_targets:
            continue
        if key in seen:
            raise ValueError(f"第 {chapter_index} 章第 {paragraph_index} 段存在重复补丁")
        new_text = str(item.get("new_text") or "").strip()
        expected = paragraphs[paragraph_index - 1]
        old_text = expected
        if not new_text or new_text == old_text:
            continue
        if "\n\n" in new_text:
            raise ValueError(f"第 {chapter_index} 章第 {paragraph_index} 段补丁不得包含多个段落")
        normalized.append(
            {
                "chapter": chapter,
                "chapter_index": chapter_index,
                "paragraph_index": paragraph_index,
                "paragraph_id": f"ch{chapter_index}-p{paragraph_index}",
                "base_content_hash": text_hash(chapter.content or ""),
                "old_text_hash": text_hash(old_text),
                "old_text": old_text,
                "new_text": new_text,
                "reason": str(item.get("reason") or "事件级局部修订").strip(),
            }
        )
        seen.add(key)
    if not normalized:
        raise ValueError("事件级局部修订没有产生可应用补丁")
    return normalized


def apply_paragraph_patches(chapter: Chapter, patches: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """在同一内容版本上应用补丁，并保证字数距离不恶化。"""
    if any(item["base_content_hash"] != text_hash(chapter.content or "") for item in patches):
        raise ValueError(f"第 {chapter.chapter_index} 章已被其他操作修改，拒绝应用旧补丁")
    paragraphs: list[str | None] = split_chapter_paragraphs(chapter.content)
    for patch in sorted(patches, key=lambda item: item["paragraph_index"]):
        offset = patch["paragraph_index"] - 1
        current_paragraph = paragraphs[offset]
        if current_paragraph is None or text_hash(current_paragraph) != patch["old_text_hash"]:
            raise ValueError(f"第 {chapter.chapter_index} 章第 {patch['paragraph_index']} 段版本已变化")
        if patch.get("operation") == "delete":
            paragraphs[offset] = None
        else:
            paragraphs[offset] = patch["new_text"]
    # 删除操作先保留原始索引完成整批校验，最后再移除冗余段；其余段落逐字不变。
    content = "\n\n".join(paragraph for paragraph in paragraphs if paragraph is not None)
    word_range = get_context_chapter_word_range(chapter.context_snapshot or {})
    before = build_word_guard_report(chapter.content, word_range)
    after = build_word_guard_report(content, word_range)
    if before["within_range"] and not after["within_range"]:
        raise ValueError(f"第 {chapter.chapter_index} 章局部修订后超出字数范围")
    if not before["within_range"] and int(after["delta"]) > int(before["delta"]):
        raise ValueError(f"第 {chapter.chapter_index} 章局部修订使字数偏差扩大")
    return content, {"before": before, "after": after}


def isolate_safe_event_patches(
    chapter: Chapter,
    patches: list[dict[str, Any]],
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """整批字数校验失败时逐条隔离，保留安全补丁。"""
    try:
        content, validation = apply_paragraph_patches(chapter, patches)
        return content, validation, patches, []
    except ValueError as exc:
        if "拒绝应用旧补丁" in str(exc) or "版本已变化" in str(exc):
            raise

    current_content = chapter.content or ""
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for patch in sorted(
        patches,
        key=lambda item: (
            len(item["new_text"]) - len(item["old_text"]),
            item["paragraph_index"],
        ),
    ):
        paragraphs = split_chapter_paragraphs(current_content)
        current_old_text = paragraphs[patch["paragraph_index"] - 1]
        isolated_patch = {
            **patch,
            "base_content_hash": text_hash(current_content),
            "old_text": current_old_text,
            "old_text_hash": text_hash(current_old_text),
        }
        shadow = SimpleNamespace(
            chapter_index=chapter.chapter_index,
            content=current_content,
            context_snapshot=chapter.context_snapshot,
        )
        try:
            current_content, _ = apply_paragraph_patches(shadow, [isolated_patch])
        except ValueError as exc:
            rejected.append(
                {
                    "chapter_index": chapter.chapter_index,
                    "paragraph_index": patch["paragraph_index"],
                    "error": str(exc),
                }
            )
            continue
        accepted.append(patch)
    if not accepted:
        return chapter.content or "", None, [], rejected
    content, validation = apply_paragraph_patches(
        chapter,
        sorted(accepted, key=lambda item: item["paragraph_index"]),
    )
    return content, validation, accepted, rejected


def normalize_structural_window_patches(
    parsed: dict[str, Any],
    chapters_by_index: dict[int, Chapter],
    plan: dict[str, Any],
    expected_base_content_hashes: dict[int, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """校验窗口范围、原文版本和改写规模，拒绝遗漏或越界补丁。"""
    allowed = {
        (
            chapter_index,
            action["start_paragraph"],
            action["end_paragraph"],
        ): action
        for chapter_index, actions in plan["windows_by_chapter"].items()
        for action in actions
    }
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int]] = set()
    for raw in parsed.get("window_patches") or []:
        if not isinstance(raw, dict):
            continue
        try:
            key = (
                int(raw.get("chapter_index")),
                int(raw.get("start_paragraph")),
                int(raw.get("end_paragraph")),
            )
        except (TypeError, ValueError):
            continue
        if key not in allowed or key in seen:
            continue
        chapter_index, start, end = key
        chapter = chapters_by_index[chapter_index]
        if (
            expected_base_content_hashes is not None
            and expected_base_content_hashes.get(chapter_index)
            != text_hash(chapter.content or "")
        ):
            raise ValueError(f"第 {chapter_index} 章已被其他操作修改，拒绝应用结构补丁")
        paragraphs = split_chapter_paragraphs(chapter.content)
        expected = paragraphs[start - 1 : end]
        new_paragraphs = [
            str(item).strip()
            for item in raw.get("new_paragraphs") or []
            if str(item).strip()
        ]
        if not new_paragraphs or len(new_paragraphs) > MAX_STRUCTURAL_WINDOW_PARAGRAPHS:
            raise ValueError(
                f"第 {chapter_index} 章 P{start}-P{end} 窗口新段落数量无效"
            )
        if any("\n\n" in item for item in new_paragraphs):
            raise ValueError("结构窗口中的单段不得包含额外空行")
        old_chars = sum(len(item) for item in expected)
        new_chars = sum(len(item) for item in new_paragraphs)
        if new_chars < max(40, int(old_chars * MIN_STRUCTURAL_WINDOW_RETENTION)):
            raise ValueError(
                f"第 {chapter_index} 章 P{start}-P{end} 结构改写删减过多"
            )
        if new_chars > int(old_chars * MAX_STRUCTURAL_WINDOW_GROWTH) + 80:
            raise ValueError(
                f"第 {chapter_index} 章 P{start}-P{end} 结构改写扩写过多"
            )
        normalized.append(
            {
                "chapter_index": chapter_index,
                "start_paragraph": start,
                "end_paragraph": end,
                "base_content_hash": text_hash(chapter.content or ""),
                "old_paragraphs": expected,
                "new_paragraphs": new_paragraphs,
                "reason": str(raw.get("reason") or "结构性局部压缩").strip()[:500],
            }
        )
        seen.add(key)
    missing = sorted(set(allowed) - seen)
    if missing:
        raise ValueError(f"结构窗口补丁未完整返回：{missing}")

    summaries: dict[int, str] = {}
    for raw in parsed.get("chapter_summaries") or []:
        if not isinstance(raw, dict) or not str(raw.get("chapter_index") or "").isdigit():
            continue
        chapter_index = int(raw["chapter_index"])
        summary = str(raw.get("summary") or "").strip()
        if chapter_index in chapters_by_index and summary:
            summaries[chapter_index] = summary[:500]
    return normalized, summaries


def apply_structural_window_patches(
    chapters_by_index: dict[int, Chapter],
    patches: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """在内存中应用全部窗口并统一校验，通过后调用方再原子写库。"""
    patches_by_chapter: dict[int, list[dict[str, Any]]] = {}
    for patch in patches:
        patches_by_chapter.setdefault(patch["chapter_index"], []).append(patch)
    results: dict[int, dict[str, Any]] = {}
    for chapter_index, chapter_patches in patches_by_chapter.items():
        chapter = chapters_by_index[chapter_index]
        if any(
            patch["base_content_hash"] != text_hash(chapter.content or "")
            for patch in chapter_patches
        ):
            raise ValueError(f"第 {chapter_index} 章已被其他操作修改，拒绝应用结构补丁")
        paragraphs = split_chapter_paragraphs(chapter.content)
        for patch in sorted(
            chapter_patches,
            key=lambda item: item["start_paragraph"],
            reverse=True,
        ):
            start = patch["start_paragraph"]
            end = patch["end_paragraph"]
            if paragraphs[start - 1 : end] != patch["old_paragraphs"]:
                raise ValueError(
                    f"第 {chapter_index} 章 P{start}-P{end} 窗口版本已变化"
                )
            paragraphs[start - 1 : end] = patch["new_paragraphs"]
        content = "\n\n".join(paragraphs)
        word_range = get_context_chapter_word_range(chapter.context_snapshot or {})
        before = build_word_guard_report(chapter.content, word_range)
        after = build_word_guard_report(content, word_range)
        if before["within_range"] and not after["within_range"]:
            raise ValueError(f"第 {chapter_index} 章结构修订后超出字数范围")
        if not before["within_range"] and int(after["delta"]) > int(before["delta"]):
            raise ValueError(f"第 {chapter_index} 章结构修订使字数偏差扩大")
        results[chapter_index] = {
            "content": content,
            "validation": {
                "strategy": "local_window_rewrite",
                "before": before,
                "after": after,
                "windows": [
                    {
                        "start_paragraph": patch["start_paragraph"],
                        "end_paragraph": patch["end_paragraph"],
                    }
                    for patch in sorted(
                        chapter_patches,
                        key=lambda item: item["start_paragraph"],
                    )
                ],
            },
        }
    return results


def review_and_repair_story_event(
    db: Session,
    novel: Novel,
    story_event: StoryEvent,
    llm_config: LLMConfig | None,
    task: GenerationTask | None = None,
    revision_llm_config: LLMConfig | None = None,
) -> dict[str, Any]:
    """Reviewer 执行总审，Writer 至多执行一次联合局部修订，再由 Reviewer 复检。"""
    if task is not None:
        emit_task_event(
            db,
            task,
            event_type="review",
            step_key="event_review_initial",
            status="running",
            title="正在执行事件总审",
            message="检查跨章节奏、时间线、资源状态、人物动机和重复剧情",
            progress=78,
        )
    initial_review = sync_event_quality_issues(
        db=db,
        novel=novel,
        story_event=story_event,
        llm_config=llm_config,
        task=task,
    )
    if task is not None:
        initial_issues = initial_review.get("quality_report", {}).get("issues", [])
        emit_task_event(
            db,
            task,
            event_type="review",
            step_key="event_review_initial",
            status="completed",
            title="事件总审完成",
            message=(
                f"发现 {len(initial_issues)} 个问题，正在定位需要修改的段落"
                if initial_issues
                else "未发现需要修订的事件级问题"
            ),
            progress=80,
            payload={
                "issue_count": len(initial_issues),
                "issues": initial_issues,
                **(
                    initial_review.get("quality_report", {}).get(
                        "request_telemetry",
                        {},
                    )
                ),
            },
        )
    result: dict[str, Any] = {
        "story_event_id": str(story_event.id),
        "strategy": "paragraph_patch",
        "max_repair_rounds": MAX_EVENT_REPAIR_ROUNDS,
        "initial_review": initial_review,
        "repair_packages": [],
        "repair_rounds": 0,
    }
    if llm_config is None:
        if task is not None:
            emit_task_event(
                db,
                task,
                event_type="review",
                step_key="event_review_final",
                status="completed",
                title="事件复检已结束",
                message="当前未配置事件审校模型，未执行局部修订后的二次复检",
                progress=94,
                payload={"skipped": True, "reason": "review_model_unavailable"},
            )
        result["final_review"] = initial_review
        result["remaining_open_risks"] = initial_review.get("remaining_open_risks", 0)
        return result

    plans = db.scalars(
        select(EventChapterPlan)
        .where(EventChapterPlan.story_event_id == story_event.id, EventChapterPlan.chapter_id.is_not(None))
        .order_by(EventChapterPlan.chapter_index.asc())
    ).all()
    chapters = db.scalars(
        select(Chapter)
        .where(Chapter.id.in_([plan.chapter_id for plan in plans]))
        .order_by(Chapter.chapter_index.asc())
    ).all()
    chapters_by_index = {chapter.chapter_index: chapter for chapter in chapters}
    packages = build_event_repair_packages(
        initial_review.get("quality_report", {}).get("issues", []),
        set(chapters_by_index),
    )
    if not packages and task is not None:
        initial_issues = initial_review.get("quality_report", {}).get("issues", [])
        emit_task_event(
            db,
            task,
            event_type="review",
            step_key="event_review_final",
            status="completed",
            title="无需二次复检",
            message=(
                "事件总审未发现问题，已跳过局部修订和二次复检"
                if not initial_issues
                else "没有可执行的高、中风险修订包，已结束本轮事件复检"
            ),
            progress=94,
            payload={
                "skipped": True,
                "reason": "no_repair_packages",
                "issue_count": len(initial_issues),
            },
        )
    if packages and revision_llm_config is None:
        if task is not None:
            emit_task_event(
                db,
                task,
                event_type="review",
                step_key="event_review_final",
                status="completed",
                title="二次复检未执行",
                message="缺少可用的正文模型 API，局部修订未执行，因此没有修订结果可供复检",
                progress=94,
                payload={
                    "skipped": True,
                    "reason": "revision_model_unavailable",
                    "repair_package_count": len(packages),
                },
            )
        result["repair_error"] = "未配置可用的正文模型 API，Reviewer 建议未交给正文模型修改"
        result["final_review"] = initial_review
        result["remaining_open_risks"] = initial_review.get("remaining_open_risks", len(packages))
        return result

    if packages:
        result["repair_rounds"] = 1
    component_states: list[dict[str, Any]] = []
    for package_number, package in enumerate(packages, start=1):
        target_chapters = [
            chapters_by_index[index]
            for index in package["chapter_indexes"]
        ]
        plan_messages = build_event_repair_plan_prompt(
            novel,
            story_event,
            target_chapters,
            package["issues"],
            event_chapters=chapters,
        )
        max_plan_tokens = REVISION_MAX_OUTPUT_TOKENS
        _, plan_budget = build_dynamic_revision_config(
            llm_config,
            plan_messages,
            max_output_tokens=max_plan_tokens,
        )
        state = {
            "package_number": package_number,
            "package": package,
            "target_chapters": target_chapters,
            "plan_messages": plan_messages,
            "max_plan_tokens": max_plan_tokens,
            "plan": None,
            "plan_telemetry": plan_budget,
            "chapter_results": {},
            "chapter_errors": {},
        }
        component_states.append(state)
        if task is not None:
            emit_task_event(
                db,
                task,
                event_type="revision_plan",
                step_key=f"revision_package_{package_number}",
                status="running",
                title=f"正在规划第 {package_number}/{len(packages)} 个事件修订组件",
                message=(
                    f"完整分析第 {', '.join(map(str, package['chapter_indexes']))} 章；"
                    f"输入约 {plan_budget['prompt_chars']} 字符，"
                    f"无流活动超时 {plan_budget['activity_timeout_seconds']} 秒"
                ),
                progress=80 + min(6, package_number),
                payload={
                    "package_number": package_number,
                    "package_count": len(packages),
                    "parallel_limit": MAX_CONCURRENT_REPAIR_REQUESTS,
                    **package,
                    **plan_budget,
                },
            )

    if component_states:
        with ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_REPAIR_REQUESTS,
            thread_name_prefix="event-revision-plan",
        ) as executor:
            plan_futures = {
                executor.submit(
                    execute_streaming_revision_request,
                    llm_config,
                    state["plan_messages"],
                    max_output_tokens=state["max_plan_tokens"],
                ): state
                for state in component_states
            }
            for future in as_completed(plan_futures):
                state = plan_futures[future]
                package_number = state["package_number"]
                try:
                    parsed, telemetry = future.result()
                    state["plan"] = normalize_repair_plan(
                        parsed,
                        {
                            chapter.chapter_index: chapter
                            for chapter in state["target_chapters"]
                        },
                    )
                    state["plan_telemetry"] = telemetry
                    if task is not None:
                        emit_task_event(
                            db,
                            task,
                            event_type="revision_plan",
                            step_key=f"revision_package_{package_number}_plan",
                            status="completed",
                            title=f"第 {package_number}/{len(packages)} 个共享修订蓝图已生成",
                            message=(
                                f"规划了 {len(state['plan']['actions_by_chapter'])} 个章节动作；"
                                f"流式接收 {telemetry['output_chars']} 字符"
                            ),
                            progress=82 + min(5, package_number),
                            payload={
                                **telemetry,
                                "chapter_indexes": state["package"]["chapter_indexes"],
                            },
                        )
                except Exception as exc:
                    telemetry = getattr(exc, "telemetry", state["plan_telemetry"])
                    state["plan_error"] = str(exc)
                    state["plan_telemetry"] = telemetry
                    if task is not None:
                        emit_task_event(
                            db,
                            task,
                            event_type="revision_plan",
                            step_key=f"revision_package_{package_number}",
                            status="failed",
                            title=f"第 {package_number}/{len(packages)} 个事件修订规划失败",
                            message=str(exc),
                            progress=83 + min(5, package_number),
                            payload={
                                **telemetry,
                                "chapter_indexes": state["package"]["chapter_indexes"],
                            },
                        )

    patch_jobs: list[dict[str, Any]] = []
    for state in component_states:
        if state.get("plan") is None:
            continue
        for chapter_index, actions in state["plan"]["actions_by_chapter"].items():
            chapter = chapters_by_index[chapter_index]
            target_paragraph_count = len(
                {
                    index
                    for action in actions
                    for index in action["paragraph_indexes"]
                }
            )
            base_content_hash = text_hash(chapter.content or "")
            window_plan = build_contiguous_window_repair_plan(chapter, actions)
            if window_plan is not None:
                job_specs = [
                    {
                        "mode": "window",
                        "messages": build_structural_window_patch_prompt(
                            novel,
                            story_event,
                            [chapter],
                            window_plan,
                        ),
                        "initial_max_output_tokens": REVISION_MAX_OUTPUT_TOKENS,
                        "expanded_max_output_tokens": REVISION_MAX_OUTPUT_TOKENS,
                        "batch_number": 1,
                        "batch_count": 1,
                        "allowed_targets": set(),
                        "window_plan": window_plan,
                    }
                ]
            else:
                action_batches = build_patch_action_batches(actions)
                job_specs = []
                for batch_number, batch_actions in enumerate(action_batches, start=1):
                    batch_plan = {
                        **state["plan"],
                        "actions_by_chapter": {chapter_index: batch_actions},
                    }
                    job_specs.append(
                        {
                            "mode": "paragraph",
                            "messages": build_chapter_patch_prompt(
                                novel,
                                story_event,
                                chapter,
                                state["target_chapters"],
                                batch_plan,
                            ),
                            "initial_max_output_tokens": REVISION_MAX_OUTPUT_TOKENS,
                            "expanded_max_output_tokens": REVISION_MAX_OUTPUT_TOKENS,
                            "batch_number": batch_number,
                            "batch_count": len(action_batches),
                            "allowed_targets": {
                                (chapter_index, paragraph_index)
                                for action in batch_actions
                                for paragraph_index in action["paragraph_indexes"]
                            },
                            "window_plan": None,
                        }
                    )
            for spec in job_specs:
                _, patch_budget = build_dynamic_revision_config(
                    revision_llm_config,
                    spec["messages"],
                    max_output_tokens=spec["initial_max_output_tokens"],
                )
                job = {
                    "state": state,
                    "chapter": chapter,
                    "base_content_hash": base_content_hash,
                    "budget": patch_budget,
                    **spec,
                }
                patch_jobs.append(job)
                if task is not None:
                    emit_task_event(
                        db,
                        task,
                        event_type="revision_patch_stream",
                        step_key=(
                            f"revision_package_{state['package_number']}"
                            f"_chapter_{chapter.chapter_index}"
                            f"_batch_{spec['batch_number']}"
                        ),
                        status="running",
                        title=(
                            f"正在生成第 {chapter.chapter_index} 章连续窗口补丁"
                            if spec["mode"] == "window"
                            else (
                                f"正在生成第 {chapter.chapter_index} 章补丁"
                                f"第 {spec['batch_number']}/{spec['batch_count']} 批"
                            )
                        ),
                        message=(
                            f"本次处理不超过 {PATCH_BATCH_SIZE} 个目标段落；"
                            f"输入约 {patch_budget['prompt_chars']} 字符"
                            if spec["mode"] == "paragraph"
                            else (
                                f"连续压缩任务已改用 {len(window_plan['windows_by_chapter'][chapter_index])}"
                                " 个原子窗口整体改写"
                            )
                        ),
                        progress=85,
                        chapter_id=chapter.id,
                        chapter_index=chapter.chapter_index,
                        payload={
                            **patch_budget,
                            "mode": spec["mode"],
                            "batch_number": spec["batch_number"],
                            "batch_count": spec["batch_count"],
                            "target_paragraph_count": target_paragraph_count,
                            "parallel_limit": MAX_CONCURRENT_REPAIR_REQUESTS,
                        },
                    )

    if patch_jobs:
        with ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_REPAIR_REQUESTS,
            thread_name_prefix="event-revision-patch",
        ) as executor:
            patch_futures = {
                executor.submit(
                    execute_patch_request_with_budget_retry,
                    revision_llm_config,
                    job["messages"],
                    initial_max_output_tokens=job["initial_max_output_tokens"],
                    expanded_max_output_tokens=job["expanded_max_output_tokens"],
                ): job
                for job in patch_jobs
            }
            for future in as_completed(patch_futures):
                job = patch_futures[future]
                state = job["state"]
                chapter = job["chapter"]
                try:
                    parsed, telemetry = future.result()
                    chapter_result = state["chapter_results"].setdefault(
                        chapter.chapter_index,
                        {
                            "mode": job["mode"],
                            "base_content_hash": job["base_content_hash"],
                            "batches": [],
                            "window_plan": job["window_plan"],
                        },
                    )
                    chapter_result["batches"].append(
                        {
                            "parsed": parsed,
                            "telemetry": telemetry,
                            "allowed_targets": job["allowed_targets"],
                            "batch_number": job["batch_number"],
                            "batch_count": job["batch_count"],
                        }
                    )
                    if task is not None:
                        emit_task_event(
                            db,
                            task,
                            event_type="revision_patch_stream",
                            step_key=(
                                f"revision_package_{state['package_number']}"
                                f"_chapter_{chapter.chapter_index}"
                                f"_batch_{job['batch_number']}"
                            ),
                            status="completed",
                            title=(
                                f"第 {chapter.chapter_index} 章连续窗口补丁接收完成"
                                if job["mode"] == "window"
                                else (
                                    f"第 {chapter.chapter_index} 章补丁"
                                    f"第 {job['batch_number']}/{job['batch_count']} 批接收完成"
                                )
                            ),
                            message=(
                                f"已接收 {telemetry['output_chars']} 字符，"
                                "等待统一校验和应用"
                            ),
                            progress=87,
                            chapter_id=chapter.id,
                            chapter_index=chapter.chapter_index,
                            payload=telemetry,
                        )
                except Exception as exc:
                    telemetry = getattr(exc, "telemetry", job["budget"])
                    error_state = state["chapter_errors"].setdefault(
                        chapter.chapter_index,
                        {"error": "", "failed_batches": []},
                    )
                    error_state["failed_batches"].append(
                        {
                            "batch_number": job["batch_number"],
                            "mode": job["mode"],
                            "error": str(exc),
                            "telemetry": telemetry,
                        }
                    )
                    error_state["error"] = (
                        f"第 {job['batch_number']}/{job['batch_count']} 批失败：{exc}"
                    )
                    if task is not None:
                        emit_task_event(
                            db,
                            task,
                            event_type="revision_patch_stream",
                            step_key=(
                                f"revision_package_{state['package_number']}"
                                f"_chapter_{chapter.chapter_index}"
                                f"_batch_{job['batch_number']}"
                            ),
                            status="failed",
                            title=(
                                f"第 {chapter.chapter_index} 章连续窗口补丁生成失败"
                                if job["mode"] == "window"
                                else (
                                    f"第 {chapter.chapter_index} 章补丁"
                                    f"第 {job['batch_number']}/{job['batch_count']} 批失败"
                                )
                            ),
                            message=str(exc),
                            progress=87,
                            chapter_id=chapter.id,
                            chapter_index=chapter.chapter_index,
                            payload=telemetry,
                        )

    for state in component_states:
        package_number = state["package_number"]
        package = state["package"]
        package_result: dict[str, Any] = {
            "component_id": package["component_id"],
            "requested_chapter_indexes": package["chapter_indexes"],
            "chapter_indexes": [],
            "issue_count": len(package["issues"]),
            "status": "failed",
            "plan_telemetry": state["plan_telemetry"],
            "chapter_errors": state["chapter_errors"],
            "rejected_patches": [],
            "patch_count": 0,
        }
        if state.get("plan_error"):
            package_result["error"] = state["plan_error"]
            result["repair_packages"].append(package_result)
            continue
        package_result["repair_plan"] = state["plan"]
        applied_chapter_indexes: set[int] = set()
        for chapter_index, chapter_result in state["chapter_results"].items():
            chapter = chapters_by_index[chapter_index]
            try:
                db.refresh(chapter)
                expected_hashes = {
                    chapter_index: chapter_result["base_content_hash"],
                }
                summaries: dict[int, str] = {}
                if chapter_result["mode"] == "window":
                    batch = chapter_result["batches"][0]
                    window_patches, summaries = normalize_structural_window_patches(
                        batch["parsed"],
                        {chapter_index: chapter},
                        chapter_result["window_plan"],
                        expected_base_content_hashes=expected_hashes,
                    )
                    window_result = apply_structural_window_patches(
                        {chapter_index: chapter},
                        window_patches,
                    )[chapter_index]
                    content = window_result["content"]
                    validation = window_result["validation"]
                    safe_patches = window_patches
                    rejected: list[dict[str, Any]] = []
                    strategy = "local_window_rewrite"
                else:
                    normalized_patches: list[dict[str, Any]] = []
                    seen_targets: set[tuple[int, int]] = set()
                    for batch in sorted(
                        chapter_result["batches"],
                        key=lambda item: item["batch_number"],
                    ):
                        try:
                            batch_patches = normalize_paragraph_patches(
                                batch["parsed"],
                                {chapter_index: chapter},
                                allowed_targets=batch["allowed_targets"],
                                expected_base_content_hashes=expected_hashes,
                            )
                        except ValueError as exc:
                            error_state = state["chapter_errors"].setdefault(
                                chapter_index,
                                {"error": "", "failed_batches": []},
                            )
                            error_state["failed_batches"].append(
                                {
                                    "batch_number": batch["batch_number"],
                                    "mode": "paragraph",
                                    "error": str(exc),
                                    "telemetry": batch["telemetry"],
                                }
                            )
                            error_state["error"] = (
                                f"第 {batch['batch_number']}/"
                                f"{batch['batch_count']} 批补丁无效：{exc}"
                            )
                            continue
                        for patch in batch_patches:
                            target = (
                                patch["chapter_index"],
                                patch["paragraph_index"],
                            )
                            if target in seen_targets:
                                continue
                            seen_targets.add(target)
                            normalized_patches.append(patch)
                    if not normalized_patches:
                        raise ValueError("本章所有成功返回批次均未产生可应用补丁")
                    content, validation, safe_patches, rejected = (
                        isolate_safe_event_patches(chapter, normalized_patches)
                    )
                    strategy = "shared_plan_chapter_patch"
                package_result["rejected_patches"].extend(rejected)
                if not safe_patches or validation is None:
                    existing_error = state["chapter_errors"].get(chapter_index) or {}
                    state["chapter_errors"][chapter_index] = {
                        **existing_error,
                        "error": "本章补丁均未通过版本或字数校验",
                    }
                    continue
                patch_records: list[ChapterRevisionPatch] = []
                for patch in safe_patches:
                    if strategy == "local_window_rewrite":
                        paragraph_index = patch["start_paragraph"]
                        paragraph_id = (
                            f"ch{chapter_index}-p{patch['start_paragraph']}"
                            f"-p{patch['end_paragraph']}"
                        )
                        old_text = "\n\n".join(patch["old_paragraphs"])
                        new_text = "\n\n".join(patch["new_paragraphs"])
                        old_text_hash = text_hash(old_text)
                    else:
                        paragraph_index = patch["paragraph_index"]
                        paragraph_id = patch["paragraph_id"]
                        old_text = patch["old_text"]
                        new_text = patch["new_text"]
                        old_text_hash = patch["old_text_hash"]
                    record = ChapterRevisionPatch(
                        novel_id=novel.id,
                        task_id=task.id if task is not None else None,
                        story_event_id=story_event.id,
                        chapter_id=chapter.id,
                        chapter_index=patch["chapter_index"],
                        paragraph_index=paragraph_index,
                        paragraph_id=paragraph_id,
                        base_content_hash=patch["base_content_hash"],
                        old_text_hash=old_text_hash,
                        old_text=old_text,
                        new_text=new_text,
                        reason=patch["reason"],
                        status="applied",
                        validation=validation,
                        applied_at=datetime.now(timezone.utc),
                    )
                    db.add(record)
                    patch_records.append(record)
                chapter.content = content
                chapter.word_count = count_chapter_words(content)
                if summaries.get(chapter_index):
                    chapter.summary = summaries[chapter_index]
                chapter.status = "done"
                chapter.context_snapshot = {
                    **(chapter.context_snapshot or {}),
                    "event_revision": {
                        "story_event_id": str(story_event.id),
                        "round": 1,
                        "strategy": strategy,
                        "component_id": package["component_id"],
                        "patch_count": len(safe_patches),
                        "word_guard": validation["after"],
                    },
                }
                package_result["patch_count"] += len(safe_patches)
                applied_chapter_indexes.add(chapter_index)
                db.commit()
                for record in patch_records:
                    db.refresh(record)
            except Exception as exc:
                db.rollback()
                existing_error = state["chapter_errors"].get(chapter_index) or {}
                state["chapter_errors"][chapter_index] = {
                    **existing_error,
                    "error": str(exc),
                }

        for plan in plans:
            if plan.chapter_index in applied_chapter_indexes:
                plan.status = "revised"
                plan.payload = {
                    **(plan.payload or {}),
                    "event_revision_round": 1,
                }
        db.commit()
        package_result["chapter_indexes"] = sorted(applied_chapter_indexes)
        package_result["chapter_errors"] = state["chapter_errors"]
        has_errors = bool(
            state["chapter_errors"] or package_result["rejected_patches"]
        )
        if applied_chapter_indexes:
            package_result["status"] = "partial" if has_errors else "resolved"
        if task is not None:
            emit_task_event(
                db,
                task,
                event_type="revision_package",
                step_key=f"revision_package_{package_number}",
                status=(
                    "completed"
                    if package_result["status"] in {"resolved", "partial"}
                    else "failed"
                ),
                title=(
                    f"第 {package_number}/{len(packages)} 个事件修订组件已应用"
                    if applied_chapter_indexes
                    else f"第 {package_number}/{len(packages)} 个事件修订组件未应用"
                ),
                message=(
                    f"已安全应用 {package_result['patch_count']} 个段落补丁"
                    + (
                        f"；{len(state['chapter_errors'])} 个章节任务待重试"
                        if state["chapter_errors"]
                        else ""
                    )
                    + (
                        f"；{len(package_result['rejected_patches'])} 个不安全补丁已跳过"
                        if package_result["rejected_patches"]
                        else ""
                    )
                ),
                progress=90,
                payload={
                    "status": package_result["status"],
                    "patch_count": package_result["patch_count"],
                    "chapter_indexes": sorted(applied_chapter_indexes),
                    "chapter_errors": state["chapter_errors"],
                    "rejected_patch_count": len(package_result["rejected_patches"]),
                },
            )
        result["repair_packages"].append(package_result)

    if packages and task is not None:
        emit_task_event(
            db,
            task,
            event_type="review",
            step_key="event_review_final",
            status="running",
            title="正在复检局部修订结果",
            message="确认补丁没有引入新的时间线、人物或资源状态冲突",
            progress=91,
        )
    final_review = (
        sync_event_quality_issues(
            db=db,
            novel=novel,
            story_event=story_event,
            llm_config=llm_config,
            task=task,
        )
        if packages
        else initial_review
    )
    retryable_chapter_indexes = sorted(
        {
            int(chapter_index)
            for package in result["repair_packages"]
            for chapter_index in (package.get("chapter_errors") or {})
            if str(chapter_index).isdigit()
        }
    )
    unresolved_repair_issues: list[dict[str, Any]] = []
    unresolved_issue_ids: set[str] = set()
    retryable_index_set = set(retryable_chapter_indexes)
    for package in packages:
        for issue in package.get("issues", []):
            affected = set(issue.get("affected_chapter_indexes", []))
            issue_id = str(issue.get("issue_id") or "")
            if not (affected & retryable_index_set) or issue_id in unresolved_issue_ids:
                continue
            unresolved_issue_ids.add(issue_id)
            unresolved_repair_issues.append(issue)

    if packages and task is not None:
        final_issues = final_review.get("quality_report", {}).get("issues", [])
        is_partial = bool(retryable_chapter_indexes)
        emit_task_event(
            db,
            task,
            event_type="review",
            step_key="event_review_final",
            status="completed",
            title="事件复检部分完成" if is_partial else "局部修订复检完成",
            message=(
                f"复检已完成，但第 {', '.join(map(str, retryable_chapter_indexes))} 章补丁生成失败，可单独重试"
                if is_partial
                else f"复检后保留 {len(final_issues)} 条质量记录"
            ),
            progress=94,
            payload={
                "issue_count": len(final_issues),
                "issues": final_issues,
                "partial": is_partial,
                "retryable": is_partial,
                "retryable_chapter_indexes": retryable_chapter_indexes,
                "unresolved_repair_issues": unresolved_repair_issues,
                **(
                    final_review.get("quality_report", {}).get(
                        "request_telemetry",
                        {},
                    )
                ),
            },
        )
    result["final_review"] = final_review
    result["retryable_chapter_indexes"] = retryable_chapter_indexes
    result["unresolved_repair_issues"] = unresolved_repair_issues
    result["remaining_open_risks"] = max(
        int(final_review.get("remaining_open_risks", 0) or 0),
        len(unresolved_repair_issues),
    )
    result["status"] = (
        "partial"
        if retryable_chapter_indexes
        else ("needs_review" if result["remaining_open_risks"] else "completed")
    )
    result["repaired_chapter_count"] = len(
        {
            index
            for package in result["repair_packages"]
            if package["status"] in {"resolved", "partial"}
            for index in package.get("chapter_indexes", [])
        }
    )
    return result


def execute_structural_component_retry(
    db: Session,
    *,
    novel: Novel,
    story_event: StoryEvent,
    source_task: GenerationTask,
    package: dict[str, Any],
    package_number: int,
    chapters_by_index: dict[int, Chapter],
    plans: list[EventChapterPlan],
    revision_llm_config: LLMConfig,
) -> dict[str, Any]:
    """对整个问题组件重新规划并原子应用安全的章内窗口改写。"""
    component_indexes = sorted(
        {
            int(index)
            for index in package.get("requested_chapter_indexes", [])
            if str(index).isdigit() and int(index) in chapters_by_index
        }
        or {
            int(index)
            for issue in package.get("issues") or []
            for index in issue.get("affected_chapter_indexes", [])
            if str(index).isdigit() and int(index) in chapters_by_index
        }
    )
    if not component_indexes:
        raise ValueError("结构重排缺少可用的问题章节")
    component_chapters = [chapters_by_index[index] for index in component_indexes]
    base_content_hashes = {
        chapter.chapter_index: text_hash(chapter.content or "")
        for chapter in component_chapters
    }
    step_key = f"revision_package_{package_number}_structural_replan"
    telemetry: dict[str, Any] = {
        "manual_retry": True,
        "structural_replan": True,
        "component_chapter_indexes": component_indexes,
    }
    emit_task_event(
        db,
        source_task,
        event_type="revision_patch_stream",
        step_key=step_key,
        status="running",
        title="正在重新规划结构性局部修订",
        message=(
            "原蓝图包含跨章合并或搬运，现保持章节数量不变，"
            "重新分配章节功能并定位章内改写窗口"
        ),
        progress=92,
        payload=telemetry,
    )

    plan_messages = build_structural_repair_plan_prompt(
        novel,
        story_event,
        component_chapters,
        package.get("issues") or [],
    )
    started = time.monotonic()
    plan_payload, plan_metrics = execute_patch_request_with_budget_retry(
        revision_llm_config,
        plan_messages,
        initial_max_output_tokens=REVISION_MAX_OUTPUT_TOKENS,
        expanded_max_output_tokens=REVISION_MAX_OUTPUT_TOKENS,
    )
    plan_metrics["total_elapsed_seconds"] = round(time.monotonic() - started, 3)
    structural_plan = normalize_structural_repair_plan(
        plan_payload,
        {index: chapters_by_index[index] for index in component_indexes},
    )
    telemetry["plan"] = plan_metrics
    telemetry["chapter_roles"] = list(structural_plan["chapter_roles"].values())
    telemetry["window_count"] = sum(
        len(items)
        for items in structural_plan["windows_by_chapter"].values()
    )
    emit_task_event(
        db,
        source_task,
        event_type="revision_patch_stream",
        step_key=step_key,
        status="running",
        title="正在生成结构窗口补丁",
        message=(
            f"已为 {len(component_indexes)} 章分配不同功能，"
            f"正在改写 {telemetry['window_count']} 个连续段落窗口"
        ),
        progress=93,
        payload=telemetry,
    )

    patch_messages = build_structural_window_patch_prompt(
        novel,
        story_event,
        component_chapters,
        structural_plan,
    )
    started = time.monotonic()
    patch_payload, patch_metrics = execute_patch_request_with_budget_retry(
        revision_llm_config,
        patch_messages,
        initial_max_output_tokens=REVISION_MAX_OUTPUT_TOKENS,
        expanded_max_output_tokens=REVISION_MAX_OUTPUT_TOKENS,
    )
    patch_metrics["total_elapsed_seconds"] = round(time.monotonic() - started, 3)
    window_patches, summaries = normalize_structural_window_patches(
        patch_payload,
        {index: chapters_by_index[index] for index in component_indexes},
        structural_plan,
        expected_base_content_hashes=base_content_hashes,
    )
    results = apply_structural_window_patches(
        {index: chapters_by_index[index] for index in component_indexes},
        window_patches,
    )
    telemetry["patch"] = patch_metrics
    telemetry["patch_count"] = len(window_patches)

    for chapter_index, result in results.items():
        chapter = chapters_by_index[chapter_index]
        chapter_patches = [
            patch
            for patch in window_patches
            if patch["chapter_index"] == chapter_index
        ]
        for patch in chapter_patches:
            start = patch["start_paragraph"]
            end = patch["end_paragraph"]
            db.add(
                ChapterRevisionPatch(
                    novel_id=novel.id,
                    task_id=source_task.id,
                    story_event_id=story_event.id,
                    chapter_id=chapter.id,
                    chapter_index=chapter_index,
                    paragraph_index=start,
                    paragraph_id=f"ch{chapter_index}-p{start}-p{end}",
                    base_content_hash=patch["base_content_hash"],
                    old_text_hash=text_hash("\n\n".join(patch["old_paragraphs"])),
                    old_text="\n\n".join(patch["old_paragraphs"]),
                    new_text="\n\n".join(patch["new_paragraphs"]),
                    reason=patch["reason"],
                    status="applied",
                    validation=result["validation"],
                    applied_at=datetime.now(timezone.utc),
                )
            )
        chapter.content = result["content"]
        chapter.word_count = count_chapter_words(chapter.content)
        if summaries.get(chapter_index):
            chapter.summary = summaries[chapter_index]
        chapter.status = "done"
        chapter.context_snapshot = {
            **(chapter.context_snapshot or {}),
            "event_revision": {
                "story_event_id": str(story_event.id),
                "round": 1,
                "strategy": "local_window_rewrite",
                "component_id": package.get("component_id"),
                "chapter_role": structural_plan["chapter_roles"][chapter_index],
                "windows": result["validation"]["windows"],
                "patch_count": len(chapter_patches),
                "word_guard": result["validation"]["after"],
            },
        }
        for plan in plans:
            if plan.chapter_index == chapter_index:
                plan.status = "revised"
    db.commit()
    emit_task_event(
        db,
        source_task,
        event_type="revision_patch_stream",
        step_key=step_key,
        status="completed",
        title="结构性局部修订完成",
        message=(
            f"已保持 {len(component_indexes)} 个章节及其边界，"
            f"应用 {len(window_patches)} 个窗口补丁"
        ),
        progress=94,
        payload=telemetry,
    )
    return {
        "applied_chapter_indexes": component_indexes,
        "patch_count": len(window_patches),
        "structural_plan": structural_plan,
        "telemetry": telemetry,
    }


def retry_failed_event_revision_chapters(
    db: Session,
    *,
    novel: Novel,
    story_event: StoryEvent,
    source_task: GenerationTask,
    chapter_indexes: list[int],
    revision_llm_config: LLMConfig | None,
    review_llm_config: LLMConfig | None,
) -> dict[str, Any]:
    """复用原修订蓝图，只重试上轮补丁生成失败的章节。"""
    if revision_llm_config is None:
        raise ValueError("未配置可用的正文模型 API")
    source_output = (source_task.result_payload or {}).get("output") or {}
    event_revision = source_output.get("event_revision") or (story_event.payload or {}).get("event_revision") or {}
    repair_packages = event_revision.get("repair_packages") or []
    retryable = {
        int(index)
        for index in event_revision.get("retryable_chapter_indexes", [])
        if str(index).isdigit()
    }
    if not retryable:
        retryable = {
            int(index)
            for package in repair_packages
            for index in (package.get("chapter_errors") or {})
            if str(index).isdigit()
        }
    requested = sorted({int(index) for index in chapter_indexes if int(index) in retryable})
    if not requested:
        raise ValueError("没有可重试的事件修订章节")

    plans = db.scalars(
        select(EventChapterPlan)
        .where(EventChapterPlan.story_event_id == story_event.id, EventChapterPlan.chapter_id.is_not(None))
        .order_by(EventChapterPlan.chapter_index.asc())
    ).all()
    chapters = [db.get(Chapter, plan.chapter_id) for plan in plans]
    chapters_by_index = {chapter.chapter_index: chapter for chapter in chapters if chapter is not None}
    applied_indexes: list[int] = []
    failed: dict[int, dict[str, Any]] = {}
    non_retryable_failed: set[int] = set()

    for package_number, package in enumerate(repair_packages, start=1):
        target_indexes = {
            int(index)
            for index in package.get("requested_chapter_indexes", [])
            if str(index).isdigit()
        }
        package_retry_indexes = sorted(target_indexes & set(requested))
        if not package_retry_indexes or not isinstance(package.get("repair_plan"), dict):
            continue
        if repair_package_requires_structural_replan(package):
            try:
                structural_result = execute_structural_component_retry(
                    db,
                    novel=novel,
                    story_event=story_event,
                    source_task=source_task,
                    package=package,
                    package_number=package_number,
                    chapters_by_index=chapters_by_index,
                    plans=list(plans),
                    revision_llm_config=revision_llm_config,
                )
                component_applied = structural_result["applied_chapter_indexes"]
                applied_indexes.extend(component_applied)
                package["patch_count"] = (
                    int(package.get("patch_count") or 0)
                    + int(structural_result["patch_count"])
                )
                package["chapter_indexes"] = sorted(
                    {
                        *package.get("chapter_indexes", []),
                        *component_applied,
                    }
                )
                errors = dict(package.get("chapter_errors") or {})
                for chapter_index in component_applied:
                    errors.pop(str(chapter_index), None)
                    errors.pop(chapter_index, None)
                package["chapter_errors"] = errors
                package["status"] = "partial" if errors else "resolved"
                package["structural_replan"] = {
                    "status": "completed",
                    "strategy": "local_window_rewrite",
                    "chapter_indexes": component_applied,
                    "patch_count": structural_result["patch_count"],
                    "chapter_roles": list(
                        structural_result["structural_plan"]["chapter_roles"].values()
                    ),
                }
            except Exception as exc:
                db.rollback()
                for chapter_index in package_retry_indexes:
                    non_retryable_failed.add(chapter_index)
                    failed[chapter_index] = {
                        "error": str(exc),
                        "telemetry": {
                            "manual_retry": True,
                            "structural_replan": True,
                        },
                    }
                emit_task_event(
                    db,
                    source_task,
                    event_type="revision_patch_stream",
                    step_key=f"revision_package_{package_number}_structural_replan",
                    status="failed",
                    title="结构性局部修订失败",
                    message=str(exc),
                    progress=94,
                    payload={
                        "manual_retry": True,
                        "structural_replan": True,
                        "retryable": False,
                        "failed_chapter_indexes": package_retry_indexes,
                    },
                )
            continue
        repair_plan = dict(package["repair_plan"])
        repair_plan["actions_by_chapter"] = {
            int(index): actions
            for index, actions in (repair_plan.get("actions_by_chapter") or {}).items()
            if str(index).isdigit()
        }
        target_chapters = [chapters_by_index[index] for index in sorted(target_indexes) if index in chapters_by_index]

        for chapter_index in package_retry_indexes:
            chapter = chapters_by_index.get(chapter_index)
            actions = repair_plan["actions_by_chapter"].get(chapter_index, [])
            if chapter is None or not actions:
                failed[chapter_index] = {"error": "原修订蓝图缺少本章目标段落"}
                continue
            base_content_hash = text_hash(chapter.content or "")
            action_batches = build_patch_action_batches(actions)
            target_paragraph_count = sum(
                len(action.get("paragraph_indexes", []))
                for batch in action_batches
                for action in batch
            )
            telemetry: dict[str, Any] = {
                "manual_retry": True,
                "target_paragraph_count": target_paragraph_count,
                "batch_count": len(action_batches),
                "batch_size": PATCH_BATCH_SIZE,
                "streaming": True,
            }
            emit_task_event(
                db,
                source_task,
                event_type="revision_patch_stream",
                step_key=f"revision_package_{package_number}_chapter_{chapter_index}",
                status="running",
                title=f"正在单独重试第 {chapter_index} 章事件补丁",
                message=(
                    f"复用原修订蓝图，将本章 {target_paragraph_count} 个目标段落"
                    f"拆成 {len(action_batches)} 批处理"
                ),
                progress=92,
                chapter_id=chapter.id,
                chapter_index=chapter_index,
                payload=telemetry,
            )
            try:
                normalized: list[dict[str, Any]] = []
                batch_telemetry: list[dict[str, Any]] = []
                for batch_number, batch_actions in enumerate(action_batches, start=1):
                    batch_plan = {
                        **repair_plan,
                        "actions_by_chapter": {chapter_index: batch_actions},
                    }
                    messages = build_chapter_patch_prompt(
                        novel,
                        story_event,
                        chapter,
                        target_chapters,
                        batch_plan,
                    )
                    batch_target_count = sum(
                        len(action.get("paragraph_indexes", []))
                        for action in batch_actions
                    )
                    max_output_tokens = REVISION_MAX_OUTPUT_TOKENS
                    _, current_telemetry = build_dynamic_revision_config(
                        revision_llm_config,
                        messages,
                        max_output_tokens=max_output_tokens,
                    )
                    current_telemetry.update({
                        "batch_number": batch_number,
                        "batch_count": len(action_batches),
                        "batch_target_count": batch_target_count,
                        "expanded_max_output_tokens": REVISION_MAX_OUTPUT_TOKENS,
                    })
                    emit_task_event(
                        db,
                        source_task,
                        event_type="revision_patch_stream",
                        step_key=f"revision_package_{package_number}_chapter_{chapter_index}",
                        status="running",
                        title=f"正在处理第 {chapter_index} 章补丁第 {batch_number}/{len(action_batches)} 批",
                        message=f"本批只处理 {batch_target_count} 个目标段落",
                        progress=min(93, 91 + batch_number),
                        chapter_id=chapter.id,
                        chapter_index=chapter_index,
                        payload={**telemetry, **current_telemetry},
                    )
                    started_at = time.monotonic()
                    last_activity_notice_at = 0.0

                    def report_patch_activity(activity: dict[str, Any]) -> None:
                        nonlocal last_activity_notice_at
                        now = time.monotonic()
                        if now - last_activity_notice_at < 10:
                            return
                        last_activity_notice_at = now
                        emit_task_event(
                            db,
                            source_task,
                            event_type="revision_patch_stream",
                            step_key=(
                                f"revision_package_{package_number}"
                                f"_chapter_{chapter_index}"
                            ),
                            status="running",
                            title=(
                                f"第 {chapter_index} 章补丁第 "
                                f"{batch_number}/{len(action_batches)} 批正在生成"
                            ),
                            message=(
                                "连接持续活跃；"
                                f"已接收思考 {int(activity.get('reasoning_chars') or 0)} 字符、"
                                f"最终输出 {int(activity.get('output_chars') or 0)} 字符；"
                                f"当前额度 {int(activity.get('max_output_tokens') or 0)} tokens"
                            ),
                            progress=min(93, 91 + batch_number),
                            chapter_id=chapter.id,
                            chapter_index=chapter_index,
                            payload={
                                **telemetry,
                                **current_telemetry,
                                "stream_activity": activity,
                            },
                        )

                    try:
                        parsed, request_telemetry = execute_patch_request_with_budget_retry(
                            revision_llm_config,
                            messages,
                            initial_max_output_tokens=max_output_tokens,
                            expanded_max_output_tokens=REVISION_MAX_OUTPUT_TOKENS,
                            on_activity=report_patch_activity,
                        )
                    except Exception as exc:
                        error_telemetry = getattr(exc, "telemetry", {})
                        current_telemetry.update({
                            "elapsed_seconds": round(time.monotonic() - started_at, 3),
                            "error": str(exc),
                            **error_telemetry,
                        })
                        telemetry.update(current_telemetry)
                        raise
                    current_telemetry.update({
                        "elapsed_seconds": round(time.monotonic() - started_at, 3),
                        **request_telemetry,
                    })
                    batch_telemetry.append(current_telemetry)
                    allowed_targets = {
                        (chapter_index, paragraph_index)
                        for action in batch_actions
                        for paragraph_index in action.get("paragraph_indexes", [])
                    }
                    normalized.extend(normalize_paragraph_patches(
                        parsed,
                        {chapter_index: chapter},
                        allowed_targets=allowed_targets,
                        expected_base_content_hashes={
                            chapter_index: base_content_hash,
                        },
                    ))
                telemetry.update({
                    "batches": batch_telemetry,
                    "output_chars": sum(item.get("output_chars", 0) for item in batch_telemetry),
                    "elapsed_seconds": round(sum(item.get("elapsed_seconds", 0) for item in batch_telemetry), 3),
                })
                content, validation, safe_patches, rejected = isolate_safe_event_patches(chapter, normalized)
                if not safe_patches or validation is None:
                    raise ValueError("本章补丁均未通过版本或字数校验")
                for patch in safe_patches:
                    db.add(ChapterRevisionPatch(
                        novel_id=novel.id,
                        task_id=source_task.id,
                        story_event_id=story_event.id,
                        chapter_id=chapter.id,
                        chapter_index=patch["chapter_index"],
                        paragraph_index=patch["paragraph_index"],
                        paragraph_id=patch["paragraph_id"],
                        base_content_hash=patch["base_content_hash"],
                        old_text_hash=patch["old_text_hash"],
                        old_text=patch["old_text"],
                        new_text=patch["new_text"],
                        reason=patch["reason"],
                        status="applied",
                        validation=validation,
                        applied_at=datetime.now(timezone.utc),
                    ))
                chapter.content = content
                chapter.word_count = count_chapter_words(content)
                chapter.status = "done"
                chapter.context_snapshot = {
                    **(chapter.context_snapshot or {}),
                    "event_revision": {
                        "story_event_id": str(story_event.id),
                        "round": 1,
                        "strategy": "failed_chapter_retry",
                        "component_id": package.get("component_id"),
                        "patch_count": len(safe_patches),
                        "word_guard": validation["after"],
                    },
                }
                for plan in plans:
                    if plan.chapter_index == chapter_index:
                        plan.status = "revised"
                db.commit()
                applied_indexes.append(chapter_index)
                package["patch_count"] = int(package.get("patch_count") or 0) + len(safe_patches)
                package["chapter_indexes"] = sorted({*package.get("chapter_indexes", []), chapter_index})
                errors = dict(package.get("chapter_errors") or {})
                errors.pop(str(chapter_index), None)
                errors.pop(chapter_index, None)
                package["chapter_errors"] = errors
                package["rejected_patches"] = [*(package.get("rejected_patches") or []), *rejected]
                package["status"] = "partial" if errors else "resolved"
                emit_task_event(
                    db,
                    source_task,
                    event_type="revision_patch_stream",
                    step_key=f"revision_package_{package_number}_chapter_{chapter_index}",
                    status="completed",
                    title=f"第 {chapter_index} 章事件补丁重试完成",
                    message=f"已安全应用 {len(safe_patches)} 个段落补丁",
                    progress=94,
                    chapter_id=chapter.id,
                    chapter_index=chapter_index,
                    payload=telemetry,
                )
            except Exception as exc:
                db.rollback()
                failed[chapter_index] = {"error": str(exc), "telemetry": telemetry}
                emit_task_event(
                    db,
                    source_task,
                    event_type="revision_patch_stream",
                    step_key=f"revision_package_{package_number}_chapter_{chapter_index}",
                    status="failed",
                    title=f"第 {chapter_index} 章事件补丁重试失败",
                    message=str(exc),
                    progress=94,
                    chapter_id=chapter.id,
                    chapter_index=chapter_index,
                    payload={**telemetry, "retryable": True, "manual_retry": True},
                )

    remaining_retryable = sorted(
        (
            (retryable - set(applied_indexes))
            | set(failed)
        )
        - non_retryable_failed
    )
    final_review = sync_event_quality_issues(
        db=db,
        novel=novel,
        story_event=story_event,
        llm_config=review_llm_config,
        task=source_task,
    ) if review_llm_config is not None else event_revision.get("final_review", {})
    remaining_risks = max(
        int(final_review.get("remaining_open_risks", 0) or 0),
        len(remaining_retryable),
        len(non_retryable_failed),
    )
    event_revision.update({
        "repair_packages": repair_packages,
        "final_review": final_review,
        "retryable_chapter_indexes": remaining_retryable,
        "remaining_open_risks": remaining_risks,
        "status": "partial" if remaining_retryable else ("needs_review" if remaining_risks else "completed"),
        "repaired_chapter_count": int(event_revision.get("repaired_chapter_count") or 0) + len(applied_indexes),
        "non_retryable_chapter_indexes": sorted(non_retryable_failed),
    })
    event_revision["unresolved_repair_issues"] = [
        issue
        for issue in event_revision.get("unresolved_repair_issues", [])
        if set(issue.get("affected_chapter_indexes", [])) & set(remaining_retryable)
    ]
    source_task.result_payload = {
        **(source_task.result_payload or {}),
        "output": {**source_output, "event_revision": event_revision},
    }
    story_event.remaining_open_risks = remaining_risks
    story_event.auto_repair_count = (story_event.auto_repair_count or 0) + len(applied_indexes)
    story_event.status = "paused" if remaining_risks else "completed"
    story_event.payload = {
        **(story_event.payload or {}),
        "event_revision": event_revision,
        "quality_revision_pause": ({
            "story_event_id": str(story_event.id),
            "remaining_open": remaining_risks,
            "scope": "event",
        } if remaining_risks else {}),
    }
    db.commit()
    emit_task_event(
        db,
        source_task,
        event_type="review",
        step_key="event_review_final",
        status="failed" if non_retryable_failed else "completed",
        title=(
            "结构重排仍未通过"
            if non_retryable_failed
            else "事件补丁重试复检完成"
        ),
        message=(
            (
                f"第 {', '.join(map(str, sorted(non_retryable_failed)))} 章"
                "无法通过安全局部修订解决，需要重新生成受影响章节"
            )
            if non_retryable_failed
            else f"已重试但第 {', '.join(map(str, remaining_retryable))} 章仍失败，可再次重试"
            if remaining_retryable
            else f"章节补丁已补齐，当前保留 {remaining_risks} 条开放风险"
        ),
        progress=95,
        payload={
            "partial": bool(remaining_retryable),
            "retryable": bool(remaining_retryable),
            "manual_retry": True,
            "retry_failed": bool(failed),
            "retryable_chapter_indexes": remaining_retryable,
            "non_retryable_chapter_indexes": sorted(non_retryable_failed),
            "applied_chapter_indexes": sorted(applied_indexes),
            "failed_chapter_indexes": sorted(failed),
            "failed_chapters": failed,
        },
    )
    return {
        "agent": "EventRevisionRetryAgent",
        "source_task_id": str(source_task.id),
        "story_event_id": str(story_event.id),
        "applied_chapter_indexes": sorted(applied_indexes),
        "failed_chapters": failed,
        "retryable_chapter_indexes": remaining_retryable,
        "non_retryable_chapter_indexes": sorted(non_retryable_failed),
        "remaining_open_risks": remaining_risks,
        "event_revision": event_revision,
    }
