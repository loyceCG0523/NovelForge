"""事件级多章节生成 LangGraph。

这个 Graph 负责把“一键围绕一个完整大事件生成多章”的流程编排起来。第一版不新增
StoryEvent 数据表，而是把事件规划写入任务结果和章节上下文快照，先验证 Agent 流水线。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select
from sqlalchemy.orm import Session


API_DIR = Path(__file__).resolve().parents[3] / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from app.models.chapter import Chapter
from app.models.event_chapter_plan import EventChapterPlan
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.models.story_event import StoryEvent
from app.models.user import User
from app.services.agents.memory_agent import sync_memory_after_chapter
from app.services.agents.quality_agent import review_chapter_quality, review_story_event_quality
from app.services.agents.story_planning_agent import get_story_bible_context
from app.services.chapter_context_builder import build_chapter_context
from app.services.llm_client import LLMClient, LLMConfig, build_llm_config
from app.services.prompt_builder import build_chapter_prompt


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
    last_issue_count: int
    memory_sync: list[dict[str, Any]]
    continuity_reviews: list[dict[str, Any]]
    revision_results: list[dict[str, Any]]
    event_summary: dict[str, Any]


def _set_task_progress(db: Session, task: GenerationTask, progress: int, note: str) -> None:
    """把 Graph 执行阶段同步到任务记录，方便前端轮询。"""
    task.progress = max(task.progress or 0, min(progress, 95))
    task.result_payload = {
        **(task.result_payload or {}),
        "graph_status": note,
    }
    db.commit()
    db.refresh(task)


def _clip_chapter_count(value: Any) -> int:
    """限制事件章节数量，让一个大事件保持正常网文章节节奏。"""
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 8
    return max(6, min(count, 12))


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


def _build_event_plan_prompt(novel: Novel, task_input: dict, chapter_count: int, start_index: int) -> list[dict[str, str]]:
    """构建事件规划 Prompt，要求返回一个闭环大事件和多章拆解。"""
    brief = novel.brief or {}
    story_bible = task_input.get("story_bible") or {}
    payload = {
        "task": {
            "type": "plan_story_event",
            "chapter_count": chapter_count,
            "start_chapter_index": start_index,
            "user_request": task_input,
        },
        "novel": {
            "title": novel.title,
            "genre": novel.genre,
            "premise": novel.premise,
            "brief": brief,
            "story_bible": story_bible,
            "current_chapter_index": novel.current_chapter_index,
        },
        "expected_output": {
            "event_title": "完整剧情事件标题",
            "event_goal": "这个事件结束时必须完成的剧情结果",
            "core_conflict": "事件核心冲突",
            "key_characters": ["主要人物"],
            "completion_criteria": ["闭环标准"],
            "next_event_hook": "事件收束后引出的下一个事件钩子",
            "chapter_plans": [
                {
                    "chapter_index": start_index,
                    "title": "章节标题",
                    "function": "铺垫|冲突|升级|反转|收束",
                    "core_event": "本章核心事件",
                    "character_beats": ["人物关系或心理推进"],
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
                    "你是 NovelForge 的事件篇章规划 Agent。",
                    "你的任务是围绕一个完整、闭环的大事件，规划连续多章剧情。",
                    "每章必须服务同一个大事件，不能散成互不相关的日常片段。",
                    "输出必须是 JSON 对象，不要输出 Markdown 或解释文字。",
                ]
            ),
        },
        {
            "role": "user",
            "content": "\n".join(
                [
                    f"请为《{novel.title}》规划接下来 {chapter_count} 章的闭环剧情事件。",
                    "要求事件有起因、升级、转折、结果，并在结尾留下下一个事件钩子。",
                    json.dumps(payload, ensure_ascii=False, indent=2),
                ]
            ),
        },
    ]


def _normalize_event_plan(raw: dict[str, Any], novel: Novel, chapter_count: int, start_index: int) -> dict[str, Any]:
    """清洗事件规划，保证后续节点拿到稳定字段。"""
    brief = novel.brief or {}
    event_title = _derive_event_title(raw, novel, start_index, chapter_count)
    chapter_plans = raw.get("chapter_plans")
    if not isinstance(chapter_plans, list):
        chapter_plans = []

    normalized_plans = []
    for offset in range(chapter_count):
        source = chapter_plans[offset] if offset < len(chapter_plans) and isinstance(chapter_plans[offset], dict) else {}
        chapter_index = start_index + offset
        normalized_plans.append(
            {
                "chapter_index": chapter_index,
                "title": _compact_text(source.get("title"), 120, f"第 {chapter_index} 章"),
                "function": _compact_text(source.get("function"), 50, "收束" if offset == chapter_count - 1 else "推进"),
                "core_event": str(source.get("core_event") or f"推进事件“{event_title}”").strip(),
                "character_beats": source.get("character_beats") if isinstance(source.get("character_beats"), list) else [],
                "foreshadowing_actions": (
                    source.get("foreshadowing_actions") if isinstance(source.get("foreshadowing_actions"), list) else []
                ),
                "ending_hook": str(source.get("ending_hook") or "").strip(),
            }
        )

    return {
        "event_title": event_title,
        "event_goal": str(raw.get("event_goal") or brief.get("selling_points") or "完成阶段性剧情推进").strip(),
        "core_conflict": str(raw.get("core_conflict") or brief.get("conflict") or "主角面临新的选择和压力").strip(),
        "key_characters": raw.get("key_characters") if isinstance(raw.get("key_characters"), list) else [],
        "completion_criteria": raw.get("completion_criteria") if isinstance(raw.get("completion_criteria"), list) else [],
        "next_event_hook": str(raw.get("next_event_hook") or "").strip(),
        "chapter_plans": normalized_plans,
    }


def _build_simulated_event_plan(novel: Novel, task_input: dict, chapter_count: int, start_index: int) -> dict[str, Any]:
    """未配置 LLM 时提供可测试的事件规划。"""
    brief = novel.brief or {}
    raw = {
        "event_title": task_input.get("event_title") or "一次改变关系的校园事件",
        "event_goal": brief.get("plot_direction") or "让主角关系出现阶段性变化，并引出下一轮冲突。",
        "core_conflict": brief.get("core_conflict") or "主角在集体事件中被低估，又被迫证明自己。",
        "key_characters": [brief.get("protagonist") or "主角"],
        "completion_criteria": ["事件起因明确", "冲突升级", "人物关系变化", "结尾留下新钩子"],
        "next_event_hook": "事件收束后，有人注意到主角隐藏的一面。",
    }
    return _normalize_event_plan(raw, novel=novel, chapter_count=chapter_count, start_index=start_index)


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
    story_bible_context = get_story_bible_context(db, novel)
    task_input = {
        **task_input,
        "story_bible": story_bible_context,
    }
    chapter_count = _clip_chapter_count(task_input.get("chapter_count") or task_input.get("event_chapter_count"))
    start_chapter_index = (
        db.scalar(select(func.max(Chapter.chapter_index)).where(Chapter.novel_id == novel.id)) or 0
    ) + 1
    owner = db.get(User, novel.owner_id)
    llm_config = build_llm_config(owner.preferences if owner else {})

    def plan_event(state: EventGenerationState) -> EventGenerationState:
        _set_task_progress(db, task, 15, "正在规划闭环剧情事件")
        if llm_config is None:
            event_plan = _build_simulated_event_plan(novel, task_input, chapter_count, start_chapter_index)
        else:
            try:
                _, parsed = LLMClient(llm_config).complete_json(
                    _build_event_plan_prompt(novel, task_input, chapter_count, start_chapter_index)
                )
                event_plan = _normalize_event_plan(
                    parsed,
                    novel=novel,
                    chapter_count=chapter_count,
                    start_index=start_chapter_index,
                )
            except Exception as exc:
                event_plan = _build_simulated_event_plan(novel, task_input, chapter_count, start_chapter_index)
                event_plan["planner_error"] = str(exc)
        story_event = _create_or_update_story_event(
            db=db,
            task=task,
            novel=novel,
            event_plan=event_plan,
            start_chapter_index=start_chapter_index,
            chapter_count=chapter_count,
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
        }

    def generate_next_chapter(state: EventGenerationState) -> EventGenerationState:
        chapter_plans = state["chapter_plans"]
        plan_index = state.get("next_plan_index", 0)
        chapter_plan = chapter_plans[plan_index]
        progress = 20 + int(45 * (plan_index / max(len(chapter_plans), 1)))
        _set_task_progress(db, task, progress, f"正在生成第 {chapter_plan['chapter_index']} 章")

        context = build_chapter_context(
            db=db,
            novel=novel,
            target_chapter_index=chapter_plan["chapter_index"],
            task_input={
                **task_input,
                "source": "event_generation_graph",
                "story_event": state["event_plan"],
                "chapter_plan": chapter_plan,
            },
        )
        if llm_config is None:
            title, summary, content = _build_simulated_chapter(context, state["event_plan"], chapter_plan)
            generation_mode = "simulation"
            llm_model = ""
        else:
            llm_result = LLMClient(llm_config).generate_chapter(build_chapter_prompt(context))
            title = llm_result["title"]
            summary = llm_result["summary"]
            content = llm_result["content"]
            generation_mode = "llm"
            llm_model = llm_config.model

        chapter = db.scalar(
            select(Chapter).where(
                Chapter.novel_id == novel.id,
                Chapter.chapter_index == chapter_plan["chapter_index"],
            )
        )
        if chapter is None:
            chapter = Chapter(
                novel_id=novel.id,
                chapter_index=chapter_plan["chapter_index"],
                title=title,
                status="done",
                summary=summary,
                content=content,
                word_count=len(content),
                context_snapshot=context,
            )
            db.add(chapter)
        else:
            chapter.title = title
            chapter.status = "done"
            chapter.summary = summary
            chapter.content = content
            chapter.word_count = len(content)
            chapter.context_snapshot = context

        novel.current_chapter_index = max(novel.current_chapter_index, chapter.chapter_index)
        db.commit()
        db.refresh(chapter)
        _update_event_chapter_plan(db, state["story_event_id"], chapter, "generated")
        _update_story_event_progress(db, state["story_event_id"], status="generating")

        generated = [
            *state.get("generated_chapters", []),
            {
                "chapter_id": str(chapter.id),
                "chapter_index": chapter.chapter_index,
                "title": chapter.title,
                "word_count": chapter.word_count,
                "generation_mode": generation_mode,
                "llm_model": llm_model,
            },
        ]
        return {
            **state,
            "generated_chapters": generated,
            "last_chapter_id": str(chapter.id),
            "next_plan_index": plan_index + 1,
        }

    def sync_memory(state: EventGenerationState) -> EventGenerationState:
        chapter = db.get(Chapter, UUID(state["last_chapter_id"]))
        result = {"chapter_id": state["last_chapter_id"], "created": 0, "error": ""}
        try:
            items = sync_memory_after_chapter(db=db, novel=novel, chapter=chapter, llm_config=llm_config)
            result["created"] = len(items)
        except Exception as exc:
            db.rollback()
            result["error"] = str(exc)
        return {**state, "memory_sync": [*state.get("memory_sync", []), result]}

    def continuity_check(state: EventGenerationState) -> EventGenerationState:
        chapter = db.get(Chapter, UUID(state["last_chapter_id"]))
        context = build_chapter_context(
            db=db,
            novel=novel,
            target_chapter_index=chapter.chapter_index,
            task_input={
                **task_input,
                "source": "event_generation_graph_continuity_check",
                "story_event": state["event_plan"],
            },
        )
        result, auto_review = review_chapter_quality(
            db=db,
            novel=novel,
            chapter=chapter,
            context=context,
            llm_config=llm_config,
            task_id=str(task.id),
            source="event_generation_graph",
            max_attempts=2,
        )
        result = {"chapter_id": str(chapter.id), **result}
        if auto_review.get("resolved"):
            db.refresh(chapter)
            _update_event_chapter_plan(db, state["story_event_id"], chapter, "revised")
        total_repairs = len([item for item in [*state.get("revision_results", []), *auto_review.get("history", [])] if item.get("status") == "resolved"])
        total_remaining = sum(
            (review.get("auto_review_handling") or {}).get("remaining_open", 0)
            for review in state.get("continuity_reviews", [])
        ) + auto_review.get("remaining_open", 0)
        _update_story_event_progress(
            db=db,
            story_event_id=state["story_event_id"],
            auto_repair_count=total_repairs,
            remaining_open_risks=total_remaining,
        )
        return {
            **state,
            "last_issue_count": result["issues"],
            "continuity_reviews": [*state.get("continuity_reviews", []), {**result, "auto_review_handling": auto_review}],
            "revision_results": [*state.get("revision_results", []), *auto_review.get("history", [])],
        }

    def summarize_event(state: EventGenerationState) -> EventGenerationState:
        _set_task_progress(db, task, 92, "正在汇总剧情事件结果")
        event_plan = state["event_plan"]
        generated = state.get("generated_chapters", [])
        summary = {
            "event_title": event_plan["event_title"],
            "event_goal": event_plan["event_goal"],
            "chapters_generated": len(generated),
            "chapter_range": {
                "start": generated[0]["chapter_index"] if generated else None,
                "end": generated[-1]["chapter_index"] if generated else None,
            },
            "next_event_hook": event_plan.get("next_event_hook", ""),
            "completion_criteria": event_plan.get("completion_criteria", []),
        }
        story_event = db.get(StoryEvent, UUID(state["story_event_id"]))
        if story_event is not None:
            story_event.status = "completed"
            story_event.start_chapter_index = summary["chapter_range"]["start"]
            story_event.end_chapter_index = summary["chapter_range"]["end"]
            story_event.generated_chapter_count = len(generated)
            story_event.next_event_hook = summary["next_event_hook"]
            story_event.payload = {**(story_event.payload or {}), "event_summary": summary}
            db.commit()
            quality_result = review_story_event_quality(
                db=db,
                novel=novel,
                story_event=story_event,
                llm_config=llm_config,
            )
            summary["event_quality"] = quality_result
        return {**state, "event_summary": summary}

    def should_continue(state: EventGenerationState) -> str:
        if state.get("next_plan_index", 0) < len(state.get("chapter_plans", [])):
            return "generate"
        return "summarize"

    graph = StateGraph(EventGenerationState)
    graph.add_node("plan_event", plan_event)
    graph.add_node("generate_next_chapter", generate_next_chapter)
    graph.add_node("sync_memory", sync_memory)
    graph.add_node("continuity_check", continuity_check)
    graph.add_node("summarize_event", summarize_event)
    graph.add_edge(START, "plan_event")
    graph.add_edge("plan_event", "generate_next_chapter")
    graph.add_edge("generate_next_chapter", "sync_memory")
    graph.add_edge("sync_memory", "continuity_check")
    graph.add_node("continue_event", lambda state: state)
    graph.add_edge("continuity_check", "continue_event")
    graph.add_conditional_edges("continue_event", should_continue, {"generate": "generate_next_chapter", "summarize": "summarize_event"})
    graph.add_edge("summarize_event", END)

    compiled = graph.compile()
    final_state = compiled.invoke({"input": task_input})
    return {
        "agent": "StoryPlanningAgent",
        "graph": "EventGenerationGraph",
        "generation_mode": "llm" if llm_config else "simulation",
        "llm_model": llm_config.model if llm_config else "",
        "story_event_id": final_state.get("story_event_id", ""),
        "event_plan": final_state.get("event_plan", {}),
        "event_summary": final_state.get("event_summary", {}),
        "generated_chapters": final_state.get("generated_chapters", []),
        "memory_sync": final_state.get("memory_sync", []),
        "continuity_reviews": final_state.get("continuity_reviews", []),
        "revision_results": final_state.get("revision_results", []),
    }
