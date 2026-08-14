"""事件级多章节生成 LangGraph。

这个 Graph 负责把“一键围绕一个完整大事件生成多章”的流程编排起来。
事件规划会持久化到 StoryEvent / EventChapterPlan，并同步任务进度、章节上下文快照和事件质量结果。
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select
from sqlalchemy.orm import Session


API_DIR = Path(__file__).resolve().parents[3] / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from app.models.chapter import Chapter
from app.models.auto_novel_run import AutoNovelRun
from app.models.event_chapter_plan import EventChapterPlan
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.models.story_event import StoryEvent
from app.models.user import User
from app.db.session import SessionLocal
from app.services.event_research import collect_event_research
from app.services.agents.story_planning_agent import get_story_bible_context
from app.services.agent_contracts import AgentFailure, agent_contract
from app.services.agent_orchestrator import enqueue_chapter_memory_tasks
from app.services.chapter_context_builder import build_chapter_context
from app.services.chapter_progress import (
    apply_revised_next_plan,
    compact_next_chapter_boundary,
    compact_story_event_for_chapter,
)
from app.services.chapter_fact_delta import sync_chapter_fact_delta
from app.services.chapter_pipeline import (
    ChapterPipelineRequest,
    run_chapter_pipeline,
    sync_event_plan_after_pipeline,
)
from app.services.chapter_review_cycle import review_and_revise_chapter_once
from app.services.event_revision_service import review_and_repair_story_event
from app.services.graph_checkpoint import load_graph_checkpoint, save_graph_checkpoint
from app.services.llm_client import (
    LLMClient,
    LLMRequestCancelledError,
    build_llm_config,
    build_review_llm_config,
)
from app.services.pacing_plan import NARRATIVE_CLOSING_EVENT_TYPES
from app.services.prompt_context import compact_story_bible
from app.services.sample_rag import build_plot_design_reference_pack
from app.services.storytelling_craft import (
    build_scene_execution_schema,
    normalize_scene_execution,
    scene_execution_is_complete,
)
from app.services.task_events import ModelThinkingPublisher, emit_task_event
from app.services.tone_pacing_contract import resolve_event_chapter_count


class EventGenerationState(TypedDict, total=False):
    """Graph 节点之间传递的可序列化状态。"""

    input: dict[str, Any]
    event_plan: dict[str, Any]
    chapter_plans: list[dict[str, Any]]
    next_plan_index: int
    start_chapter_index: int
    story_event_id: str
    generated_chapters: list[dict[str, Any]]
    last_chapter_id: str
    last_chapter_progress: dict[str, Any]
    last_issue_count: int
    memory_sync: list[dict[str, Any]]
    continuity_reviews: list[dict[str, Any]]
    revision_results: list[dict[str, Any]]
    event_summary: dict[str, Any]
    word_revision_pause: dict[str, Any]
    quality_revision_pause: dict[str, Any]
    pause_requested: bool
    immediate_pause: dict[str, Any]
    research_source_ids: list[str]
    research_summary: dict[str, Any]
    event_revision: dict[str, Any]
    plot_reference_pack: dict[str, Any]
    _resume_node: str


class EventPlanningQualityError(RuntimeError):
    """规划模型多次违反作品硬约束；不得降级成占位剧情继续生成。"""


MAX_AUXILIARY_REPAIR_ROUNDS = 3


def _set_task_progress(
    db: Session,
    task: GenerationTask,
    progress: int,
    note: str,
    *,
    step_key: str = "",
    status: str = "running",
    chapter_index: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """把 Graph 执行阶段同步到任务记录，方便前端轮询。"""
    task.progress = max(task.progress or 0, min(progress, 95))
    task.result_payload = {
        **(task.result_payload or {}),
        "graph_status": note,
    }
    db.commit()
    db.refresh(task)
    emit_task_event(
        db,
        task,
        event_type="step",
        step_key=step_key or f"progress_{progress}",
        status=status,
        title=note,
        progress=task.progress,
        chapter_index=chapter_index,
        payload=payload,
    )


def _event_research_message(research_summary: dict[str, Any]) -> str:
    """说明本次事件实际关联了多少条情节、表达与现实资料。"""
    source_count = len(research_summary.get("source_ids", []))
    if research_summary.get("reason") == "not_needed":
        return "未找到可用的情节与表达资料"
    return f"已关联 {source_count} 条情节、表达与现实资料"


def _clip_chapter_count(value: Any, allow_single_chapter: bool = False) -> int:
    """限制事件章节数量，让一个大事件保持正常网文章节节奏。"""
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 6
    if allow_single_chapter and count == 1:
        return 1
    return max(4, min(count, 12))


def _compact_text(value: Any, max_length: int, fallback: str = "") -> str:
    """把模型输出压缩到数据库短字段允许的长度。"""
    text = str(value or fallback or "").strip()
    text = " ".join(text.split())
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip("，。；、,. ") + "…"


def _derive_event_title(raw: dict[str, Any], novel: Novel, start_index: int, chapter_count: int) -> str:
    """生成适合 StoryEvent.title 的短标题，避免把剧情方向整段写入标题字段。"""
    explicit_title = _compact_text(raw.get("event_title"), 80)
    if explicit_title and "\n" not in explicit_title and len(explicit_title) <= 80:
        return explicit_title

    brief = novel.brief or {}
    genre = brief.get("work_type") or novel.genre or "剧情"
    selling_points = _compact_text(brief.get("selling_points"), 18)
    if selling_points:
        return f"第{start_index}-{start_index + chapter_count - 1}章：{selling_points}事件"
    return f"第{start_index}-{start_index + chapter_count - 1}章：{genre}阶段事件"


def _pacing_state_from_input(task_input: dict[str, Any]) -> dict[str, Any]:
    """读取整书生产总控传入的阶段信息。"""
    production_pacing = task_input.get("production_pacing")
    if not isinstance(production_pacing, dict):
        return {}
    pacing_state = production_pacing.get("pacing_state")
    return pacing_state if isinstance(pacing_state, dict) else {}


def _default_event_type(task_input: dict[str, Any]) -> str:
    """根据全书阶段给事件类型兜底。"""
    phase = str(_pacing_state_from_input(task_input).get("phase") or "").lower()
    if phase == "ending":
        return "finale"
    if phase == "final_arc":
        return "final_arc"
    if phase in {"midpoint", "escalation"}:
        return "turning_point"
    return "ordinary"


def _normalize_event_type(value: Any, task_input: dict[str, Any]) -> str:
    allowed = {"ordinary", "turning_point", "final_arc", "finale", "epilogue"}
    event_type = str(value or "").strip().lower()
    if event_type in allowed:
        return event_type
    return _default_event_type(task_input)


def _normalize_narrative_completion(raw: Any, event_type: str) -> dict[str, bool]:
    """标准化事件对整书闭环的声明。"""
    raw = raw if isinstance(raw, dict) else {}
    is_closing = event_type in NARRATIVE_CLOSING_EVENT_TYPES
    return {
        "main_conflict_resolved": bool(raw.get("main_conflict_resolved", is_closing)),
        "protagonist_arc_completed": bool(raw.get("protagonist_arc_completed", is_closing)),
        "key_foreshadowing_resolved": bool(raw.get("key_foreshadowing_resolved", is_closing)),
        "ending_satisfied": bool(raw.get("ending_satisfied", is_closing)),
    }


def _candidate_diversity_report(raw: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        item
        for item in (raw.get("candidate_directions") or [])
        if isinstance(item, dict)
    ]
    signatures = {
        (
            str(item.get("conflict_source") or "").strip(),
            str(item.get("character_choice") or "").strip(),
            str(item.get("turn_or_reframe") or "").strip(),
            str(item.get("cost_or_consequence") or "").strip(),
        )
        for item in candidates
    }
    complete_candidates = sum(
        1
        for signature in signatures
        if sum(bool(value) for value in signature) >= 3
    )
    passed = (
        len(candidates) >= 4
        and len(signatures) >= 4
        and complete_candidates >= 4
    )
    return {
        "status": "passed" if passed else "failed",
        "passed": passed,
        "candidate_count": len(candidates),
        "unique_structure_count": len(signatures),
        "complete_structure_count": complete_candidates,
        "rule": "至少 4 个候选，且冲突来源、人物选择、转折、代价四个维度形成 4 组不同结构",
    }


def _narrative_contract_from_input(
    novel: Novel,
    task_input: dict[str, Any],
) -> dict[str, Any]:
    """Read a per-book genre contract while keeping old StoryBibles compatible."""
    story_bible = task_input.get("story_bible")
    story_bible = story_bible if isinstance(story_bible, dict) else {}
    content = (
        story_bible.get("content")
        if isinstance(story_bible.get("content"), dict)
        else story_bible
    )
    contract = (
        content.get("narrative_contract")
        if isinstance(content.get("narrative_contract"), dict)
        else {}
    )
    positioning = (
        content.get("positioning")
        if isinstance(content.get("positioning"), dict)
        else {}
    )
    main_plot = (
        content.get("main_plot")
        if isinstance(content.get("main_plot"), dict)
        else {}
    )
    brief = novel.brief or {}
    return {
        "primary_genre": str(
            contract.get("primary_genre")
            or positioning.get("genre")
            or novel.genre
            or ""
        ).strip(),
        "primary_reader_promise": str(
            contract.get("primary_reader_promise")
            or positioning.get("reader_expectation")
            or brief.get("reader_expectation")
            or brief.get("selling_points")
            or novel.premise
            or ""
        ).strip(),
        "core_plot_engines": (
            contract.get("core_plot_engines")
            if isinstance(contract.get("core_plot_engines"), list)
            else [
                value
                for value in (
                    main_plot.get("long_term_goal"),
                    main_plot.get("core_conflict"),
                )
                if value
            ]
        ),
        "supporting_element_policy": str(
            contract.get("supporting_element_policy")
            or (
                "职业、技能、身份、设定名词和生活流程默认只作为能力、压力或代价来源；"
                "只有用户明确将其设为主类型或核心卖点时才能连续主导事件。"
            )
        ).strip(),
        "payoff_patterns": (
            contract.get("payoff_patterns")
            if isinstance(contract.get("payoff_patterns"), list)
            else positioning.get("core_selling_points") or []
        ),
        "event_requirements": (
            contract.get("event_requirements")
            if isinstance(contract.get("event_requirements"), list)
            else []
        ),
    }


_AUXILIARY_TOPIC_GROUPS: dict[str, tuple[str, ...]] = {
    "career_technology": (
        "产品经理", "产品思维", "用户需求", "用户反馈", "swot", "算法", "模型",
        "变量", "代码", "接口", "版本", "数据", "方案", "汇报", "职场", "求职",
    ),
    "housing_administration": (
        "租房", "合租", "协议", "条款", "合同", "押金", "门禁", "钥匙", "开锁",
        "物业", "备案", "登记", "核验", "清单", "文书", "手续", "规则确认",
    ),
}


def _auxiliary_dominance_report(
    chapter_plans: list[dict[str, Any]],
    narrative_contract: dict[str, Any],
) -> dict[str, Any]:
    """用可解释的文本门禁拦住连续由职业术语或生活手续主导的计划。"""
    primary_genre = str(narrative_contract.get("primary_genre") or "").lower()
    policy = str(narrative_contract.get("supporting_element_policy") or "").lower()
    policy_is_restrictive = any(
        marker in policy
        for marker in ("辅助", "不是作品主题", "不得连续", "不能连续", "不主导")
    )
    if not policy_is_restrictive:
        return {"passed": True, "violations": [], "chapter_topics": {}}

    chapter_topics: dict[int, list[str]] = {}
    for offset, plan in enumerate(chapter_plans, start=1):
        try:
            chapter_index = int(plan.get("chapter_index") or offset)
        except (TypeError, ValueError):
            chapter_index = offset
        # 只检查真正决定剧情走向的字段。secondary_element_role 与
        # compressed_processes 本来就是“说明该元素仅作辅助/需要压缩”的字段，
        # 把它们计入命中会造成“越强调不主导，越容易被判主导”的反向误判。
        material = " ".join(
            str(plan.get(key) or "")
            for key in (
                "title",
                "function",
                "plot_engine",
                "core_event",
                "state_change",
                "dramatic_turn",
                "reader_payoff",
                "ending_hook",
            )
        ).lower()
        topics: list[str] = []
        for topic, markers in _AUXILIARY_TOPIC_GROUPS.items():
            if topic == "career_technology" and any(
                marker in primary_genre for marker in ("职场", "商战", "科技", "职业")
            ):
                continue
            if topic == "housing_administration" and any(
                marker in primary_genre for marker in ("房产", "租房", "地产")
            ):
                continue
            # 同一词重复出现不等于它主导剧情；至少命中两个不同概念才进入疑点列表。
            distinct_hits = {marker for marker in markers if marker in material}
            if len(distinct_hits) >= 2:
                topics.append(topic)
        chapter_topics[chapter_index] = topics

    violations: list[dict[str, Any]] = []
    for topic in _AUXILIARY_TOPIC_GROUPS:
        run: list[int] = []
        for chapter_index in sorted(chapter_topics):
            if topic in chapter_topics[chapter_index]:
                run.append(chapter_index)
                continue
            if len(run) >= 2:
                violations.append({"topic": topic, "chapter_indexes": run})
            run = []
        if len(run) >= 2:
            violations.append({"topic": topic, "chapter_indexes": run})
    return {
        "passed": not violations,
        "violations": violations,
        "chapter_topics": chapter_topics,
    }


def _event_plan_quality_report(
    raw: dict[str, Any],
    narrative_contract: dict[str, Any],
) -> dict[str, Any]:
    """Reject plans that are busy but fail to serve the book's reader promise."""
    candidates = [
        item
        for item in (raw.get("candidate_directions") or [])
        if isinstance(item, dict)
    ]
    candidate_required = (
        "plot_engine",
        "primary_promise_served",
        "secondary_element_role",
        "dramatic_escalation",
        "reader_payoff",
    )
    complete_candidates = sum(
        1
        for item in candidates
        if all(str(item.get(key) or "").strip() for key in candidate_required)
    )
    plot_engines = {
        str(item.get("plot_engine") or "").strip()
        for item in candidates
        if str(item.get("plot_engine") or "").strip()
    }
    chapter_plans = [
        item
        for item in (raw.get("chapter_plans") or [])
        if isinstance(item, dict)
    ]
    complete_chapters = sum(
        1
        for item in chapter_plans
        if str(item.get("plot_engine") or "").strip()
        and str(item.get("dramatic_turn") or "").strip()
        and str(item.get("reader_payoff") or "").strip()
    )
    complete_scene_executions = sum(
        1
        for item in chapter_plans
        if scene_execution_is_complete(item.get("scene_execution"))
    )
    event_fields_complete = all(
        str(raw.get(key) or "").strip()
        for key in (
            "genre_alignment",
            "dramatic_escalation",
            "major_reversal",
            "reader_payoff",
        )
    )
    auxiliary_dominance = _auxiliary_dominance_report(
        chapter_plans,
        narrative_contract,
    )
    comedy_required = any(
        marker in (
            str(narrative_contract.get("primary_genre") or "")
            + str(narrative_contract.get("primary_reader_promise") or "")
        )
        for marker in ("喜剧", "搞笑", "幽默")
    )
    comedy_delivery_passed = (
        not comedy_required
        or all(
            isinstance(item.get("comedy_beats"), list)
            and len(item["comedy_beats"]) >= 4
            for item in chapter_plans
        )
    )
    first_plan = chapter_plans[0] if chapter_plans else {}
    first_chapter_hook_passed = not first_plan or not (
        int(first_plan.get("chapter_index") or 0) == 1
        and str(first_plan.get("function") or "").strip() in {"铺垫", "背景", "介绍"}
    )
    passed = (
        len(candidates) >= 4
        and complete_candidates >= 4
        and len(plot_engines) >= 3
        and event_fields_complete
        and bool(chapter_plans)
        and complete_chapters == len(chapter_plans)
        and complete_scene_executions == len(chapter_plans)
        and auxiliary_dominance["passed"]
        and comedy_delivery_passed
        and first_chapter_hook_passed
    )
    return {
        "status": "passed" if passed else "failed",
        "passed": passed,
        "primary_genre": narrative_contract.get("primary_genre", ""),
        "primary_reader_promise": narrative_contract.get(
            "primary_reader_promise",
            "",
        ),
        "candidate_count": len(candidates),
        "complete_candidate_count": complete_candidates,
        "unique_plot_engine_count": len(plot_engines),
        "chapter_count": len(chapter_plans),
        "complete_chapter_count": complete_chapters,
        "complete_scene_execution_count": complete_scene_executions,
        "event_fields_complete": event_fields_complete,
        "auxiliary_dominance": auxiliary_dominance,
        "comedy_delivery_passed": comedy_delivery_passed,
        "first_chapter_hook_passed": first_chapter_hook_passed,
        "rule": (
            "候选须使用至少3种剧情驱动力，并说明主类型承诺、升级和回报；"
            "事件与每章须有明确转折和读者回报；辅助元素不得连续主导；"
            "每章须有两轮以上的场景攻防、对白压力与可承接的章末余力；"
            "喜剧作品每章至少规划4个因果型喜剧节拍；首章不得只是铺垫。"
        ),
    }


def _quality_passes_without_auxiliary(report: dict[str, Any]) -> bool:
    """判断除辅助元素疑点外的结构、类型与场景门禁是否全部通过。"""
    chapter_count = int(report.get("chapter_count") or 0)
    return bool(
        int(report.get("candidate_count") or 0) >= 4
        and int(report.get("complete_candidate_count") or 0) >= 4
        and int(report.get("unique_plot_engine_count") or 0) >= 3
        and report.get("event_fields_complete") is True
        and chapter_count > 0
        and int(report.get("complete_chapter_count") or 0) == chapter_count
        and int(report.get("complete_scene_execution_count") or 0) == chapter_count
        and report.get("comedy_delivery_passed") is True
        and report.get("first_chapter_hook_passed") is True
    )


def _auxiliary_suspect_indexes(report: dict[str, Any]) -> list[int]:
    indexes: set[int] = set()
    auxiliary = report.get("auxiliary_dominance") or {}
    for violation in auxiliary.get("violations") or []:
        if not isinstance(violation, dict):
            continue
        for value in violation.get("chapter_indexes") or []:
            try:
                indexes.add(int(value))
            except (TypeError, ValueError):
                continue
    return sorted(indexes)


def _chapter_index(value: Any) -> int:
    """宽容解析模型返回的章节编号，非法值统一视为 0。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _build_auxiliary_semantic_review_prompt(
    event_plan: dict[str, Any],
    quality_report: dict[str, Any],
    narrative_contract: dict[str, Any],
) -> list[dict[str, str]]:
    """让审校模型判断关键词疑点是否真的成为章节主要剧情驱动力。"""
    suspect_indexes = set(_auxiliary_suspect_indexes(quality_report))
    chapter_plans = [
        item
        for item in (event_plan.get("chapter_plans") or [])
        if isinstance(item, dict)
        and _chapter_index(item.get("chapter_index")) in suspect_indexes
    ]
    payload = {
        "narrative_contract": narrative_contract,
        "event": {
            key: event_plan.get(key)
            for key in (
                "event_title",
                "event_goal",
                "core_conflict",
                "genre_alignment",
                "dramatic_escalation",
                "major_reversal",
                "reader_payoff",
            )
        },
        "keyword_suspicions": (
            (quality_report.get("auxiliary_dominance") or {}).get("violations") or []
        ),
        "suspect_chapter_plans": chapter_plans,
        "expected_output": {
            "dominant_chapter_indexes": ["真正由辅助元素主导的章节编号；没有则为空数组"],
            "assessments": [
                {
                    "chapter_index": "章节编号",
                    "dominant": "true|false",
                    "primary_plot_driver": "本章真正推动局势变化的力量",
                    "reason": "为什么属于主导或只是背景/工具",
                }
            ],
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "你是严格的剧情主导性审校器，只输出 JSON。关键词命中只是疑点，不是结论。"
                "逐章判断：若删除职业、技术、租房手续或物业规则后，本章主要冲突、人物选择、"
                "转折和读者回报仍然成立，则该元素只是背景或工具，dominant=false；"
                "若主要篇幅和局势变化依赖办理、核验、条款讨论、职业方案或技术流程本身，"
                "则 dominant=true。不得因为字段提到‘需要压缩流程’就判定主导，也不得为了放行而宽松判断。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def _semantic_auxiliary_targets(
    semantic_review: dict[str, Any],
    quality_report: dict[str, Any],
) -> list[int]:
    """把语义复核结论限制在关键词初筛范围内，并扩展到对应连续疑点组。"""
    suspect_indexes = set(_auxiliary_suspect_indexes(quality_report))
    dominant_indexes: set[int] = set()
    for value in semantic_review.get("dominant_chapter_indexes") or []:
        try:
            chapter_index = int(value)
        except (TypeError, ValueError):
            continue
        if chapter_index in suspect_indexes:
            dominant_indexes.add(chapter_index)
    if not dominant_indexes:
        return []

    expanded = set(dominant_indexes)
    auxiliary = quality_report.get("auxiliary_dominance") or {}
    for violation in auxiliary.get("violations") or []:
        if not isinstance(violation, dict):
            continue
        group = {
            int(value)
            for value in (violation.get("chapter_indexes") or [])
            if str(value).isdigit()
        }
        if group & dominant_indexes:
            expanded.update(group)
    return sorted(expanded & suspect_indexes)


def _mark_auxiliary_semantically_passed(
    quality_report: dict[str, Any],
    semantic_review: dict[str, Any],
) -> dict[str, Any]:
    """记录语义复核通过，并重新计算完整质量门禁。"""
    auxiliary = {
        **(quality_report.get("auxiliary_dominance") or {}),
        "passed": True,
        "semantic_review": semantic_review,
        "keyword_violations": (
            (quality_report.get("auxiliary_dominance") or {}).get("violations") or []
        ),
        "violations": [],
    }
    updated = {**quality_report, "auxiliary_dominance": auxiliary}
    updated["passed"] = _quality_passes_without_auxiliary(updated)
    updated["status"] = "passed" if updated["passed"] else "failed"
    return updated


def _build_auxiliary_repair_prompt(
    event_plan: dict[str, Any],
    quality_report: dict[str, Any],
    semantic_review: dict[str, Any],
    target_indexes: list[int],
    narrative_contract: dict[str, Any],
    repair_round: int,
) -> list[dict[str, str]]:
    """只要求模型替换真正有问题的章节计划，禁止重写整个事件。"""
    target_set = set(target_indexes)
    all_plans = [
        item for item in (event_plan.get("chapter_plans") or []) if isinstance(item, dict)
    ]
    payload = {
        "repair_round": repair_round,
        "narrative_contract": narrative_contract,
        "event": {
            key: event_plan.get(key)
            for key in (
                "event_title",
                "event_goal",
                "core_conflict",
                "genre_alignment",
                "dramatic_escalation",
                "major_reversal",
                "reader_payoff",
            )
        },
        "semantic_review": semantic_review,
        "violations": (quality_report.get("auxiliary_dominance") or {}).get("violations") or [],
        "target_chapter_indexes": target_indexes,
        "target_chapter_plans": [
            item
            for item in all_plans
            if _chapter_index(item.get("chapter_index")) in target_set
        ],
        "continuity_context": [
            {
                "chapter_index": item.get("chapter_index"),
                "core_event": item.get("core_event"),
                "state_change": item.get("state_change"),
                "dramatic_turn": item.get("dramatic_turn"),
                "ending_hook": item.get("ending_hook"),
            }
            for item in all_plans
        ],
        "expected_output": {
            "chapter_plan_replacements": [
                "为每个 target_chapter_indexes 返回一份字段完整的章节计划对象"
            ]
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 NovelForge 剧情计划局部修复器，只输出 JSON。只替换指定章节，禁止重写事件总纲、"
                "候选方向或其他章节。新章节必须保留前后状态连续性，但更换真正的 plot_engine："
                "让人物目标冲突、误解、关系攻防、信息反转、限时选择或主动承担后果推动剧情。"
                "租房、物业、手续、职业或技术只能一笔带过并作为压力/工具，不能成为主要讨论对象、"
                "解决目标、对白中心或读者回报。每个替换章节仍须保留完整 scene_execution、"
                "4—6个因果型喜剧节拍、明确 dramatic_turn、state_change、reader_payoff 和 ending_hook。"
                "后续修复轮次必须换用不同于上一版的剧情驱动力，不能只替换措辞来规避关键词。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def _merge_chapter_plan_replacements(
    event_plan: dict[str, Any],
    replacements_payload: dict[str, Any],
    target_indexes: list[int],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """校验并合并局部章节替换；任何目标缺失时保持原计划不变。"""
    target_set = set(target_indexes)
    original_plans = {
        _chapter_index(item.get("chapter_index")): item
        for item in (event_plan.get("chapter_plans") or [])
        if isinstance(item, dict)
    }
    replacements: dict[int, dict[str, Any]] = {}
    invalid_indexes: list[int] = []
    unchanged_plot_engine_indexes: list[int] = []
    for item in replacements_payload.get("chapter_plan_replacements") or []:
        if not isinstance(item, dict):
            continue
        try:
            chapter_index = int(item.get("chapter_index") or 0)
        except (TypeError, ValueError):
            continue
        if chapter_index not in target_set:
            continue
        old_plot_engine = str(
            (original_plans.get(chapter_index) or {}).get("plot_engine") or ""
        ).strip().casefold()
        new_plot_engine = str(item.get("plot_engine") or "").strip().casefold()
        if old_plot_engine and new_plot_engine == old_plot_engine:
            unchanged_plot_engine_indexes.append(chapter_index)
            continue
        complete = bool(
            str(item.get("core_event") or "").strip()
            and str(item.get("plot_engine") or "").strip()
            and str(item.get("state_change") or "").strip()
            and str(item.get("dramatic_turn") or "").strip()
            and str(item.get("reader_payoff") or "").strip()
            and str(item.get("ending_hook") or "").strip()
            and isinstance(item.get("comedy_beats"), list)
            and len(item.get("comedy_beats") or []) >= 4
            and scene_execution_is_complete(item.get("scene_execution"))
        )
        if not complete:
            invalid_indexes.append(chapter_index)
            continue
        replacements[chapter_index] = item

    missing_indexes = sorted(target_set - set(replacements))
    validation = {
        "target_indexes": sorted(target_set),
        "replaced_indexes": sorted(replacements),
        "missing_indexes": missing_indexes,
        "invalid_indexes": sorted(set(invalid_indexes)),
        "unchanged_plot_engine_indexes": sorted(set(unchanged_plot_engine_indexes)),
        "passed": not missing_indexes,
    }
    if missing_indexes:
        return event_plan, validation

    merged_plans = []
    for plan in event_plan.get("chapter_plans") or []:
        if not isinstance(plan, dict):
            continue
        chapter_index = _chapter_index(plan.get("chapter_index"))
        merged_plans.append(
            {**plan, **replacements[chapter_index], "chapter_index": chapter_index}
            if chapter_index in replacements
            else plan
        )
    return {**event_plan, "chapter_plans": merged_plans}, validation


def _build_scene_orchestration_prompt(
    event_plan: dict[str, Any],
    narrative_contract: dict[str, Any],
) -> list[dict[str, str]]:
    """把宏观章节目的地再编排成可直接写作的视角、对白与章际接力。"""
    payload = {
        "narrative_contract": narrative_contract,
        "event": {
            key: event_plan.get(key)
            for key in (
                "event_title",
                "event_goal",
                "core_conflict",
                "genre_alignment",
                "dramatic_escalation",
                "major_reversal",
                "reader_payoff",
            )
        },
        "chapter_plans": event_plan.get("chapter_plans") or [],
        "expected_output": {
            "chapter_scene_directions": [
                {
                    "chapter_index": "章节编号",
                    "scene_execution": build_scene_execution_schema(),
                    "interaction_contexts": [
                        {
                            "characters": ["核心对手戏人物"],
                            "relationship": "当前真实关系与距离",
                            "emotion": "双方不同的即时情绪",
                            "speech_goal": "表面语言目的与不能直说的真实目的",
                        }
                    ],
                    "character_beats": ["用动作或选择显露的人物变化"],
                    "comedy_beats": [
                        "喜剧作品填写4—6个：现实铺垫→人物逻辑偏移→对方反应→局面后果/回调"
                    ],
                    "compressed_processes": ["不值得展开、只交代结果的说明或流程"],
                }
            ]
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 NovelForge 章节场面编排 Agent，只输出 JSON。宏观事件、章节顺序、core_event、"
                "state_change、dramatic_turn、reader_payoff 和 ending_hook 已经确定，禁止改写。"
                "你的任务是让每章像连载小说现场而不是提纲：一章围绕一个正在发生的问题和一组核心对手戏；"
                "说明只在人物眼下需要时给最少信息。逐章补全 scene_execution，尤其是"
                "‘可观察细节→带私心的误读→即时冲动→可见反应’和"
                "‘话/动作刺激→按性格回避或抓错重点→对方接招→局面变化’。"
                "对白只规划功能，不得预写可粘贴台词。关系变化用称呼、距离、视线、步速、物品、等待或"
                "欲言又止等微动作显露，不由旁白宣布。喜剧优先人物自利解释、字面误读、一本正经补救、"
                "回旋镖和身份反转；每个笑点必须有对方反应与后果，热梗不计数。"
                "上一章 ending_residual_force.next_chapter_first_beat 必须成为下一章 entry_pressure 的直接承接；"
                "禁止章末总结后下一章另起炉灶。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def _merge_scene_orchestration(
    event_plan: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """只合并场面执行字段；宏观剧情字段始终以原规划为准。"""
    plans = [
        item
        for item in (event_plan.get("chapter_plans") or [])
        if isinstance(item, dict)
    ]
    target_indexes = {
        _chapter_index(item.get("chapter_index"))
        for item in plans
        if _chapter_index(item.get("chapter_index")) > 0
    }
    directions: dict[int, dict[str, Any]] = {}
    invalid_indexes: list[int] = []
    for item in payload.get("chapter_scene_directions") or []:
        if not isinstance(item, dict):
            continue
        chapter_index = _chapter_index(item.get("chapter_index"))
        if chapter_index not in target_indexes:
            continue
        if not scene_execution_is_complete(item.get("scene_execution")):
            invalid_indexes.append(chapter_index)
            continue
        directions[chapter_index] = item

    merged_plans: list[dict[str, Any]] = []
    complete_indexes: list[int] = []
    for plan in plans:
        chapter_index = _chapter_index(plan.get("chapter_index"))
        direction = directions.get(chapter_index)
        merged = dict(plan)
        if direction:
            merged["scene_execution"] = normalize_scene_execution(
                direction.get("scene_execution")
            )
            for key in (
                "interaction_contexts",
                "character_beats",
                "comedy_beats",
                "compressed_processes",
            ):
                if isinstance(direction.get(key), list):
                    merged[key] = direction[key]
        if scene_execution_is_complete(merged.get("scene_execution")):
            complete_indexes.append(chapter_index)
        merged_plans.append(merged)

    missing_indexes = sorted(target_indexes - set(complete_indexes))
    validation = {
        "target_indexes": sorted(target_indexes),
        "refined_indexes": sorted(directions),
        "retained_indexes": sorted(target_indexes - set(directions)),
        "invalid_indexes": sorted(set(invalid_indexes)),
        "missing_indexes": missing_indexes,
        "passed": not missing_indexes,
    }
    return {**event_plan, "chapter_plans": merged_plans}, validation


def _compact_plot_experience(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    return {
        "title": str(value.get("title") or "")[:160],
        "setup": str(value.get("setup") or "")[:500],
        "trigger": str(value.get("trigger") or "")[:500],
        "character_desire": str(value.get("character_desire") or "")[:500],
        "conflict_and_escalation": str(
            value.get("conflict_and_escalation") or ""
        )[:800],
        "character_choice": str(value.get("character_choice") or "")[:500],
        "turn_or_reframe": str(value.get("turn_or_reframe") or "")[:500],
        "payoff": str(value.get("payoff") or "")[:500],
        "consequence": str(value.get("consequence") or "")[:500],
        "why_effective": str(value.get("why_effective") or "")[:700],
        "transferable_pattern": str(
            value.get("transferable_pattern") or ""
        )[:800],
        "applicable_genres": [
            str(item)[:120]
            for item in (value.get("applicable_genres") or [])[:8]
        ],
        "applicable_scenes": [
            str(item)[:120]
            for item in (value.get("applicable_scenes") or [])[:8]
        ],
    }


def _build_event_plan_prompt(novel: Novel, task_input: dict, chapter_count: int, start_index: int) -> list[dict[str, str]]:
    """构建事件规划 Prompt，只下发一份作品事实和当前阶段。"""
    story_bible = compact_story_bible(task_input.get("story_bible"))
    story_content = story_bible.get("content") or {}
    main_plot = story_content.get("main_plot") or {}
    plot_reference_pack = task_input.get("plot_reference_pack") or {}
    pacing_state = _pacing_state_from_input(task_input)
    production_pacing = task_input.get("production_pacing") or {}
    pacing_plan = production_pacing.get("pacing_plan") or {}
    current_phase = next(
        (
            phase
            for phase in (pacing_plan.get("global_phases") or [])
            if isinstance(phase, dict)
            and (
                phase.get("phase") == pacing_state.get("phase")
                or phase.get("label") == pacing_state.get("phase_label")
            )
        ),
        {},
    )
    compact_pacing_state = {
        key: pacing_state.get(key)
        for key in (
            "phase",
            "phase_label",
            "phase_goal",
            "event_policy",
            "progress_percent",
            "remaining_words",
            "is_final_phase",
            "allow_new_major_hooks",
            "requires_narrative_closure",
        )
        if pacing_state.get(key) not in (None, "", [], {})
    }
    user_request = {
        key: value
        for key, value in task_input.items()
        if key not in {"plot_reference_pack", "story_bible", "production_pacing"}
    }
    payload = {
        "task": {
            "type": "plan_story_event",
            "chapter_count": chapter_count,
            "start_chapter_index": start_index,
            "user_request": user_request,
            "production_pacing": {
                "pacing_state": compact_pacing_state,
                "current_phase": {
                    key: current_phase.get(key)
                    for key in (
                        "phase",
                        "label",
                        "narrative_goal",
                        "event_policy",
                        "key_milestones",
                    )
                    if current_phase.get(key) not in (None, "", [], {})
                },
            },
        },
        "novel": {
            "title": novel.title,
            "genre": novel.genre,
            "current_chapter_index": novel.current_chapter_index,
            **(
                {"premise": novel.premise}
                if not main_plot.get("premise")
                else {}
            ),
        },
        "story_contract": story_bible,
        "planning_order": {
            "requested_chapter_range": [
                start_index,
                start_index + chapter_count - 1,
            ],
            "ordered_planned_events": (
                main_plot.get("planned_events")
                if isinstance(main_plot.get("planned_events"), list)
                else []
            ),
            "rule": "从当前真实状态继续，按顺序覆盖本范围内最早未完成的节点；相邻节点可组成一个连续剧情单元，但不得跳过前置节点。",
        },
        "plot_design_reference_pack": {
            "usage_policy": {
                "goal": "使用结构化剧情经验扩展人物欲望、主动选择、升级、转折、代价和回报",
                "forbidden": "复制人物、专名、具体事件组合、独特道具、原句或结局",
            },
            "references": [
                {
                    "passage_id": item.get("passage_id", ""),
                    "excerpt": item.get("excerpt", ""),
                    "mechanism": item.get("mechanism") or {},
                    "experience": _compact_plot_experience(
                        item.get("experience")
                    ),
                    "technique": item.get("technique", ""),
                }
                for item in (plot_reference_pack.get("references") or [])[:4]
            ],
        },
        "expected_output": {
            "event_type": "ordinary|turning_point|final_arc|finale|epilogue",
            "event_title": "完整剧情事件标题",
            "event_goal": "这个事件结束时必须完成的剧情结果",
            "core_conflict": "事件核心冲突",
            "genre_alignment": "本事件如何兑现本书主类型与核心卖点，而非被人物职业或设定名词带偏",
            "dramatic_escalation": "局势如何从普通问题升级为必须选择并承担后果的问题",
            "major_reversal": "改变读者和人物判断的关键反转",
            "reader_payoff": "本事件最终交付的类型化回报",
            "key_characters": ["主要人物"],
            "supporting_character_roles": [
                {
                    "name": "本事件启用的配角，通常1—2名",
                    "agenda": "他主动参与是为了得到什么",
                    "entry_action": "他通过什么行动进入事件",
                    "story_impact": "他的介入怎样改变局势，而非只递消息或助攻",
                }
            ],
            "completion_criteria": ["闭环标准"],
            "next_event_hook": "事件收束后引出的下一个事件钩子",
            "narrative_completion": {
                "main_conflict_resolved": False,
                "protagonist_arc_completed": False,
                "key_foreshadowing_resolved": False,
                "ending_satisfied": False,
            },
            "candidate_directions": [
                {
                    "direction": "候选剧情方向",
                    "plot_engine": "该方向使用的核心剧情驱动力",
                    "primary_promise_served": "它具体兑现哪项主类型承诺",
                    "secondary_element_role": "职业、技能、身份或设定元素只承担何种辅助功能；若确为核心须说明依据",
                    "conflict_source": "冲突从谁的主动行为或哪种制度/关系中产生",
                    "character_choice": "主角必须作出的主动选择",
                    "dramatic_escalation": "局势升级方式",
                    "turn_or_reframe": "使剧情换轨而非简单升级的变化",
                    "cost_or_consequence": "选择付出的代价及后续影响",
                    "reader_payoff": "该方向准备交付的类型化回报",
                    "difference_from_other_candidates": "该方向为何不是其它候选的换皮",
                }
            ],
            "selection_rationale": "为什么最终选择的事件不是最直觉、最套路的方向",
            "adapted_plot_mechanisms": ["只写迁移的抽象机制，不写样本人物和事件"],
            "chapter_plans": [
                {
                    "chapter_index": start_index,
                    "title": "章节标题",
                    "function": "铺垫|冲突|升级|反转|收束",
                    "plot_engine": "本章实际使用的剧情驱动力",
                    "secondary_element_role": "职业、技能、身份或设定元素在本章只承担的辅助作用",
                    "story_time": "本章在故事内发生的日期、时段或相对时间",
                    "elapsed_time": "相对上一章经过了多久",
                    "core_event": "本章核心事件",
                    "state_change": "本章结束时相较开头发生的明确变化",
                    "dramatic_turn": "本章局势升级、判断改变或关系换轨的具体节点",
                    "reader_payoff": "本章给读者的即时回报，不能只是流程完成",
                    "scene_execution": build_scene_execution_schema(),
                    "participants": ["本章实际出场并参与核心互动的人物"],
                    "interaction_contexts": [
                        {
                            "characters": ["发生互动的双方或多人"],
                            "relationship": "当下真实关系与熟悉程度",
                            "emotion": "互动时的具体情绪及强度",
                            "speech_goal": "这次对话要完成的试探、催答、拒绝、安慰、调侃等语言目的",
                        }
                    ],
                    "character_beats": ["人物关系或心理推进"],
                    "comedy_beats": ["喜剧作品填写4—6个分散在章内、由人物行动触发且带回应或后果的喜剧节点；网络热梗不计入数量"],
                    "compressed_processes": ["应一句带过、不得展开成流程的手续或日常事项"],
                    "foreshadowing_actions": ["伏笔埋设/推进/回收"],
                    "ending_hook": "章末钩子",
                }
            ],
        },
    }
    return [
        {
            "role": "system",
            "content": "\n".join(
                [
                    "你是 NovelForge 多章剧情规划 Agent；规划一个有阶段结果的连续剧情单元，只输出 JSON。",
                    "唯一作品事实源是 story_contract；novel 只提供基础元数据。不得自行补写已经发生但正文尚未生成的前史。",
                    "先服从 planning_order：从当前真实状态开始，按 ordered_planned_events 的顺序覆盖本批次最早未完成节点。相邻小事件可合并为同一剧情单元，不得为了维持单一事件而跳过前置节点。",
                    "先给至少 4 个真正不同的 candidate_directions；差异必须来自冲突发起者、人物选择、信息结构、转折和代价，不能只换地点/道具。淘汰最直觉方向后再选择。",
                    "服从 story_contract.content.narrative_contract：先判断主类型承诺和核心剧情驱动力，再使用职业、技能、身份或世界设定；辅助元素只能制造能力、压力、代价或关键翻盘。",
                    "候选方向至少覆盖3种真正不同的 plot_engine；每个候选必须写清 primary_promise_served、dramatic_escalation 和 reader_payoff。爽点按本书类型定义，可为胜利、揭示、突破、关系确认、资源获得、反击或情绪释放，不得写死为某一种题材模板。",
                    "事件和每章都必须有可感知的 dramatic_turn 与 reader_payoff；会议结束、手续完成、方案通过、修炼结束或调查结束本身都不是回报，除非它造成局势反转、目标进展或人物状态变化。",
                    "plot_design_reference_pack 只迁移“触发—阻力—选择—转折—代价”机制；禁止拼接窗口或复制人物、关系、事件链、专名、道具、原句和结局。",
                    "每章 core_event 写清触发、阻力、决定、后果；story_time/elapsed_time 必须从上一章真实结尾连续推进，不能引用尚未生成的章节作为前史。",
                    "大纲只规定本章目的地，不等于正文讲解顺序。一章优先围绕一个正在发生的问题和一组核心对手戏推进，背景只在当前动作需要时露出。每章 scene_execution 必须写出开场压力、双方即时目标、至少两轮策略—反制—局部变化，以及能被下一章第一拍接住的章末余力。",
                    "每章必须规划 pov_reaction_chain：可观察细节→带人物私心的误读→即时冲动→可见反应；不得由旁白直接宣布标准情绪。还必须规划 dialogue_reaction_chain：刺激→回避/抓错重点→对方接招→局面变化。只描述功能，不预写可粘贴台词。",
                    "对白规划必须有隐藏意图和反应链。人物可以回避、装没听懂、答非所问、嘴硬或说半句，但对方必须用动作、追问、沉默或反击接住；至少一轮交流改变信息、立场、关系或行动，禁止标准问答和轮流说明。",
                    "每章必须产生新的 state_change；不得把一次租房、采购、登记、面试或规则协商拆成多章逐项确认。",
                    "职业和技术能力可以在必要处解决问题，但职业术语不得成为人物的固定口癖、感情比喻或连续笑点；若作品契约将其定义为辅助元素，连续两章由同一职业/技术/手续话题主导即为不合格。",
                    "若主类型包含喜剧，每章规划4—6个分散的因果型喜剧节拍：优先用人物自利解释、抓错重点、过度字面理解、一本正经补救、前文回旋镖或身份反转，并产生回应、升级或关系后果；网络热梗不算喜剧节拍，也不能代替原创笑点。",
                    "喜剧可按需使用网络语义的‘抽象’：答非所问、错位联想、一本正经跑偏、因果倒置、概念偷换或过度字面理解。每次只做一次清楚的逻辑偏移，必须来自人物性格、误判、自尊或即时困境，并由他人反应和现实后果接住；禁止随机胡言乱语或全员同频发疯。",
                    "第1章禁止把功能写成单纯‘铺垫/背景介绍’：前10%发生具体扰动，前25%出现一次计划受阻或判断反转，背景只随行动露出；结尾钩子必须立刻改变下一步行动，喜剧/关系作品应尽早让核心对手戏人物以出场、声音或直接行动进入。",
                    "配角必须带着自己的目标主动介入；单个普通事件通常只启用1—2名核心配角，禁止把配角写成随叫随到的工具人或集体助攻团。",
                    "遵守 story_contract 中的人物与世界事实；科技、职业、制度和生活细节符合年代。重大关系推进须有现实动机和安全缓冲。",
                    "ending_hook 必须是具体信息、未完动作、半句话、危险、选择或关系变化，形成追读问题；ending_residual_force.next_chapter_first_beat 必须先兑现这一拍，再允许换场或补背景，禁止章末总结后下一章另起炉灶。",
                    "服从 production_pacing：final_arc/ending 不开大型支线，优先闭合主线、人物弧光和伏笔；finale/epilogue 可无 next_event_hook，否则只能留番外/续作钩子。",
                ]
            ),
        },
        {
            "role": "user",
            "content": "\n".join(
                [
                    f"请为《{novel.title}》规划接下来 {chapter_count} 章的闭环剧情事件。",
                    "先按既定节点确定本批次起点，再发散实现方向并生成 event_plan/chapter_plans；剧情单元须有起因、升级、转折和阶段结果。",
                    f"当前全书阶段：{pacing_state.get('phase_label') or pacing_state.get('phase') or '未指定'}；"
                    f"当前字数进度：{pacing_state.get('progress_percent', 0)}%；"
                    f"剩余目标字数：{pacing_state.get('remaining_words', 0)}。",
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ]
            ),
        },
    ]


def _normalize_event_plan(
    raw: dict[str, Any],
    novel: Novel,
    chapter_count: int,
    start_index: int,
    task_input: dict[str, Any],
) -> dict[str, Any]:
    """清洗事件规划，保证后续节点拿到稳定字段。"""
    brief = novel.brief or {}
    event_title = _derive_event_title(raw, novel, start_index, chapter_count)
    chapter_plans = raw.get("chapter_plans")
    if not isinstance(chapter_plans, list):
        chapter_plans = []

    event_type = _normalize_event_type(raw.get("event_type"), task_input)
    narrative_completion = _normalize_narrative_completion(raw.get("narrative_completion"), event_type)
    normalized_plans = []
    for offset in range(chapter_count):
        source = chapter_plans[offset] if offset < len(chapter_plans) and isinstance(chapter_plans[offset], dict) else {}
        chapter_index = start_index + offset
        normalized_plans.append(
            {
                "chapter_index": chapter_index,
                "title": _compact_text(source.get("title"), 120, f"第 {chapter_index} 章"),
                "function": _compact_text(source.get("function"), 50, "收束" if offset == chapter_count - 1 else "推进"),
                "plot_engine": _compact_text(source.get("plot_engine"), 120),
                "secondary_element_role": _compact_text(
                    source.get("secondary_element_role"),
                    180,
                ),
                "story_time": _compact_text(source.get("story_time"), 120, "时间待正文确认"),
                "elapsed_time": _compact_text(source.get("elapsed_time"), 120, "承接上一章"),
                "core_event": str(source.get("core_event") or f"推进事件“{event_title}”").strip(),
                "state_change": str(source.get("state_change") or "").strip(),
                "dramatic_turn": str(source.get("dramatic_turn") or "").strip(),
                "reader_payoff": str(source.get("reader_payoff") or "").strip(),
                "scene_execution": normalize_scene_execution(source.get("scene_execution")),
                "participants": [
                    str(item).strip()
                    for item in (source.get("participants") or [])[:8]
                    if str(item).strip()
                ],
                "interaction_contexts": [
                    {
                        "characters": [
                            str(name).strip()
                            for name in (item.get("characters") or [])[:5]
                            if str(name).strip()
                        ],
                        "relationship": _compact_text(
                            item.get("relationship"),
                            120,
                        ),
                        "emotion": _compact_text(item.get("emotion"), 100),
                        "speech_goal": _compact_text(
                            item.get("speech_goal"),
                            140,
                        ),
                    }
                    for item in (source.get("interaction_contexts") or [])[:4]
                    if isinstance(item, dict)
                ],
                "character_beats": source.get("character_beats") if isinstance(source.get("character_beats"), list) else [],
                "comedy_beats": source.get("comedy_beats") if isinstance(source.get("comedy_beats"), list) else [],
                "compressed_processes": (
                    source.get("compressed_processes")
                    if isinstance(source.get("compressed_processes"), list)
                    else []
                ),
                "foreshadowing_actions": (
                    source.get("foreshadowing_actions") if isinstance(source.get("foreshadowing_actions"), list) else []
                ),
                "ending_hook": str(source.get("ending_hook") or "").strip(),
            }
        )

    next_event_hook = str(raw.get("next_event_hook") or "").strip()
    if event_type in NARRATIVE_CLOSING_EVENT_TYPES and not next_event_hook:
        next_event_hook = ""

    candidate_directions = []
    for item in (raw.get("candidate_directions") or [])[:6]:
        if not isinstance(item, dict):
            continue
        candidate_directions.append(
            {
                key: str(item.get(key) or "").strip()
                for key in (
                    "direction",
                    "plot_engine",
                    "primary_promise_served",
                    "secondary_element_role",
                    "conflict_source",
                    "character_choice",
                    "dramatic_escalation",
                    "turn_or_reframe",
                    "cost_or_consequence",
                    "reader_payoff",
                    "difference_from_other_candidates",
                )
            }
        )

    supporting_character_roles = []
    for item in (raw.get("supporting_character_roles") or [])[:3]:
        if not isinstance(item, dict):
            continue
        role = {
            key: str(item.get(key) or "").strip()
            for key in ("name", "agenda", "entry_action", "story_impact")
        }
        if role["name"]:
            supporting_character_roles.append(role)

    return {
        "event_type": event_type,
        "event_title": event_title,
        "event_goal": str(raw.get("event_goal") or brief.get("selling_points") or "完成阶段性剧情推进").strip(),
        "core_conflict": str(raw.get("core_conflict") or brief.get("conflict") or "主角面临新的选择和压力").strip(),
        "genre_alignment": str(raw.get("genre_alignment") or "").strip(),
        "dramatic_escalation": str(raw.get("dramatic_escalation") or "").strip(),
        "major_reversal": str(raw.get("major_reversal") or "").strip(),
        "reader_payoff": str(raw.get("reader_payoff") or "").strip(),
        "key_characters": raw.get("key_characters") if isinstance(raw.get("key_characters"), list) else [],
        "supporting_character_roles": supporting_character_roles,
        "completion_criteria": raw.get("completion_criteria") if isinstance(raw.get("completion_criteria"), list) else [],
        "next_event_hook": next_event_hook,
        "narrative_completion": narrative_completion,
        "candidate_directions": candidate_directions,
        "selection_rationale": str(raw.get("selection_rationale") or "").strip(),
        "narrative_contract": _narrative_contract_from_input(novel, task_input),
        "adapted_plot_mechanisms": [
            str(item).strip()
            for item in (raw.get("adapted_plot_mechanisms") or [])[:8]
            if str(item).strip()
        ],
        "planning_diversity": _candidate_diversity_report(raw),
        "planning_quality": _event_plan_quality_report(
            raw,
            _narrative_contract_from_input(novel, task_input),
        ),
        "chapter_plans": normalized_plans,
    }


def _build_simulated_event_plan(novel: Novel, task_input: dict, chapter_count: int, start_index: int) -> dict[str, Any]:
    """未配置 LLM 时提供可测试的事件规划。"""
    brief = novel.brief or {}
    event_type = _default_event_type(task_input)
    is_closing = event_type in NARRATIVE_CLOSING_EVENT_TYPES
    contract = _narrative_contract_from_input(novel, task_input)
    primary_promise = (
        contract.get("primary_reader_promise")
        or "兑现本书主类型的阶段承诺"
    )
    generic_engines = [
        "人物目标冲突",
        "关系与立场变化",
        "信息揭示与判断反转",
        "资源代价与限时选择",
    ]
    chapter_plans = []
    for offset in range(chapter_count):
        chapter_index = start_index + offset
        engine = generic_engines[offset % len(generic_engines)]
        chapter_plans.append(
            {
                "chapter_index": chapter_index,
                "title": f"第 {chapter_index} 章",
                "function": "收束" if offset == chapter_count - 1 else "升级",
                "plot_engine": engine,
                "secondary_element_role": "辅助制造压力或提供行动手段，不主导剧情",
                "story_time": "承接上一章的连续时段",
                "elapsed_time": "承接上一章",
                "core_event": f"通过{engine}推进事件，并迫使人物作出选择。",
                "state_change": "人物目标、关系或局势至少一项发生可验证变化",
                "dramatic_turn": "新行动造成意外后果，人物必须重新判断局势",
                "reader_payoff": primary_promise,
                "scene_execution": normalize_scene_execution(
                    {
                        "entry_pressure": "上一章未完动作或当前麻烦已经逼到眼前",
                        "protagonist_want": "主角想立刻解决眼前阻力并保住自己的退路",
                        "opposing_want": "对手戏人物要维护自己的目标，不会无条件配合主角",
                        "tactic_turns": [
                            {
                                "actor": "主角",
                                "tactic": "先试探并采取一个可见行动",
                                "counterforce": "对方识破或现实条件顶回来",
                                "local_change": "原计划失效，信息与风险发生变化",
                            },
                            {
                                "actor": "对手戏人物",
                                "tactic": "利用新局面提出条件或主动行动",
                                "counterforce": "主角作出不能撤回的选择",
                                "local_change": "关系、决定或下一步行动被改写",
                            },
                        ],
                        "dialogue_pressure": {
                            "surface_topic": "双方围绕眼前问题交涉",
                            "hidden_stakes": "双方都在保护不愿直接承认的真实利益",
                            "decisive_exchange": "一次试探被回避后，反问迫使人物改变下一步行动",
                        },
                        "pov_reaction_chain": {
                            "observable_detail": "主角先注意到对方一个与口头态度不一致的动作",
                            "biased_interpretation": "主角按自己的愿望或戒备误读这个动作",
                            "immediate_impulse": "主角因此想回避、试探或逞强",
                            "visible_response": "主角的停顿、动作或改口让对方能够接招",
                        },
                        "dialogue_reaction_chain": {
                            "trigger": "对方用一句话或动作逼近当前问题",
                            "evasion_or_misread": "主角按性格抓错重点或故意回避",
                            "countermove": "对方追问、拆穿或顺势利用主角的回避",
                            "local_consequence": "双方关系、信息或下一步行动随之改变",
                        },
                        "voice_contrast": [],
                        "absurd_comedy_mode": {"enabled": False},
                        "ending_residual_force": {
                            "last_change": "一个行动造成无法忽略的新后果",
                            "reader_question": "人物将如何处理这个已经发生的变化",
                            "next_chapter_first_beat": "从对该后果的即时回应开始",
                        },
                    }
                ),
                "participants": [brief.get("protagonist") or "主角"],
                "interaction_contexts": [],
                "ending_hook": (
                    ""
                    if is_closing and offset == chapter_count - 1
                    else "人物发现一个足以改变下一步选择的新情况。"
                ),
            }
        )
    raw = {
        "event_type": event_type,
        "event_title": task_input.get("event_title") or "一次改变人物选择的事件",
        "event_goal": brief.get("plot_direction") or ("完成主线收束和人物落点。" if is_closing else "让主角关系出现阶段性变化，并引出下一轮冲突。"),
        "core_conflict": brief.get("core_conflict") or "主角在目标受阻后必须作出会改变局势的选择。",
        "genre_alignment": f"围绕“{contract.get('primary_genre') or novel.genre}”兑现：{primary_promise}",
        "dramatic_escalation": "局部阻力升级为需要人物主动取舍并承担代价的冲突。",
        "major_reversal": "关键行动暴露此前未知的信息，改变人物对局势的判断。",
        "reader_payoff": primary_promise,
        "key_characters": [brief.get("protagonist") or "主角"],
        "completion_criteria": ["事件起因明确", "冲突升级", "人物关系变化", "结尾完成收束" if is_closing else "结尾留下新钩子"],
        "next_event_hook": "" if is_closing else "事件收束后，有人注意到主角隐藏的一面。",
        "narrative_completion": _normalize_narrative_completion({}, event_type),
        "candidate_directions": [
            {
                "direction": f"用{engine}驱动事件",
                "plot_engine": engine,
                "primary_promise_served": primary_promise,
                "secondary_element_role": "只作压力、代价或行动工具，不替代主类型冲突",
                "conflict_source": "人物目标与当前阻力发生正面冲突",
                "character_choice": "主角主动选择推进或改变局势",
                "dramatic_escalation": "选择引发更高成本和更紧迫的后果",
                "turn_or_reframe": "新信息改变人物原有判断",
                "cost_or_consequence": "人物必须承担可延续到后文的代价",
                "reader_payoff": primary_promise,
                "difference_from_other_candidates": f"核心由{engine}而非其它驱动力推进",
            }
            for engine in generic_engines
        ],
        "selection_rationale": "选择最能兑现本书主类型承诺且能形成升级、转折和回报的方向。",
        "chapter_plans": chapter_plans,
    }
    return _normalize_event_plan(raw, novel=novel, chapter_count=chapter_count, start_index=start_index, task_input=task_input)


def _build_simulated_chapter(context: dict, event_plan: dict, chapter_plan: dict) -> tuple[str, str, str]:
    """事件 Graph 的兜底章节生成，方便无 API Key 时跑通流程。"""
    novel = context["novel"]
    chapter_index = chapter_plan["chapter_index"]
    title = chapter_plan.get("title") or f"第 {chapter_index} 章"
    summary = f"本章围绕“{event_plan['event_title']}”推进，承担“{chapter_plan.get('function', '推进')}”功能。"
    content = "\n\n".join(
        [
            title,
            f"这一章继续推进事件“{event_plan['event_title']}”。",
            f"事件目标：{event_plan['event_goal']}",
            f"本章核心事件：{chapter_plan.get('core_event', '推进主线冲突')}",
            f"人物推进：{'；'.join(chapter_plan.get('character_beats') or ['人物关系出现新的变化'])}",
            f"伏笔动作：{'；'.join(chapter_plan.get('foreshadowing_actions') or ['保留后续回收空间'])}",
            f"章末钩子：{chapter_plan.get('ending_hook') or event_plan.get('next_event_hook') or '新的问题浮出水面。'}",
            f"本段为事件级 LangGraph 生成的模拟章节，用于验证《{novel['title']}》多章节闭环生产流程。",
        ]
    )
    return title, summary, content


def _create_or_update_story_event(
    db: Session,
    task: GenerationTask,
    novel: Novel,
    event_plan: dict[str, Any],
    start_chapter_index: int,
    chapter_count: int,
) -> StoryEvent:
    """把事件规划写入正式 StoryEvent / EventChapterPlan 表。"""
    story_event = db.scalar(select(StoryEvent).where(StoryEvent.task_id == task.id))
    if story_event is None:
        story_event = StoryEvent(novel_id=novel.id, task_id=task.id)
        db.add(story_event)

    story_event.title = _compact_text(event_plan.get("event_title"), 160, "新的剧情事件")
    story_event.goal = event_plan.get("event_goal", "")
    story_event.core_conflict = event_plan.get("core_conflict", "")
    story_event.status = "generating"
    story_event.start_chapter_index = start_chapter_index
    story_event.end_chapter_index = start_chapter_index + chapter_count - 1
    story_event.planned_chapter_count = chapter_count
    story_event.next_event_hook = event_plan.get("next_event_hook", "")
    story_event.payload = event_plan
    db.commit()
    db.refresh(story_event)

    for chapter_plan in event_plan.get("chapter_plans", []):
        plan = db.scalar(
            select(EventChapterPlan).where(
                EventChapterPlan.story_event_id == story_event.id,
                EventChapterPlan.chapter_index == chapter_plan["chapter_index"],
            )
        )
        if plan is None:
            plan = EventChapterPlan(
                story_event_id=story_event.id,
                novel_id=novel.id,
                chapter_index=chapter_plan["chapter_index"],
                status="planned",
            )
            db.add(plan)
        elif plan.status not in {"generated", "revised"}:
            plan.status = "planned"

        plan.title = _compact_text(chapter_plan.get("title"), 160, f"第 {chapter_plan['chapter_index']} 章")
        plan.function = _compact_text(chapter_plan.get("function"), 60, "推进")
        plan.core_event = chapter_plan.get("core_event", "")
        plan.ending_hook = chapter_plan.get("ending_hook", "")
        plan.payload = chapter_plan
    db.commit()
    return story_event


def _update_event_chapter_plan(
    db: Session,
    story_event_id: str,
    chapter: Chapter,
    status: str,
) -> None:
    """把已生成章节回填到对应事件章节计划。"""
    plan = db.scalar(
        select(EventChapterPlan).where(
            EventChapterPlan.story_event_id == UUID(story_event_id),
            EventChapterPlan.chapter_index == chapter.chapter_index,
        )
    )
    if plan is None:
        return
    plan.chapter_id = chapter.id
    plan.title = chapter.title or plan.title
    plan.status = status
    plan.payload = {
        **(plan.payload or {}),
        "generated_chapter_id": str(chapter.id),
        "generated_word_count": chapter.word_count,
        "word_guard": (chapter.context_snapshot or {}).get("word_guard", {}),
    }
    db.commit()


def _update_story_event_progress(
    db: Session,
    story_event_id: str,
    status: str | None = None,
    auto_repair_count: int | None = None,
    remaining_open_risks: int | None = None,
) -> None:
    """同步 StoryEvent 的聚合进度字段。"""
    story_event = db.get(StoryEvent, UUID(story_event_id))
    if story_event is None:
        return

    generated_count = db.scalar(
        select(func.count())
        .select_from(EventChapterPlan)
        .where(
            EventChapterPlan.story_event_id == story_event.id,
            EventChapterPlan.status.in_(["generated", "revised"]),
        )
    )
    story_event.generated_chapter_count = generated_count or 0
    if status:
        story_event.status = status
    if auto_repair_count is not None:
        story_event.auto_repair_count = auto_repair_count
    if remaining_open_risks is not None:
        story_event.remaining_open_risks = remaining_open_risks
    db.commit()


def run_event_generation_graph(db: Session, task: GenerationTask, novel: Novel) -> dict[str, Any]:
    """运行事件级多章节生成 Graph，并返回可写入任务结果的摘要。"""
    task_input = (task.result_payload or {}).get("input", {})
    plan_only = bool(task_input.get("plan_only"))
    story_bible_context = get_story_bible_context(db, novel)
    planning_input = {
        **task_input,
        "story_bible": story_bible_context,
    }
    test_run_scope = str(
        task_input.get("test_run_scope")
        or (task_input.get("production_pacing") or {}).get("test_run_scope")
        or "event"
    ).strip()
    chapter_count = _clip_chapter_count(
        task_input.get("chapter_count")
        or task_input.get("event_chapter_count")
        or resolve_event_chapter_count(novel.brief or {}),
        allow_single_chapter=test_run_scope == "first_chapter",
    )
    start_chapter_index = (
        db.scalar(select(func.max(Chapter.chapter_index)).where(Chapter.novel_id == novel.id)) or 0
    ) + 1
    owner = db.get(User, novel.owner_id)
    preferences = owner.preferences if owner else {}
    llm_config = build_llm_config(preferences)
    review_llm_config = build_review_llm_config(preferences)
    planning_llm_config = review_llm_config
    graph_name = "EventGenerationGraph"
    review_executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="chapter-review-pipeline",
    )
    review_futures: dict[int, Future] = {}
    pause_poll_lock = Lock()
    pause_poll_state = {"checked_at": 0.0, "requested": False}

    def is_pause_requested() -> bool:
        """读取总控暂停标记；单次模型请求无法中断，但其余步骤应立即停止。"""
        auto_run_id = task_input.get("auto_run_id")
        if not auto_run_id:
            return False
        if pause_poll_state["requested"]:
            return True
        now = time.monotonic()
        if now - pause_poll_state["checked_at"] < 0.75:
            return False
        with pause_poll_lock:
            if pause_poll_state["requested"]:
                return True
            now = time.monotonic()
            if now - pause_poll_state["checked_at"] < 0.75:
                return False
            pause_poll_state["checked_at"] = now
            try:
                resolved_id = UUID(str(auto_run_id))
            except (TypeError, ValueError):
                return False
            with SessionLocal() as pause_db:
                auto_run = pause_db.get(AutoNovelRun, resolved_id)
                requested = bool(
                    auto_run
                    and (
                        auto_run.status == "paused"
                        or (auto_run.payload or {}).get("pause_requested")
                    )
                )
            pause_poll_state["requested"] = requested
            return requested

    if llm_config is not None:
        llm_config.cancel_check = is_pause_requested
    if review_llm_config is not None:
        review_llm_config.cancel_check = is_pause_requested

    def run_parallel_chapter_review(
        chapter_id: str,
        chapter_index: int,
        progress: int,
        pipeline_input: dict[str, Any],
        story_event_id: str,
    ) -> dict[str, Any]:
        """使用独立 DB Session 审校已落库草稿，避免阻塞后续章节生成。"""
        with SessionLocal() as review_db:
            review_task = review_db.get(GenerationTask, task.id)
            review_novel = review_db.get(Novel, novel.id)
            review_chapter = review_db.get(Chapter, UUID(chapter_id))
            if review_task is None or review_novel is None or review_chapter is None:
                raise RuntimeError(f"第 {chapter_index} 章并行审校上下文不存在")
            result = review_and_revise_chapter_once(
                db=review_db,
                novel=review_novel,
                chapter=review_chapter,
                task=review_task,
                reviewer_config=review_llm_config,
                writer_config=llm_config,
                story_event_id=UUID(story_event_id),
                progress=progress,
            )
            review_db.refresh(review_chapter)
            sync_event_plan_after_pipeline(
                review_db,
                task_input=pipeline_input,
                chapter=review_chapter,
                status="generated",
            )
            chapter_progress = (
                (review_chapter.context_snapshot or {}).get("chapter_progress") or {}
            )
            fact_delta = sync_chapter_fact_delta(
                review_db,
                novel=review_novel,
                chapter=review_chapter,
                chapter_progress=chapter_progress,
            )
            memory_tasks = enqueue_chapter_memory_tasks(
                review_db,
                novel=review_novel,
                chapters=[review_chapter],
                source_task=review_task,
                story_event_id=UUID(story_event_id),
                source_progress=min(96, max(0, progress + 2)),
                sync_reason="parallel_chapter_review_complete",
                expected_total=chapter_count,
            )
            return {
                **result,
                "chapter_id": chapter_id,
                "chapter_index": chapter_index,
                "fact_delta_id": str(fact_delta.id) if fact_delta is not None else "",
                "memory_tasks": [
                    {
                        "task_id": str(memory_task.id),
                        "chapter_id": str(memory_task.chapter_id),
                        "status": "queued",
                        "background": True,
                        "sync_reason": "parallel_chapter_review_complete",
                    }
                    for memory_task in memory_tasks
                ],
                "parallel": True,
            }

    def submit_parallel_chapter_review(
        *,
        chapter_id: str,
        chapter_index: int,
        progress: int,
        pipeline_input: dict[str, Any],
        story_event_id: str,
    ) -> Future:
        existing = review_futures.get(chapter_index)
        if existing is not None:
            return existing
        future = review_executor.submit(
            run_parallel_chapter_review,
            chapter_id,
            chapter_index,
            progress,
            pipeline_input,
            story_event_id,
        )
        review_futures[chapter_index] = future
        return future

    def settle_parallel_chapter_reviews(
        state: EventGenerationState,
    ) -> EventGenerationState:
        """生成完全部草稿后统一收口；审校失败不回滚已生成的后续章节。"""
        generated = [dict(item) for item in state.get("generated_chapters", [])]
        story_event_id = str(state.get("story_event_id") or "")
        for item in generated:
            chapter_index = int(item["chapter_index"])
            if chapter_index not in review_futures:
                persisted_chapter = db.get(Chapter, UUID(str(item["chapter_id"])))
                persisted_review = (
                    (persisted_chapter.context_snapshot or {}).get("chapter_review_cycle")
                    if persisted_chapter is not None
                    else None
                )
                if (
                    isinstance(persisted_review, dict)
                    and persisted_review.get("status") in {"applied", "clean", "deferred"}
                ):
                    completed_future: Future = Future()
                    completed_future.set_result(
                        {
                            **persisted_review,
                            "chapter_id": str(item["chapter_id"]),
                            "chapter_index": chapter_index,
                            "memory_tasks": [],
                            "parallel": True,
                            "restored_from_chapter_snapshot": True,
                        }
                    )
                    review_futures[chapter_index] = completed_future
                    continue
                plans = state.get("chapter_plans") or []
                plan_offset = next(
                    (
                        index
                        for index, plan in enumerate(plans)
                        if int(plan.get("chapter_index") or 0) == chapter_index
                    ),
                    -1,
                )
                if plan_offset < 0:
                    raise RuntimeError(f"找不到第 {chapter_index} 章的事件计划")
                chapter_plan = plans[plan_offset]
                next_plan = (
                    plans[plan_offset + 1]
                    if plan_offset + 1 < len(plans)
                    else None
                )
                pipeline_input = {
                    **task_input,
                    "source": "event_generation_graph",
                    "story_event_id": story_event_id,
                    "story_event": compact_story_event_for_chapter(state["event_plan"]),
                    "chapter_plan": chapter_plan,
                    "next_chapter_boundary": compact_next_chapter_boundary(next_plan),
                }
                submit_parallel_chapter_review(
                    chapter_id=str(item["chapter_id"]),
                    chapter_index=chapter_index,
                    progress=65,
                    pipeline_input=pipeline_input,
                    story_event_id=story_event_id,
                )

        reviews: list[dict[str, Any]] = []
        memory_sync = list(state.get("memory_sync", []))
        for item in generated:
            chapter_index = int(item["chapter_index"])
            try:
                review = review_futures[chapter_index].result()
            except Exception as exc:
                review = {
                    "status": "failed",
                    "error": str(exc),
                    "chapter_id": str(item["chapter_id"]),
                    "chapter_index": chapter_index,
                    "parallel": True,
                }
            item["chapter_review_cycle"] = review
            reviews.append(review)
            memory_sync.extend(review.get("memory_tasks") or [])
        db.expire_all()
        return {
            **state,
            "generated_chapters": generated,
            "continuity_reviews": reviews,
            "revision_results": reviews,
            "memory_sync": memory_sync,
        }

    def plan_event(state: EventGenerationState) -> EventGenerationState:
        _set_task_progress(db, task, 12, "正在从优秀样本检索剧情可能性", step_key="plot_rag")
        try:
            plot_reference_pack = build_plot_design_reference_pack(
                db,
                novel=novel,
                planning_input=planning_input,
                preferences=preferences,
            )
        except Exception as exc:
            db.rollback()
            plot_reference_pack = {
                "status": "degraded",
                "reason": f"剧情样本检索失败：{exc}",
                "channel": "plot",
                "references": [],
                "total_chars": 0,
            }
        current_planning_input = {
            **planning_input,
            "plot_reference_pack": plot_reference_pack,
        }
        _set_task_progress(db, task, 15, "正在发散并筛选多方向剧情事件", step_key="event_plan")
        if planning_llm_config is None:
            event_plan = _build_simulated_event_plan(
                novel,
                current_planning_input,
                chapter_count,
                start_chapter_index,
            )
        else:
            try:
                planning_messages = _build_event_plan_prompt(
                    novel,
                    current_planning_input,
                    chapter_count,
                    start_chapter_index,
                )
                planner_client = LLMClient(planning_llm_config)
                narrative_contract = _narrative_contract_from_input(
                    novel,
                    current_planning_input,
                )
                last_planner_activity_notice_at = 0.0
                planner_round = 1
                auxiliary_repair_records: list[dict[str, Any]] = []
                scene_orchestration_records: list[dict[str, Any]] = []
                planner_thinking = ModelThinkingPublisher(
                    task,
                    source_step_key="event_plan",
                    model_role="reviewer",
                    model=planning_llm_config.model,
                    title="审校模型 · 剧情事件规划",
                )
                planner_thinking.start()

                def report_planner_activity(activity: dict[str, Any]) -> None:
                    nonlocal last_planner_activity_notice_at, planner_round
                    planner_thinking.append_activity(
                        {**activity, "generation_attempt": planner_round}
                    )
                    now = time.monotonic()
                    if now - last_planner_activity_notice_at < 10:
                        return
                    last_planner_activity_notice_at = now
                    emit_task_event(
                        db,
                        task,
                        event_type="step",
                        step_key="event_plan",
                        status="running",
                        title="深度思考模型正在规划剧情事件",
                        message=(
                            "连接持续活跃；"
                            f"已接收思考 {int(activity.get('reasoning_chars') or 0)} 字符、"
                            f"最终输出 {int(activity.get('output_chars') or 0)} 字符"
                        ),
                        progress=15,
                        payload={"stream_activity": activity},
                    )

                def refine_scene_orchestration(
                    current_plan: dict[str, Any],
                    *,
                    reason: str,
                ) -> dict[str, Any]:
                    nonlocal planner_round
                    planner_round += 1
                    planner_thinking.start(attempt=planner_round)
                    emit_task_event(
                        db,
                        task,
                        event_type="event_plan_scene_orchestration",
                        step_key="event_plan",
                        status="running",
                        title="正在编排章节对手戏与语言节奏",
                        message="正在补全视角误读、对白反应链、人物微动作与章际接力",
                        progress=15,
                        payload={"phase": "scene_orchestration", "reason": reason},
                    )
                    try:
                        _, scene_payload = planner_client.complete_json(
                            _build_scene_orchestration_prompt(
                                current_plan,
                                narrative_contract,
                            ),
                            required_keys=("chapter_scene_directions",),
                            required_non_empty_keys=("chapter_scene_directions",),
                            on_activity=report_planner_activity,
                        )
                    except LLMRequestCancelledError:
                        raise
                    except Exception as exc:
                        scene_orchestration_records.append(
                            {
                                "status": "failed",
                                "reason": reason,
                                "error": str(exc),
                            }
                        )
                        return current_plan
                    refined, validation = _merge_scene_orchestration(
                        current_plan,
                        scene_payload,
                    )
                    scene_orchestration_records.append(
                        {
                            "status": "completed" if validation["passed"] else "partial",
                            "reason": reason,
                            "validation": validation,
                        }
                    )
                    return refined

                try:
                    _, parsed = planner_client.complete_json(
                        planning_messages,
                        on_activity=report_planner_activity,
                    )
                    parsed = refine_scene_orchestration(
                        parsed,
                        reason="initial_plan",
                    )
                    diversity_report = _candidate_diversity_report(parsed)
                    quality_report = _event_plan_quality_report(
                        parsed,
                        narrative_contract,
                    )
                    # 结构缺失、喜剧交付不足等全局问题允许完整重规划一次；
                    # 单纯的辅助元素疑点不得触发整份重写。
                    if (
                        not diversity_report["passed"]
                        or not _quality_passes_without_auxiliary(quality_report)
                    ):
                        planner_round += 1
                        planner_thinking.start(attempt=planner_round)
                        _, parsed = planner_client.complete_json(
                            [
                                *planning_messages,
                                {
                                    "role": "assistant",
                                    "content": json.dumps(
                                        parsed,
                                        ensure_ascii=False,
                                    ),
                                },
                                {
                                    "role": "user",
                                    "content": (
                                        "上一次结果没有同时通过剧情方向多样性和本书类型质量门禁。"
                                        "请按 narrative_contract 重新规划：至少4个候选、至少3种 plot_engine；"
                                        "每个候选补全 primary_promise_served、dramatic_escalation、reader_payoff；"
                                        "事件补全 genre_alignment、major_reversal 和 reader_payoff；"
                                        "每章补全 plot_engine、dramatic_turn、reader_payoff，以及完整 scene_execution："
                                        "开场压力、双方即时目标、至少两轮 actor/tactic/counterforce/local_change、"
                                        "dialogue_pressure、pov_reaction_chain、dialogue_reaction_chain 和 ending_residual_force 都必须填写。"
                                        "职业、技能、身份和设定名词若不是本书核心类型，只能作为能力、压力、代价或翻盘工具，"
                                        "不能连续主导剧情，不能成为固定口癖、感情比喻或主要笑点。"
                                        "喜剧作品每章必须有4—6个不依赖网络热梗的因果型喜剧节拍。"
                                        "可使用答非所问、错位联想、一本正经跑偏等抽象机制，但必须锚定人物并由现场反应和后果接住。"
                                        "第1章不得只是铺垫，必须用即时扰动、受阻/反转和强行动钩子承担追读功能。"
                                        "不能只是更换地点、道具或流程。门禁报告："
                                        + json.dumps(
                                            {
                                                "diversity": diversity_report,
                                                "quality": quality_report,
                                            },
                                            ensure_ascii=False,
                                        )
                                        + "。重新输出完整 JSON。"
                                    ),
                                },
                            ],
                            on_activity=report_planner_activity,
                        )
                        parsed = refine_scene_orchestration(
                            parsed,
                            reason="global_replan",
                        )
                        diversity_report = _candidate_diversity_report(parsed)
                        quality_report = _event_plan_quality_report(
                            parsed,
                            narrative_contract,
                        )
                        if (
                            not diversity_report["passed"]
                            or not _quality_passes_without_auxiliary(quality_report)
                        ):
                            raise EventPlanningQualityError(
                                "剧情事件规划的结构或类型交付连续两次不完整，"
                                "系统未生成不合格章节。"
                            )

                    repair_round = 0
                    while not (
                        quality_report.get("auxiliary_dominance") or {}
                    ).get("passed"):
                        suspect_indexes = _auxiliary_suspect_indexes(quality_report)
                        emit_task_event(
                            db,
                            task,
                            event_type="event_plan_repair",
                            step_key="event_plan",
                            status="running",
                            title="正在语义复核辅助元素是否主导剧情",
                            message=(
                                "正在检查第 "
                                + "、".join(str(index) for index in suspect_indexes)
                                + " 章；关键词命中不会直接判定失败"
                            ),
                            progress=15,
                            payload={
                                "phase": "semantic_review",
                                "suspect_chapter_indexes": suspect_indexes,
                                "keyword_violations": (
                                    quality_report.get("auxiliary_dominance") or {}
                                ).get("violations")
                                or [],
                            },
                        )
                        try:
                            planner_round += 1
                            planner_thinking.start(attempt=planner_round)
                            _, semantic_review = LLMClient(
                                planning_llm_config
                            ).complete_json(
                                _build_auxiliary_semantic_review_prompt(
                                    parsed,
                                    quality_report,
                                    narrative_contract,
                                ),
                                required_keys=(
                                    "dominant_chapter_indexes",
                                    "assessments",
                                ),
                                on_activity=report_planner_activity,
                            )
                            target_indexes = _semantic_auxiliary_targets(
                                semantic_review,
                                quality_report,
                            )
                        except LLMRequestCancelledError:
                            raise
                        except Exception as exc:
                            # 语义复核失败时采取保守策略：不放行，自动修复全部疑点章。
                            target_indexes = suspect_indexes
                            semantic_review = {
                                "status": "failed",
                                "error": str(exc),
                                "dominant_chapter_indexes": target_indexes,
                                "assessments": [],
                                "fallback": "repair_all_suspects",
                            }

                        if not target_indexes:
                            quality_report = _mark_auxiliary_semantically_passed(
                                quality_report,
                                semantic_review,
                            )
                            emit_task_event(
                                db,
                                task,
                                event_type="event_plan_repair",
                                step_key="event_plan",
                                status="completed",
                                title="辅助元素语义复核通过",
                                message="疑点章节中的流程或职业元素仅作为背景/工具，不主导剧情",
                                progress=15,
                                payload={
                                    "phase": "semantic_review",
                                    "semantic_review": semantic_review,
                                },
                            )
                            break

                        if repair_round >= MAX_AUXILIARY_REPAIR_ROUNDS:
                            break
                        repair_round += 1
                        planner_round += 1
                        planner_thinking.start(attempt=planner_round)
                        emit_task_event(
                            db,
                            task,
                            event_type="event_plan_repair",
                            step_key="event_plan",
                            status="running",
                            title="正在自动修复问题章节",
                            message=(
                                "正在自动修复问题章节："
                                f"第 {repair_round}/{MAX_AUXILIARY_REPAIR_ROUNDS} 轮；"
                                "仅处理第 "
                                + "、".join(str(index) for index in target_indexes)
                                + " 章，其他合格计划保持不变"
                            ),
                            progress=15,
                            payload={
                                "phase": "targeted_repair",
                                "repair_round": repair_round,
                                "target_chapter_indexes": target_indexes,
                                "semantic_review": semantic_review,
                            },
                        )
                        _, replacements_payload = planner_client.complete_json(
                            _build_auxiliary_repair_prompt(
                                parsed,
                                quality_report,
                                semantic_review,
                                target_indexes,
                                narrative_contract,
                                repair_round,
                            ),
                            on_activity=report_planner_activity,
                            required_keys=("chapter_plan_replacements",),
                            required_non_empty_keys=("chapter_plan_replacements",),
                        )
                        repaired_plan, merge_validation = (
                            _merge_chapter_plan_replacements(
                                parsed,
                                replacements_payload,
                                target_indexes,
                            )
                        )
                        auxiliary_repair_records.append(
                            {
                                "repair_round": repair_round,
                                "target_chapter_indexes": target_indexes,
                                "semantic_review": semantic_review,
                                "merge_validation": merge_validation,
                            }
                        )
                        if not merge_validation["passed"]:
                            continue
                        parsed = repaired_plan
                        diversity_report = _candidate_diversity_report(parsed)
                        quality_report = _event_plan_quality_report(
                            parsed,
                            narrative_contract,
                        )

                    if (
                        not diversity_report["passed"]
                        or not quality_report["passed"]
                    ):
                        remaining_indexes = _auxiliary_suspect_indexes(quality_report)
                        emit_task_event(
                            db,
                            task,
                            event_type="event_plan_repair",
                            step_key="event_plan",
                            status="failed",
                            title="问题章节自动重构未通过验收",
                            message=(
                                "仍未通过的章节："
                                + "、".join(str(index) for index in remaining_indexes)
                            ),
                            progress=15,
                            payload={
                                "phase": "repair_failed",
                                "remaining_chapter_indexes": remaining_indexes,
                                "diversity": diversity_report,
                                "quality": quality_report,
                                "repair_records": auxiliary_repair_records,
                            },
                        )
                        raise EventPlanningQualityError(
                            "剧情事件规划已自动局部重构 "
                            f"{repair_round} 轮，但问题章节仍未通过质量验收；"
                            "系统未继续生成不合格正文。"
                        )
                except Exception:
                    planner_thinking.finish(status="failed")
                    raise
                else:
                    planner_thinking.finish()
                event_plan = _normalize_event_plan(
                    parsed,
                    novel=novel,
                    chapter_count=chapter_count,
                    start_index=start_chapter_index,
                    task_input=current_planning_input,
                )
                # 归一化会重新运行关键词初筛；保留已经完成的语义复核与自动修复结论。
                event_plan["planning_diversity"] = diversity_report
                event_plan["planning_quality"] = {
                    **quality_report,
                    "automatic_repair_records": auxiliary_repair_records,
                    "scene_orchestration_records": scene_orchestration_records,
                }
            except EventPlanningQualityError:
                raise
            except Exception as exc:
                event_plan = _build_simulated_event_plan(
                    novel,
                    current_planning_input,
                    chapter_count,
                    start_chapter_index,
                )
                event_plan["planner_error"] = str(exc)
                event_plan["generation_mode"] = "simulation_after_planner_error"
        event_plan["plot_rag"] = {
            "status": plot_reference_pack.get("status", "skipped"),
            "reference_count": len(plot_reference_pack.get("references") or []),
            "total_chars": plot_reference_pack.get("total_chars", 0),
            "embedding_model": plot_reference_pack.get("embedding_model", ""),
            "reason": plot_reference_pack.get("reason", ""),
        }
        story_event = _create_or_update_story_event(
            db=db,
            task=task,
            novel=novel,
            event_plan=event_plan,
            start_chapter_index=start_chapter_index,
            chapter_count=chapter_count,
        )
        emit_task_event(
            db,
            task,
            event_type="step",
            step_key="event_plan",
            status="completed",
            title="剧情事件规划完成",
            message=f"已拆分为 {chapter_count} 个章节计划",
            progress=17,
            payload={
                "event_title": event_plan.get("event_title"),
                "chapter_count": chapter_count,
                "candidate_direction_count": len(event_plan.get("candidate_directions") or []),
                "planning_quality": event_plan.get("planning_quality", {}),
                "plot_rag": event_plan.get("plot_rag", {}),
            },
        )
        _set_task_progress(db, task, 18, "正在检索情节写法与搞笑话术", step_key="event_research")
        research_summary = collect_event_research(
            db=db,
            novel=novel,
            owner=owner,
            event_plan=event_plan,
            llm_config=planning_llm_config,
            search_llm_config=llm_config,
        )
        story_event.payload = {**(story_event.payload or {}), "event_research": research_summary}
        db.commit()
        emit_task_event(
            db,
            task,
            event_type="step",
            step_key="event_research",
            status="completed",
            title="情节与表达资料检索完成",
            message=_event_research_message(research_summary),
            progress=19,
            payload=research_summary,
        )
        return {
            **state,
            "story_event_id": str(story_event.id),
            "event_plan": event_plan,
            "chapter_plans": event_plan["chapter_plans"],
            "next_plan_index": 0,
            "start_chapter_index": start_chapter_index,
            "generated_chapters": [],
            "memory_sync": [],
            "continuity_reviews": [],
            "revision_results": [],
            "research_source_ids": research_summary.get("source_ids", []),
            "research_summary": research_summary,
            "plot_reference_pack": plot_reference_pack,
        }

    def finish_plan_only(state: EventGenerationState) -> EventGenerationState:
        _set_task_progress(
            db,
            task,
            92,
            "剧情事件计划已生成，等待用户确认章节计划",
            step_key="event_plan_confirmation",
        )
        event_plan = state["event_plan"]
        story_event = db.get(StoryEvent, UUID(state["story_event_id"]))
        summary = {
            "event_type": event_plan.get("event_type", ""),
            "event_title": event_plan["event_title"],
            "event_goal": event_plan["event_goal"],
            "chapters_generated": 0,
            "chapter_range": {
                "start": story_event.start_chapter_index if story_event else state.get("start_chapter_index"),
                "end": story_event.end_chapter_index if story_event else None,
            },
            "next_event_hook": event_plan.get("next_event_hook", ""),
            "completion_criteria": event_plan.get("completion_criteria", []),
            "narrative_completion": event_plan.get("narrative_completion", {}),
            "plan_review": "pending",
        }
        if story_event is not None:
            story_event.status = "planned"
            story_event.generated_chapter_count = 0
            story_event.payload = {
                **(story_event.payload or {}),
                "event_summary": summary,
                "human_review": {
                    "status": "pending_confirmation",
                    "source": task_input.get("source", ""),
                },
            }
            db.commit()
        return {**state, "event_summary": summary}

    def generate_next_chapter(state: EventGenerationState) -> EventGenerationState:
        if is_pause_requested():
            return {**state, "pause_requested": True}
        chapter_plans = state["chapter_plans"]
        plan_index = state.get("next_plan_index", 0)
        chapter_plan = chapter_plans[plan_index]
        next_plan = chapter_plans[plan_index + 1] if plan_index + 1 < len(chapter_plans) else None
        progress = 20 + int(45 * (plan_index / max(len(chapter_plans), 1)))
        _set_task_progress(
            db,
            task,
            progress,
            f"正在生成第 {chapter_plan['chapter_index']} 章",
            step_key=f"chapter_{chapter_plan['chapter_index']}_draft",
            chapter_index=chapter_plan["chapter_index"],
            payload={"chapter_plan": compact_next_chapter_boundary(chapter_plan)},
        )

        context = build_chapter_context(
            db=db,
            novel=novel,
            target_chapter_index=chapter_plan["chapter_index"],
            task_input={
                **task_input,
                "source": "event_generation_graph",
                "story_event": compact_story_event_for_chapter(state["event_plan"]),
                "chapter_plan": chapter_plan,
                "next_chapter_boundary": compact_next_chapter_boundary(next_plan),
                "production_pacing": task_input.get("production_pacing") or {},
                "research_source_ids": state.get("research_source_ids", []),
            },
        )
        pipeline_input = {
            **task_input,
            "source": "event_generation_graph",
            "story_event_id": state["story_event_id"],
            "story_event": compact_story_event_for_chapter(state["event_plan"]),
            "chapter_plan": chapter_plan,
            "next_chapter_boundary": compact_next_chapter_boundary(next_plan),
            "production_pacing": task_input.get("production_pacing") or {},
            "research_source_ids": state.get("research_source_ids", []),
        }
        try:
            pipeline = run_chapter_pipeline(
                db=db,
                task=task,
                novel=novel,
                request=ChapterPipelineRequest(
                    target_chapter_index=chapter_plan["chapter_index"],
                    task_input=pipeline_input,
                    simulated_builder=lambda current_context: _build_simulated_chapter(
                        current_context,
                        state["event_plan"],
                        chapter_plan,
                    ),
                    llm_config=llm_config,
                    review_llm_config=review_llm_config,
                    context=context,
                    story_event_id=UUID(state["story_event_id"]),
                    progress=progress,
                    force_title=True,
                    memory_expected_total=len(chapter_plans),
                    strict_quality_gate=bool(
                        task_input.get(
                            "strict_quality_gate",
                            review_llm_config is not None,
                        )
                    ),
                    defer_review=True,
                ),
            )
        except LLMRequestCancelledError:
            db.rollback()
            return {**state, "pause_requested": True}
        chapter = pipeline.chapter
        generation_mode = pipeline.generation_mode
        llm_model = pipeline.llm_model
        word_guard = pipeline.word_guard
        chapter_progress = pipeline.chapter_progress
        chapter_review = pipeline.chapter_review
        requires_word_revision = pipeline.requires_word_revision
        if not requires_word_revision:
            submit_parallel_chapter_review(
                chapter_id=str(chapter.id),
                chapter_index=chapter.chapter_index,
                progress=progress,
                pipeline_input=pipeline_input,
                story_event_id=state["story_event_id"],
            )
        if is_pause_requested():
            return {**state, "pause_requested": True}
        _update_story_event_progress(
            db,
            state["story_event_id"],
            status="paused" if requires_word_revision else "generating",
        )

        updated_chapter_plans = apply_revised_next_plan(chapter_plans, plan_index + 1, chapter_progress)
        if not requires_word_revision and updated_chapter_plans != chapter_plans and next_plan is not None:
            revised_plan = updated_chapter_plans[plan_index + 1]
            plan_record = db.scalar(
                select(EventChapterPlan).where(
                    EventChapterPlan.story_event_id == UUID(state["story_event_id"]),
                    EventChapterPlan.chapter_index == revised_plan["chapter_index"],
                )
            )
            if plan_record is not None:
                plan_record.title = revised_plan.get("title", plan_record.title)
                plan_record.function = revised_plan.get("function", plan_record.function)
                plan_record.core_event = revised_plan.get("core_event", plan_record.core_event)
                plan_record.ending_hook = revised_plan.get("ending_hook", plan_record.ending_hook)
                plan_record.payload = {
                    **(plan_record.payload or {}),
                    **revised_plan,
                    "rolling_revision": {
                        "source_chapter_index": chapter.chapter_index,
                        "consumed_next_beats": chapter_progress.get("consumed_next_beats", []),
                    },
                }
            story_event = db.get(StoryEvent, UUID(state["story_event_id"]))
            if story_event is not None:
                story_event.payload = {
                    **(story_event.payload or {}),
                    "chapter_plans": updated_chapter_plans,
                }
            db.commit()

        if chapter_review.get("status") == "applied":
            db.refresh(chapter)
            _update_event_chapter_plan(
                db,
                state["story_event_id"],
                chapter,
                "generated",
            )

        chapter_memory_tasks = pipeline.memory_tasks

        generated = [
            *state.get("generated_chapters", []),
            {
                "chapter_id": str(chapter.id),
                "chapter_index": chapter.chapter_index,
                "title": chapter.title,
                "word_count": chapter.word_count,
                "generation_mode": generation_mode,
                "llm_model": llm_model,
                "word_guard": word_guard,
                "chapter_progress": chapter_progress,
                "chapter_review_cycle": chapter_review,
            },
        ]
        next_state = {
            **state,
            "generated_chapters": generated,
            "last_chapter_id": str(chapter.id),
            "last_chapter_progress": chapter_progress,
            "chapter_plans": updated_chapter_plans,
            "event_plan": {**state["event_plan"], "chapter_plans": updated_chapter_plans},
            "next_plan_index": plan_index if requires_word_revision else plan_index + 1,
            "continuity_reviews": [*state.get("continuity_reviews", []), chapter_review],
            "revision_results": [*state.get("revision_results", []), chapter_review],
            "memory_sync": [
                *state.get("memory_sync", []),
                *[
                    {
                        "task_id": str(memory_task.id),
                        "chapter_id": str(memory_task.chapter_id),
                        "status": "queued",
                        "background": True,
                        "sync_reason": "chapter_review_complete",
                    }
                    for memory_task in chapter_memory_tasks
                ],
            ],
        }
        if requires_word_revision:
            next_state["word_revision_pause"] = {
                "story_event_id": state["story_event_id"],
                "chapter_id": str(chapter.id),
                "chapter_index": chapter.chapter_index,
                "word_count": chapter.word_count,
                "word_guard": word_guard,
            }
        if pipeline.requires_quality_revision:
            next_state["continuity_reviews"] = [
                *next_state.get("continuity_reviews", []),
                {
                    "status": "pending_parallel_review",
                    "chapter_id": str(chapter.id),
                    "chapter_index": chapter.chapter_index,
                    "pipeline_quality_flags": (pipeline.contract or {}).get("failure") or {},
                },
            ]
        return next_state

    def pause_for_user_request(state: EventGenerationState) -> EventGenerationState:
        plan_index = state.get("next_plan_index", 0)
        chapter_plans = state.get("chapter_plans", [])
        next_chapter_index = (
            chapter_plans[plan_index].get("chapter_index")
            if plan_index < len(chapter_plans)
            else None
        )
        pause_info = {
            "story_event_id": state.get("story_event_id", ""),
            "next_chapter_index": next_chapter_index,
            "chapters_generated": len(state.get("generated_chapters", [])),
        }
        _set_task_progress(db, task, 92, "已响应暂停请求，未继续生成后续章节", step_key="pause", status="completed")
        if next_chapter_index is not None:
            chapter_progress = 20 + int(45 * (plan_index / max(len(chapter_plans), 1)))
            emit_task_event(
                db,
                task,
                event_type="step",
                step_key=f"chapter_{next_chapter_index}_draft",
                status="paused",
                title=f"第 {next_chapter_index} 章生成已暂停",
                message="已停止当前生成；继续自动生产后会从本章重新开始",
                progress=chapter_progress,
                chapter_index=next_chapter_index,
                payload={
                    "pause_requested": True,
                    "discarded_unsaved_generation": True,
                },
            )
        story_event_id = pause_info["story_event_id"]
        story_event = db.get(StoryEvent, UUID(story_event_id)) if story_event_id else None
        if story_event is not None:
            story_event.status = "paused"
            story_event.payload = {
                **(story_event.payload or {}),
                "immediate_pause": pause_info,
            }
            db.commit()
        return {
            **state,
            "pause_requested": True,
            "immediate_pause": pause_info,
            "event_summary": {
                "event_title": (state.get("event_plan") or {}).get("event_title", ""),
                "chapters_generated": pause_info["chapters_generated"],
                "status": "paused_immediately",
                "immediate_pause": pause_info,
            },
        }

    def pause_for_word_revision(state: EventGenerationState) -> EventGenerationState:
        pause_info = state.get("word_revision_pause") or {}
        _set_task_progress(
            db,
            task,
            92,
            f"第 {pause_info.get('chapter_index', '')} 章已保存为待字数修正草稿",
        )
        story_event = db.get(StoryEvent, UUID(state["story_event_id"]))
        if story_event is not None:
            story_event.status = "paused"
            story_event.payload = {
                **(story_event.payload or {}),
                "word_revision_pause": pause_info,
            }
            db.commit()
        return {
            **state,
            "event_summary": {
                "event_title": (state.get("event_plan") or {}).get("event_title", ""),
                "chapters_generated": len(state.get("generated_chapters", [])),
                "status": "word_revision_required",
                "word_revision_pause": pause_info,
            },
        }

    def pause_for_quality_revision(state: EventGenerationState) -> EventGenerationState:
        pause_info = state.get("quality_revision_pause") or {}
        _set_task_progress(
            db,
            task,
            92,
            (
                f"第 {pause_info.get('chapter_index', '')} 章需要质量修订，"
                f"已暂停后续生成："
                f"{pause_info.get('error') or pause_info.get('reason') or '质量门禁未通过'}"
            ),
            step_key="quality_gate_pause",
            status="failed",
        )
        story_event = db.get(StoryEvent, UUID(state["story_event_id"]))
        if story_event is not None:
            story_event.status = "paused"
            story_event.payload = {
                **(story_event.payload or {}),
                "quality_revision_pause": pause_info,
            }
            db.commit()
        return {
            **state,
            "event_summary": {
                "event_title": (state.get("event_plan") or {}).get("event_title", ""),
                "chapters_generated": len(state.get("generated_chapters", [])),
                "status": "quality_revision_required",
                "quality_revision_pause": pause_info,
            },
        }

    def summarize_event(state: EventGenerationState) -> EventGenerationState:
        state = settle_parallel_chapter_reviews(state)
        _set_task_progress(db, task, 78, "正在执行事件级统一审校与局部补丁修订", step_key="event_review_initial")
        event_plan = state["event_plan"]
        generated = state.get("generated_chapters", [])
        summary = {
            "event_type": event_plan.get("event_type", ""),
            "event_title": event_plan["event_title"],
            "event_goal": event_plan["event_goal"],
            "chapters_generated": len(generated),
            "chapter_range": {
                "start": generated[0]["chapter_index"] if generated else None,
                "end": generated[-1]["chapter_index"] if generated else None,
            },
            "next_event_hook": event_plan.get("next_event_hook", ""),
            "completion_criteria": event_plan.get("completion_criteria", []),
            "narrative_completion": event_plan.get("narrative_completion", {}),
        }
        story_event = db.get(StoryEvent, UUID(state["story_event_id"]))
        if story_event is not None:
            story_event.status = "reviewing"
            story_event.start_chapter_index = summary["chapter_range"]["start"]
            story_event.end_chapter_index = summary["chapter_range"]["end"]
            story_event.generated_chapter_count = len(generated)
            story_event.next_event_hook = summary["next_event_hook"]
            story_event.payload = {**(story_event.payload or {}), "event_summary": summary}
            db.commit()
            event_revision = review_and_repair_story_event(
                db=db,
                novel=novel,
                story_event=story_event,
                llm_config=review_llm_config,
                task=task,
                revision_llm_config=llm_config,
            )
            remaining_open = int(event_revision.get("remaining_open_risks") or 0)
            story_event.status = "paused" if remaining_open else "completed"
            story_event.auto_repair_count = (story_event.auto_repair_count or 0) + int(
                event_revision.get("repaired_chapter_count") or 0
            )
            story_event.remaining_open_risks = remaining_open
            summary["status"] = "quality_revision_required" if remaining_open else "completed"
            summary["event_quality"] = event_revision.get("final_review", {})
            summary["event_revision"] = event_revision
            story_event.payload = {
                **(story_event.payload or {}),
                "event_summary": summary,
                "event_revision": event_revision,
            }
            db.commit()
            revised_chapter_indexes = {
                int(chapter_index)
                for package in event_revision.get("repair_packages", [])
                if package.get("status") in {"resolved", "partial"}
                for chapter_index in package.get("chapter_indexes", [])
            }
            memory_chapters = [
                db.get(Chapter, UUID(item["chapter_id"]))
                for item in generated
                if int(item["chapter_index"]) in revised_chapter_indexes
            ]
            memory_tasks = enqueue_chapter_memory_tasks(
                db,
                novel=novel,
                chapters=[chapter for chapter in memory_chapters if chapter is not None],
                source_task=task,
                story_event_id=story_event.id,
                source_progress=95,
                sync_reason="event_revision_resync",
                expected_total=len(generated),
            )
            memory_sync = [
                {
                    "task_id": str(memory_task.id),
                    "chapter_id": str(memory_task.chapter_id),
                    "status": "queued",
                    "background": True,
                }
                for memory_task in memory_tasks
            ]
            next_state = {
                **state,
                "event_summary": summary,
                "event_revision": event_revision,
                "memory_sync": [*state.get("memory_sync", []), *memory_sync],
            }
            if remaining_open:
                next_state["quality_revision_pause"] = {
                    "story_event_id": str(story_event.id),
                    "remaining_open": remaining_open,
                    "repair_rounds": event_revision.get("repair_rounds", 0),
                    "max_attempts": event_revision.get("max_repair_rounds", 1),
                    "scope": "event",
                }
            return next_state
        return {**state, "event_summary": summary}

    def should_continue(state: EventGenerationState) -> str:
        has_remaining_chapters = state.get("next_plan_index", 0) < len(state.get("chapter_plans", []))
        if (state.get("pause_requested") or is_pause_requested()) and has_remaining_chapters:
            return "user_pause"
        if has_remaining_chapters:
            return "generate"
        return "summarize"

    def should_generate_after_plan(state: EventGenerationState) -> str:
        if is_pause_requested():
            return "user_pause"
        return "plan_only" if plan_only else "generate"

    def should_continue_after_generation(state: EventGenerationState) -> str:
        if state.get("pause_requested"):
            return "user_pause"
        if state.get("quality_revision_pause"):
            return "quality_revision_pause"
        return "word_revision_pause" if state.get("word_revision_pause") else "continue"

    def checkpointed(node_name: str, handler, *, terminal: bool = False):
        def wrapped(state: EventGenerationState) -> EventGenerationState:
            result = handler(state)
            merged = {**state, **result, "_resume_node": node_name}
            save_graph_checkpoint(
                db,
                task=task,
                graph_name=graph_name,
                node_name=node_name,
                state=merged,
                status="completed" if terminal else "running",
            )
            return merged

        return wrapped

    def resume_route(state: EventGenerationState) -> str:
        node = state.get("_resume_node", "")
        if node == "plan_event":
            return "after_plan"
        if node == "generate_next_chapter":
            return "after_generation"
        if node in {
            "finish_plan_only",
            "pause_for_word_revision",
            "pause_for_quality_revision",
            "pause_for_user_request",
            "summarize_event",
        }:
            return "finished"
        return "plan"

    graph = StateGraph(EventGenerationState)
    graph.add_node("resume_entry", lambda state: state)
    graph.add_node("resume_after_plan", lambda state: state)
    graph.add_node("resume_after_generation", lambda state: state)
    graph.add_node("resume_finished", lambda state: state)
    graph.add_node("plan_event", checkpointed("plan_event", plan_event))
    graph.add_node(
        "finish_plan_only",
        checkpointed("finish_plan_only", finish_plan_only, terminal=True),
    )
    graph.add_node(
        "generate_next_chapter",
        checkpointed("generate_next_chapter", generate_next_chapter),
    )
    graph.add_node(
        "pause_for_word_revision",
        checkpointed("pause_for_word_revision", pause_for_word_revision, terminal=True),
    )
    graph.add_node(
        "pause_for_quality_revision",
        checkpointed("pause_for_quality_revision", pause_for_quality_revision, terminal=True),
    )
    graph.add_node(
        "pause_for_user_request",
        checkpointed("pause_for_user_request", pause_for_user_request, terminal=True),
    )
    graph.add_node(
        "summarize_event",
        checkpointed("summarize_event", summarize_event, terminal=True),
    )
    graph.add_edge(START, "resume_entry")
    graph.add_conditional_edges(
        "resume_entry",
        resume_route,
        {
            "plan": "plan_event",
            "after_plan": "resume_after_plan",
            "after_generation": "resume_after_generation",
            "finished": "resume_finished",
        },
    )
    graph.add_edge("resume_finished", END)
    graph.add_conditional_edges(
        "plan_event",
        should_generate_after_plan,
        {"plan_only": "finish_plan_only", "generate": "generate_next_chapter", "user_pause": "pause_for_user_request"},
    )
    graph.add_conditional_edges(
        "resume_after_plan",
        should_generate_after_plan,
        {"plan_only": "finish_plan_only", "generate": "generate_next_chapter", "user_pause": "pause_for_user_request"},
    )
    graph.add_edge("finish_plan_only", END)
    graph.add_conditional_edges(
        "generate_next_chapter",
        should_continue_after_generation,
        {
            "continue": "continue_event",
            "word_revision_pause": "pause_for_word_revision",
            "quality_revision_pause": "pause_for_quality_revision",
            "user_pause": "pause_for_user_request",
        },
    )
    graph.add_conditional_edges(
        "resume_after_generation",
        should_continue_after_generation,
        {
            "continue": "continue_event",
            "word_revision_pause": "pause_for_word_revision",
            "quality_revision_pause": "pause_for_quality_revision",
            "user_pause": "pause_for_user_request",
        },
    )
    graph.add_edge("pause_for_word_revision", END)
    graph.add_edge("pause_for_quality_revision", END)
    graph.add_edge("pause_for_user_request", END)
    graph.add_node("continue_event", lambda state: state)
    graph.add_conditional_edges(
        "continue_event",
        should_continue,
        {"generate": "generate_next_chapter", "summarize": "summarize_event", "user_pause": "pause_for_user_request"},
    )
    graph.add_edge("summarize_event", END)

    compiled = graph.compile()
    persisted = load_graph_checkpoint(db, task=task, graph_name=graph_name)
    initial_state: EventGenerationState = {
        **(persisted.state if persisted is not None else {}),
        "input": task_input,
    }
    if persisted is not None:
        initial_state["_resume_node"] = persisted.node_name
    try:
        final_state = compiled.invoke(initial_state)
    finally:
        review_executor.shutdown(wait=True, cancel_futures=False)
    save_graph_checkpoint(
        db,
        task=task,
        graph_name=graph_name,
        node_name=final_state.get("_resume_node", "completed"),
        state=final_state,
        status="completed",
    )
    planner_degraded = bool((final_state.get("event_plan") or {}).get("planner_error"))
    waiting_for_intervention = bool(
        final_state.get("word_revision_pause")
        or final_state.get("quality_revision_pause")
        or final_state.get("pause_requested")
    )
    failure = (
        AgentFailure(
            code="planner_fallback_used",
            message=str((final_state.get("event_plan") or {}).get("planner_error") or ""),
            stage="event_planning",
            retryable=True,
        )
        if planner_degraded
        else None
    )
    return {
        "agent": "StoryPlanningAgent",
        "graph": "EventGenerationGraph",
        "generation_mode": "llm" if llm_config else "simulation",
        "llm_model": llm_config.model if llm_config else "",
        "writer_model": llm_config.model if llm_config else "",
        "planning_model": planning_llm_config.model if planning_llm_config else "",
        "reviewer_model": review_llm_config.model if review_llm_config else "",
        "story_event_id": final_state.get("story_event_id", ""),
        "event_plan": final_state.get("event_plan", {}),
        "event_summary": final_state.get("event_summary", {}),
        "generated_chapters": final_state.get("generated_chapters", []),
        "memory_sync": final_state.get("memory_sync", []),
        "continuity_reviews": final_state.get("continuity_reviews", []),
        "revision_results": final_state.get("revision_results", []),
        "event_revision": final_state.get("event_revision", {}),
        "research_summary": final_state.get("research_summary", {}),
        "requires_word_revision": bool(final_state.get("word_revision_pause")),
        "word_revision_pause": final_state.get("word_revision_pause", {}),
        "requires_quality_revision": bool(final_state.get("quality_revision_pause")),
        "quality_revision_pause": final_state.get("quality_revision_pause", {}),
        "pause_requested": bool(final_state.get("pause_requested")),
        "immediate_pause": final_state.get("immediate_pause", {}),
        "contract": agent_contract(
            "StoryPlanningAgent",
            "event_generation",
            status=(
                "degraded"
                if planner_degraded
                else ("waiting" if waiting_for_intervention else "success")
            ),
            generation_mode=(
                "simulation_after_planner_error"
                if planner_degraded
                else ("llm" if llm_config else "simulation")
            ),
            degraded=planner_degraded,
            failure=failure,
        ),
    }
