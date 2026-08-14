"""整本小说自动生产 LangGraph。"""

from __future__ import annotations

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

from app.models.auto_novel_run import AutoNovelRun
from app.models.chapter import Chapter
from app.models.foreshadowing import Foreshadowing
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.models.story_event import StoryEvent
from app.services.agents.story_planning_agent import get_story_bible_context
from app.services.agent_contracts import agent_contract
from app.services.agent_orchestrator import push_task_to_queue
from app.services.graph_checkpoint import load_graph_checkpoint, save_graph_checkpoint
from app.services.pacing_plan import is_closing_event, normalize_pacing_plan, resolve_pacing_state
from app.services.task_events import emit_task_event, initialize_task_todo
from app.services.tone_pacing_contract import resolve_event_chapter_count


PRODUCTION_MODE_AUTO = "auto"
PRODUCTION_MODE_HUMAN_LOOP = "human_in_loop"
PRODUCTION_MODE_TOMATO_TRIAL = "tomato_trial"
PRODUCTION_MODE_TEST_RUN = "test_run"
TOMATO_TRIAL_MIN_WORDS = 80_000
TOMATO_TRIAL_MAX_WORDS = 100_000


class NovelProductionState(TypedDict, total=False):
    """整本书生产 Graph 的可序列化状态。"""

    auto_run_id: str
    target_words: int
    current_words: int
    produced_event_count: int
    max_event_count: int
    chapter_count_per_event: int
    stage: str
    events: list[dict[str, Any]]
    stop_reason: str
    pacing_state: dict[str, Any]
    production_mode: str
    pending_child_task_id: str


def _current_word_count(db: Session, novel: Novel) -> int:
    """统计作品当前正文总字数。"""
    return db.scalar(select(func.coalesce(func.sum(Chapter.word_count), 0)).where(Chapter.novel_id == novel.id)) or 0


def _clip_event_count(value: Any) -> int:
    """限制单次生产最多推进的剧情事件数，避免误触后无法停机。"""
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 20
    return max(1, min(count, 80))


def _clip_chapter_count(value: Any) -> int:
    """限制每个剧情事件的章节数，保持网文大事件节奏。"""
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 6
    return max(4, min(count, 12))


def _normalize_production_mode(value: Any) -> str:
    """规范化整书生产模式。"""
    mode = str(value or PRODUCTION_MODE_AUTO).strip()
    if mode in {PRODUCTION_MODE_AUTO, PRODUCTION_MODE_HUMAN_LOOP, PRODUCTION_MODE_TOMATO_TRIAL, PRODUCTION_MODE_TEST_RUN}:
        return mode
    return PRODUCTION_MODE_AUTO


def _normalize_test_run_scope(value: Any) -> str:
    return "first_chapter" if str(value or "").strip() == "first_chapter" else "event"


def _set_parent_progress(db: Session, task: GenerationTask, run: AutoNovelRun, progress: int, stage: str) -> None:
    """同步父任务和 AutoNovelRun 状态，供工作台轮询。"""
    task.progress = max(task.progress or 0, min(progress, 95))
    task.result_payload = {
        **(task.result_payload or {}),
        "graph_status": stage,
    }
    run.stage = stage
    db.commit()
    db.refresh(task)
    db.refresh(run)


def _sync_run_stats(db: Session, novel: Novel, run: AutoNovelRun, extra_payload: dict[str, Any] | None = None) -> None:
    """刷新总控生产进度和可追溯 payload。"""
    run.current_words = _current_word_count(db, novel)
    run.payload = {
        **(run.payload or {}),
        **(extra_payload or {}),
    }
    db.commit()
    db.refresh(run)


def _pacing_plan_from_story_bible(story_bible_context: dict[str, Any], novel: Novel) -> dict[str, Any]:
    """从 StoryBible 中读取全书节奏计划；旧作品没有该字段时自动补默认计划。"""
    content = story_bible_context.get("content") if isinstance(story_bible_context, dict) else {}
    content = content if isinstance(content, dict) else {}
    return normalize_pacing_plan(content.get("pacing_plan"), novel.target_words)


def _latest_completed_story_event(db: Session, novel: Novel) -> StoryEvent | None:
    """读取最近完成的剧情事件，用于判断整书是否已经叙事闭环。"""
    return db.scalar(
        select(StoryEvent)
        .where(StoryEvent.novel_id == novel.id, StoryEvent.status == "completed")
        .order_by(StoryEvent.end_chapter_index.desc().nullslast(), StoryEvent.updated_at.desc())
        .limit(1)
    )


def _narrative_completion_snapshot(db: Session, novel: Novel) -> dict[str, Any]:
    """整理全书完结判断所需的叙事闭环状态。"""
    latest_event = _latest_completed_story_event(db, novel)
    latest_payload = latest_event.payload if latest_event and isinstance(latest_event.payload, dict) else {}
    open_foreshadowing_count = db.scalar(
        select(func.count())
        .select_from(Foreshadowing)
        .where(Foreshadowing.novel_id == novel.id, Foreshadowing.status.in_(["planted", "pending", "active"]))
    ) or 0
    return {
        "latest_event_id": str(latest_event.id) if latest_event else "",
        "latest_event_title": latest_event.title if latest_event else "",
        "latest_event_type": str(latest_payload.get("event_type") or ""),
        "latest_event_completion": latest_payload.get("narrative_completion") or {},
        "narrative_closed": is_closing_event(latest_payload),
        "open_foreshadowing_count": open_foreshadowing_count,
    }


def _create_child_event_task(
    db: Session,
    parent_task: GenerationTask,
    novel: Novel,
    run: AutoNovelRun,
    event_index: int,
    chapter_count: int,
    production_pacing: dict[str, Any],
    plan_only: bool = False,
) -> GenerationTask:
    """创建真正异步的事件子任务；父任务进入 waiting，完成后由子任务唤醒。"""
    child_task = GenerationTask(
        novel_id=novel.id,
        task_type="generate_story_event",
        status="queued",
        progress=0,
        result_payload={
            "input": {
                "source": "novel_production_graph",
                "auto_run_id": str(run.id),
                "parent_task_id": str(parent_task.id),
                "production_event_index": event_index,
                "chapter_count": chapter_count,
                "test_run_scope": production_pacing.get("test_run_scope", "event"),
                "production_pacing": production_pacing,
                "plan_only": plan_only,
            },
            "agent": "NovelProductionAgent.child_event",
            "graph_status": "由整本书生产总控触发剧情事件生成",
        },
    )
    db.add(child_task)
    db.commit()
    db.refresh(child_task)
    initialize_task_todo(
        db,
        child_task,
        start_chapter_index=max(1, novel.current_chapter_index + 1),
        chapter_count=chapter_count,
        plan_only=plan_only,
    )
    try:
        push_task_to_queue(str(child_task.id))
    except Exception as exc:
        child_task.result_payload = {
            **(child_task.result_payload or {}),
            "queue_notification_error": str(exc),
            "queue_notification_retryable": True,
        }
        db.commit()
    return child_task


def run_novel_production_graph(db: Session, task: GenerationTask, novel: Novel) -> dict[str, Any]:
    """运行整本书生产总控 Graph。"""
    task_input = (task.result_payload or {}).get("input", {})
    auto_run_id = task_input.get("auto_run_id")
    if not auto_run_id:
        raise ValueError("整本书生产任务缺少 auto_run_id")

    auto_run = db.get(AutoNovelRun, UUID(str(auto_run_id)))
    if auto_run is None or auto_run.novel_id != novel.id:
        raise ValueError("AutoNovelRun 不存在，或不属于当前作品")

    chapter_count_per_event = _clip_chapter_count(
        task_input.get("chapter_count_per_event")
        or (auto_run.payload or {}).get("chapter_count_per_event")
        or resolve_event_chapter_count(novel.brief or {})
    )
    max_event_count = _clip_event_count(task_input.get("max_event_count") or auto_run.max_event_count)
    production_mode = _normalize_production_mode(task_input.get("production_mode") or (auto_run.payload or {}).get("production_mode"))
    test_run_scope = _normalize_test_run_scope(task_input.get("test_run_scope") or (auto_run.payload or {}).get("test_run_scope"))
    event_chapter_count = 1 if production_mode == PRODUCTION_MODE_TEST_RUN and test_run_scope == "first_chapter" else chapter_count_per_event
    graph_name = "NovelProductionGraph"

    def initialize(state: NovelProductionState) -> NovelProductionState:
        _set_parent_progress(db, task, auto_run, 8, "检查作品设定与生产状态")
        story_bible_context = get_story_bible_context(db, novel)
        pacing_plan = _pacing_plan_from_story_bible(story_bible_context, novel)
        existing_payload = auto_run.payload or {}
        existing_test_run = existing_payload.get("test_run") or {}
        pacing_state = resolve_pacing_state(
            target_words=novel.target_words,
            current_words=_current_word_count(db, novel),
            pacing_plan=pacing_plan,
        )
        _sync_run_stats(
            db,
            novel,
            auto_run,
            {
                "chapter_count_per_event": event_chapter_count,
                "production_mode": production_mode,
                "test_run_scope": test_run_scope,
                "pause_requested": bool(existing_payload.get("pause_requested")),
                "pacing_plan": pacing_plan,
                "pacing_state": pacing_state,
                "tomato_trial": {
                    "enabled": production_mode == PRODUCTION_MODE_TOMATO_TRIAL,
                    "min_words": TOMATO_TRIAL_MIN_WORDS,
                    "max_words": TOMATO_TRIAL_MAX_WORDS,
                    "note": "番茄试写模式按用户原始目标字数控制全书节奏，但首轮在 8-10 万字事件边界暂停。",
                },
                "test_run": {
                    "enabled": production_mode == PRODUCTION_MODE_TEST_RUN,
                    "event_limit": 1,
                    "scope": test_run_scope,
                    "start_event_count": int(
                        existing_test_run.get(
                            "start_event_count",
                            auto_run.produced_event_count,
                        )
                    ),
                    "note": "测试模式按所选范围生成：可完成一个剧情事件，或仅生成首章后暂停。",
                },
            },
        )
        auto_run.status = "running"
        auto_run.max_event_count = max_event_count
        auto_run.target_words = novel.target_words
        db.commit()
        return {
            **state,
            "target_words": novel.target_words,
            "current_words": auto_run.current_words,
            "produced_event_count": auto_run.produced_event_count,
            "max_event_count": max_event_count,
            "chapter_count_per_event": event_chapter_count,
            "events": (auto_run.payload or {}).get("events", []),
            "pacing_state": pacing_state,
            "production_mode": production_mode,
            "stop_reason": "",
            "pending_child_task_id": str(
                (auto_run.payload or {}).get("pending_child_task_id") or ""
            ),
        }

    def produce_next_event(state: NovelProductionState) -> NovelProductionState:
        event_number = int(state.get("produced_event_count", 0)) + 1
        progress = 10 + int(80 * min(event_number, max_event_count) / max(max_event_count, 1))
        pacing_plan = (auto_run.payload or {}).get("pacing_plan")
        pacing_state = resolve_pacing_state(
            target_words=auto_run.target_words,
            current_words=_current_word_count(db, novel),
            pacing_plan=pacing_plan,
        )
        _set_parent_progress(
            db,
            task,
            auto_run,
            progress,
            f"正在生成第 {event_number} 个剧情事件（{pacing_state.get('phase_label') or pacing_state.get('phase')}）",
        )

        child_task = _create_child_event_task(
            db=db,
            parent_task=task,
            novel=novel,
            run=auto_run,
            event_index=event_number,
            chapter_count=event_chapter_count,
            production_pacing={
                "pacing_plan": pacing_plan,
                "pacing_state": pacing_state,
                "production_mode": production_mode,
                "test_run_scope": test_run_scope,
            },
            plan_only=production_mode == PRODUCTION_MODE_HUMAN_LOOP,
        )
        auto_run.status = "running"
        auto_run.stage = "waiting_event_child"
        auto_run.current_event_id = None
        auto_run.payload = {
            **(auto_run.payload or {}),
            "pending_child_task_id": str(child_task.id),
            "pending_event_number": event_number,
            "pending_event_phase": pacing_state.get("phase", ""),
            "stop_reason": "waiting_event_child",
        }
        db.commit()
        emit_task_event(
            db,
            task,
            event_type="handoff",
            step_key=f"event_{event_number}_handoff",
            status="waiting",
            title=f"第 {event_number} 个剧情事件已交给独立 Worker 任务",
            message=f"子任务 {child_task.id} 完成后会自动唤醒整书总控",
            progress=progress,
            payload={
                "child_task_id": str(child_task.id),
                "event_number": event_number,
                "asynchronous": True,
            },
        )
        return {
            **state,
            "stop_reason": "waiting_event_child",
            "pending_child_task_id": str(child_task.id),
            "production_mode": production_mode,
        }

    def check_stop_condition(state: NovelProductionState) -> NovelProductionState:
        db.refresh(auto_run)
        pacing_plan = (auto_run.payload or {}).get("pacing_plan")
        pacing_state = resolve_pacing_state(
            target_words=auto_run.target_words,
            current_words=_current_word_count(db, novel),
            pacing_plan=pacing_plan,
        )
        narrative_completion = _narrative_completion_snapshot(db, novel)
        _sync_run_stats(
            db,
            novel,
            auto_run,
            {
                "pacing_state": pacing_state,
                "narrative_completion": narrative_completion,
            },
        )
        pending_child_task_id = str(
            (auto_run.payload or {}).get("pending_child_task_id") or ""
        )
        if pending_child_task_id:
            pending_child = db.get(
                GenerationTask,
                UUID(pending_child_task_id),
            )
            if pending_child is not None and pending_child.status in {
                "queued",
                "running",
            }:
                return {
                    **state,
                    "stop_reason": "waiting_event_child",
                    "pending_child_task_id": pending_child_task_id,
                    "current_words": auto_run.current_words,
                    "pacing_state": pacing_state,
                }
        if state.get("stop_reason") in {
            "chapter_word_revision_required",
            "event_quality_revision_required",
            "paused_immediately",
        }:
            return {
                **state,
                "stop_reason": state["stop_reason"],
                "current_words": auto_run.current_words,
                "pacing_state": pacing_state,
            }
        if auto_run.status == "paused" or (auto_run.payload or {}).get("pause_requested"):
            return {**state, "stop_reason": "paused", "current_words": auto_run.current_words, "pacing_state": pacing_state}
        if (auto_run.payload or {}).get("human_loop_status") == "waiting_plan_confirmation":
            return {
                **state,
                "stop_reason": "human_plan_review_required",
                "current_words": auto_run.current_words,
                "pacing_state": pacing_state,
            }
        if (auto_run.payload or {}).get("production_mode") == PRODUCTION_MODE_TOMATO_TRIAL and auto_run.current_words >= TOMATO_TRIAL_MIN_WORDS:
            return {
                **state,
                "stop_reason": "tomato_trial_reached",
                "current_words": auto_run.current_words,
                "pacing_state": pacing_state,
            }
        test_run_payload = (auto_run.payload or {}).get("test_run") or {}
        test_run_start_event_count = int(test_run_payload.get("start_event_count") or 0)
        if (
            (auto_run.payload or {}).get("production_mode") == PRODUCTION_MODE_TEST_RUN
            and auto_run.produced_event_count > test_run_start_event_count
        ):
            return {
                **state,
                "stop_reason": "test_run_event_completed",
                "current_words": auto_run.current_words,
                "pacing_state": pacing_state,
            }
        if pacing_state["is_in_completion_window"] and narrative_completion["narrative_closed"]:
            return {
                **state,
                "stop_reason": "narrative_completion_reached",
                "current_words": auto_run.current_words,
                "pacing_state": pacing_state,
            }
        if pacing_state["is_overrun"] and not narrative_completion["narrative_closed"]:
            return {
                **state,
                "stop_reason": "ending_overrun_needs_review",
                "current_words": auto_run.current_words,
                "pacing_state": pacing_state,
            }
        if auto_run.produced_event_count >= auto_run.max_event_count:
            return {**state, "stop_reason": "max_event_count_reached", "current_words": auto_run.current_words, "pacing_state": pacing_state}
        return {**state, "stop_reason": "", "pacing_state": pacing_state}

    def finalize(state: NovelProductionState) -> NovelProductionState:
        stop_reason = state.get("stop_reason") or "completed"
        if stop_reason == "paused":
            auto_run.status = "paused"
            auto_run.stage = "paused_after_event"
        elif stop_reason == "paused_immediately":
            auto_run.status = "paused"
            auto_run.stage = "paused_immediately"
        elif stop_reason in {"target_words_reached", "narrative_completion_reached"}:
            auto_run.status = "completed"
            auto_run.stage = "completed"
            novel.status = "completed"
        elif stop_reason == "ending_overrun_needs_review":
            auto_run.status = "paused"
            auto_run.stage = "ending_needs_review"
        elif stop_reason == "human_plan_review_required":
            auto_run.status = "paused"
            auto_run.stage = "waiting_plan_confirmation"
        elif stop_reason == "tomato_trial_reached":
            auto_run.status = "paused"
            auto_run.stage = "tomato_trial_completed"
        elif stop_reason == "test_run_event_completed":
            auto_run.status = "paused"
            auto_run.stage = "test_run_completed"
        elif stop_reason == "chapter_word_revision_required":
            auto_run.status = "paused"
            auto_run.stage = "word_revision_required"
        elif stop_reason == "event_quality_revision_required":
            auto_run.status = "paused"
            auto_run.stage = "quality_revision_required"
        elif stop_reason == "waiting_event_child":
            auto_run.status = "running"
            auto_run.stage = "waiting_event_child"
        elif stop_reason == "max_event_count_reached":
            auto_run.status = "paused"
            auto_run.stage = "guardrail_paused"
        elif stop_reason != "waiting_event_child":
            auto_run.status = "completed"
            auto_run.stage = "completed"
        auto_run.current_words = _current_word_count(db, novel)
        auto_run.payload = {
            **(auto_run.payload or {}),
            "stop_reason": stop_reason,
            "pause_requested": False,
            "pacing_state": state.get("pacing_state") or (auto_run.payload or {}).get("pacing_state", {}),
            "narrative_completion": _narrative_completion_snapshot(db, novel),
        }
        db.commit()
        _set_parent_progress(db, task, auto_run, 95, auto_run.stage)
        return {**state, "current_words": auto_run.current_words, "stop_reason": stop_reason}

    def should_continue(state: NovelProductionState) -> str:
        return "finalize" if state.get("stop_reason") else "produce"

    def checkpointed(node_name: str, handler, *, terminal: bool = False):
        def wrapped(state: NovelProductionState) -> NovelProductionState:
            result = handler(state)
            merged = {**state, **result}
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

    graph = StateGraph(NovelProductionState)
    graph.add_node("initialize", checkpointed("initialize", initialize))
    graph.add_node(
        "produce_next_event",
        checkpointed("produce_next_event", produce_next_event),
    )
    graph.add_node(
        "check_stop_condition",
        checkpointed("check_stop_condition", check_stop_condition),
    )
    graph.add_node("finalize", checkpointed("finalize", finalize, terminal=True))
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "check_stop_condition")
    graph.add_conditional_edges("check_stop_condition", should_continue, {"produce": "produce_next_event", "finalize": "finalize"})
    graph.add_edge("produce_next_event", "check_stop_condition")
    graph.add_edge("finalize", END)

    persisted = load_graph_checkpoint(db, task=task, graph_name=graph_name)
    initial_state: NovelProductionState = {
        **(persisted.state if persisted is not None else {}),
        "auto_run_id": str(auto_run.id),
    }
    final_state = graph.compile().invoke(initial_state)
    save_graph_checkpoint(
        db,
        task=task,
        graph_name=graph_name,
        node_name="finalize",
        state=final_state,
        status=(
            "waiting"
            if final_state.get("stop_reason") == "waiting_event_child"
            else "completed"
        ),
    )
    return {
        "agent": "NovelProductionAgent",
        "graph": "NovelProductionGraph",
        "auto_run_id": str(auto_run.id),
        "status": auto_run.status,
        "stage": auto_run.stage,
        "target_words": auto_run.target_words,
        "current_words": auto_run.current_words,
        "produced_event_count": auto_run.produced_event_count,
        "max_event_count": auto_run.max_event_count,
        "stop_reason": final_state.get("stop_reason", ""),
        "events": final_state.get("events", []),
        "deferred": final_state.get("stop_reason") == "waiting_event_child",
        "child_task_id": final_state.get("pending_child_task_id", ""),
        "contract": agent_contract(
            "NovelProductionAgent",
            "novel_production_graph",
            status=(
                "waiting"
                if final_state.get("stop_reason") == "waiting_event_child"
                else "success"
            ),
        ),
    }
