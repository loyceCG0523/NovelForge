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
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.models.story_event import StoryEvent
from app.services.agents.story_planning_agent import get_story_bible_context
from worker.graphs.event_generation_graph import run_event_generation_graph


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
        count = 8
    return max(6, min(count, 12))


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


def _create_child_event_task(
    db: Session,
    parent_task: GenerationTask,
    novel: Novel,
    run: AutoNovelRun,
    event_index: int,
    chapter_count: int,
) -> GenerationTask:
    """创建不会入队的子任务，用于复用现有 EventGenerationGraph 并保留审计记录。"""
    child_task = GenerationTask(
        novel_id=novel.id,
        task_type="generate_story_event",
        status="running",
        progress=5,
        result_payload={
            "input": {
                "source": "novel_production_graph",
                "auto_run_id": str(run.id),
                "parent_task_id": str(parent_task.id),
                "production_event_index": event_index,
                "chapter_count": chapter_count,
            },
            "agent": "NovelProductionAgent.child_event",
            "graph_status": "由整本书生产总控触发剧情事件生成",
        },
    )
    db.add(child_task)
    db.commit()
    db.refresh(child_task)
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
        task_input.get("chapter_count_per_event") or (auto_run.payload or {}).get("chapter_count_per_event")
    )
    max_event_count = _clip_event_count(task_input.get("max_event_count") or auto_run.max_event_count)

    def initialize(state: NovelProductionState) -> NovelProductionState:
        _set_parent_progress(db, task, auto_run, 8, "检查作品设定与生产状态")
        get_story_bible_context(db, novel)
        _sync_run_stats(
            db,
            novel,
            auto_run,
            {
                "chapter_count_per_event": chapter_count_per_event,
                "pause_requested": False,
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
            "chapter_count_per_event": chapter_count_per_event,
            "events": (auto_run.payload or {}).get("events", []),
        }

    def produce_next_event(state: NovelProductionState) -> NovelProductionState:
        event_number = int(state.get("produced_event_count", 0)) + 1
        progress = 10 + int(80 * min(event_number, max_event_count) / max(max_event_count, 1))
        _set_parent_progress(db, task, auto_run, progress, f"正在生成第 {event_number} 个剧情事件")

        child_task = _create_child_event_task(
            db=db,
            parent_task=task,
            novel=novel,
            run=auto_run,
            event_index=event_number,
            chapter_count=chapter_count_per_event,
        )
        try:
            output = run_event_generation_graph(db=db, task=child_task, novel=novel)
            child_task.status = "completed"
            child_task.progress = 100
            child_task.result_payload = {
                **(child_task.result_payload or {}),
                "agent": "StoryPlanningAgent",
                "output": output,
            }
            story_event_id = output.get("story_event_id") or ""
            story_event = db.get(StoryEvent, UUID(story_event_id)) if story_event_id else None
            auto_run.current_event_id = story_event.id if story_event else None
            auto_run.produced_event_count = event_number
            auto_run.last_error = ""
            events = [
                *state.get("events", []),
                {
                    "story_event_id": story_event_id,
                    "title": (output.get("event_plan") or {}).get("event_title", ""),
                    "chapter_count": len(output.get("generated_chapters", [])),
                    "word_count": sum(item.get("word_count", 0) for item in output.get("generated_chapters", [])),
                },
            ]
            _sync_run_stats(db, novel, auto_run, {"events": events})
            db.commit()
            return {
                **state,
                "current_words": auto_run.current_words,
                "produced_event_count": event_number,
                "events": events,
            }
        except Exception as exc:
            db.rollback()
            child_task = db.get(GenerationTask, child_task.id)
            if child_task is not None:
                child_task.status = "failed"
                child_task.progress = 100
                child_task.error_message = str(exc)
            auto_run.status = "failed"
            auto_run.stage = "event_failed"
            auto_run.last_error = str(exc)
            db.commit()
            raise

    def check_stop_condition(state: NovelProductionState) -> NovelProductionState:
        db.refresh(auto_run)
        _sync_run_stats(db, novel, auto_run)
        if auto_run.status == "paused" or (auto_run.payload or {}).get("pause_requested"):
            return {**state, "stop_reason": "paused", "current_words": auto_run.current_words}
        if auto_run.current_words >= auto_run.target_words:
            return {**state, "stop_reason": "target_words_reached", "current_words": auto_run.current_words}
        if auto_run.produced_event_count >= auto_run.max_event_count:
            return {**state, "stop_reason": "max_event_count_reached", "current_words": auto_run.current_words}
        return {**state, "stop_reason": ""}

    def finalize(state: NovelProductionState) -> NovelProductionState:
        stop_reason = state.get("stop_reason") or "completed"
        if stop_reason == "paused":
            auto_run.status = "paused"
            auto_run.stage = "paused_after_event"
        elif stop_reason == "target_words_reached":
            auto_run.status = "completed"
            auto_run.stage = "completed"
            novel.status = "completed"
        elif stop_reason == "max_event_count_reached":
            auto_run.status = "paused"
            auto_run.stage = "guardrail_paused"
        else:
            auto_run.status = "completed"
            auto_run.stage = "completed"
        auto_run.current_words = _current_word_count(db, novel)
        auto_run.payload = {
            **(auto_run.payload or {}),
            "stop_reason": stop_reason,
            "pause_requested": False,
        }
        db.commit()
        _set_parent_progress(db, task, auto_run, 95, auto_run.stage)
        return {**state, "current_words": auto_run.current_words, "stop_reason": stop_reason}

    def should_continue(state: NovelProductionState) -> str:
        return "finalize" if state.get("stop_reason") else "produce"

    graph = StateGraph(NovelProductionState)
    graph.add_node("initialize", initialize)
    graph.add_node("produce_next_event", produce_next_event)
    graph.add_node("check_stop_condition", check_stop_condition)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "check_stop_condition")
    graph.add_conditional_edges("check_stop_condition", should_continue, {"produce": "produce_next_event", "finalize": "finalize"})
    graph.add_edge("produce_next_event", "check_stop_condition")
    graph.add_edge("finalize", END)

    final_state = graph.compile().invoke({"auto_run_id": str(auto_run.id)})
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
    }
