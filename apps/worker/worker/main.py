"""NovelForge 后台任务 Worker。

API 负责创建任务并写入 Redis 队列：Worker 独立消费队列并执行耗时的 Agent 工作。
当前版本已支持基于用户 LLM 配置的真实章节生成、质量修订和 LangGraph 编排；
未配置 API Key 时，部分生成流程会使用模拟内容兜底，方便本地打通任务闭环。
"""

import argparse
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from redis import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session


# Worker 是独立 Python 包，为了复用 API 层的配置、模型和服务，这里把 apps/api 加入导入路径。
API_DIR = Path(__file__).resolve().parents[2] / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.chapter import Chapter  # noqa: E402
from app.models.auto_novel_run import AutoNovelRun  # noqa: E402
from app.models.event_chapter_plan import EventChapterPlan  # noqa: E402
from app.models.generation_task import GenerationTask  # noqa: E402
from app.models.novel import Novel  # noqa: E402
from app.models.sample_analysis import SampleAnalysis  # noqa: E402
from app.models.story_event import StoryEvent  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.agent_contracts import (  # noqa: E402
    AgentExecutionError,
    AgentFailure,
    agent_contract,
    agent_input_contract,
)
from app.services.agents.quality_agent import review_story_event_quality  # noqa: E402
from app.services.agents.story_planning_agent import build_or_refresh_story_bible  # noqa: E402
from app.services.chapter_context_builder import build_chapter_context  # noqa: E402
from app.services.chapter_progress import (  # noqa: E402
    compact_next_chapter_boundary,
    compact_story_event_for_chapter,
)
from app.services.chapter_memory_bundle import sync_chapter_memory_bundle  # noqa: E402
from app.services.chapter_pipeline import (  # noqa: E402
    ChapterPipelineRequest,
    run_chapter_pipeline,
)
from app.services.agent_orchestrator import (  # noqa: E402
    aggregate_memory_batch_progress,
    enqueue_chapter_memory_tasks,
)
from app.services.event_revision_service import (  # noqa: E402
    retry_failed_event_revision_chapters,
    review_and_repair_story_event,
)
from app.services.llm_client import build_llm_config, build_review_llm_config  # noqa: E402
from app.services.sample_experience_builder import build_sample_experience_document  # noqa: E402
from app.services.sample_passage_indexer import index_sample_experiences  # noqa: E402
from app.services.novel_production_handoff import (  # noqa: E402
    complete_event_child_handoff,
    fail_event_child_handoff,
)
from app.services.task_events import emit_task_event  # noqa: E402
from worker.graphs.event_generation_graph import run_event_generation_graph  # noqa: E402
from worker.graphs.novel_production_graph import run_novel_production_graph  # noqa: E402


TEST_FIRST_CHAPTER_QUALITY_REPAIR_ATTEMPTS = 4

TASK_AGENT_NAMES = {
    "generate_chapter": "ChapterWritingAgent",
    "generate_story_event": "StoryPlanningAgent",
    "produce_novel": "NovelProductionAgent",
    "continue_story_event": "StoryPlanningAgent",
    "check_story_event_quality": "QualityAgent",
    "build_story_bible": "StoryPlanningAgent",
    "analyze_sample": "SampleAnalysisAgent",
    "sync_chapter_memory": "MemoryAgent",
    "retry_event_revision": "EventRevisionRetryAgent",
}


def is_test_run(task_input: dict) -> bool:
    """判断续跑任务是否仍属于测试模式。"""
    pacing = task_input.get("production_pacing") or {}
    return str(pacing.get("production_mode") or "").strip() == "test_run"


def get_redis_client() -> Redis:
    """创建 Redis 客户端；队列名由统一配置读取，避免 API 和 Worker 不一致。"""
    return Redis.from_url(settings.redis_url, decode_responses=True)


def processing_queue_name(queue_name: str) -> str:
    """可靠消费使用的处理中列表；只有任务终态落库后才从这里确认删除。"""
    return f"{queue_name}:processing"


def claim_queued_task(db: Session, task_id: UUID) -> bool:
    """数据库原子认领，抵御 Redis 重复通知和多 Worker 竞争。"""
    result = db.execute(
        update(GenerationTask)
        .where(
            GenerationTask.id == task_id,
            GenerationTask.status == "queued",
        )
        .values(status="running", progress=10, error_message="")
    )
    db.commit()
    return bool(result.rowcount)


class TaskHeartbeat:
    """任务执行期间持续刷新租约，Worker 崩溃后恢复器才会接管。"""

    def __init__(self, task_id: UUID) -> None:
        self.task_id = task_id
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"task-heartbeat-{task_id}",
            daemon=True,
        )

    def _run(self) -> None:
        interval = max(2, int(settings.task_heartbeat_seconds))
        while not self.stop_event.wait(interval):
            try:
                with SessionLocal() as heartbeat_db:
                    heartbeat_db.execute(
                        update(GenerationTask)
                        .where(
                            GenerationTask.id == self.task_id,
                            GenerationTask.status == "running",
                        )
                        .values(updated_at=func.now())
                    )
                    heartbeat_db.commit()
            except Exception as exc:
                print(f"Task heartbeat failed: {self.task_id} - {exc}")

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=3)


def _queue_handles_task(queue_name: str, task: GenerationTask) -> bool:
    is_memory_queue = queue_name == settings.memory_task_queue
    return (task.task_type == "sync_chapter_memory") == is_memory_queue


def recover_worker_queue(redis_client: Redis, queue_name: str) -> dict[str, int]:
    """恢复崩溃租约、清理已完成 processing 项，并补发孤儿 queued 任务。"""
    processing_queue = processing_queue_name(queue_name)
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=max(30, int(settings.task_lease_seconds))
    )
    recovered = 0
    acknowledged = 0
    notified = 0
    with SessionLocal() as db:
        processing_ids = list(dict.fromkeys(redis_client.lrange(processing_queue, 0, -1)))
        for raw_task_id in processing_ids:
            try:
                task_id = UUID(str(raw_task_id))
            except (TypeError, ValueError):
                redis_client.lrem(processing_queue, 0, raw_task_id)
                acknowledged += 1
                continue
            task = db.get(GenerationTask, task_id)
            if task is not None and task.status in {"completed", "failed"}:
                task_input = (task.result_payload or {}).get("input") or {}
                if (
                    task.task_type == "generate_story_event"
                    and task_input.get("parent_task_id")
                ):
                    if task.status == "completed":
                        complete_event_child_handoff(
                            db,
                            child_task=task,
                            output=(task.result_payload or {}).get("output") or {},
                        )
                    else:
                        fail_event_child_handoff(
                            db,
                            child_task=task,
                            error=task.error_message or "剧情事件子任务失败",
                        )
            if task is None or task.status in {"completed", "failed", "cancelled"}:
                redis_client.lrem(processing_queue, 0, raw_task_id)
                acknowledged += 1
                continue
            updated_at = task.updated_at
            if updated_at is not None and updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if task.status == "running" and updated_at and updated_at >= cutoff:
                continue
            task.status = "queued"
            task.error_message = ""
            task.result_payload = {
                **(task.result_payload or {}),
                "queue_recovery": {
                    "recovered_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "worker_lease_expired",
                    "retryable": True,
                },
            }
            db.commit()
            redis_client.lrem(processing_queue, 0, raw_task_id)
            if redis_client.lpos(queue_name, str(task.id)) is None:
                redis_client.rpush(queue_name, str(task.id))
            recovered += 1

        stale_running = db.scalars(
            select(GenerationTask).where(
                GenerationTask.status == "running",
                GenerationTask.updated_at < cutoff,
            )
        ).all()
        for task in stale_running:
            if not _queue_handles_task(queue_name, task):
                continue
            task.status = "queued"
            task.error_message = ""
            task.result_payload = {
                **(task.result_payload or {}),
                "queue_recovery": {
                    "recovered_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "orphaned_running_task",
                    "retryable": True,
                },
            }
            db.commit()
            if redis_client.lpos(queue_name, str(task.id)) is None:
                redis_client.rpush(queue_name, str(task.id))
            recovered += 1

        queued_tasks = db.scalars(
            select(GenerationTask).where(GenerationTask.status == "queued")
        ).all()
        for task in queued_tasks:
            if not _queue_handles_task(queue_name, task):
                continue
            task_id = str(task.id)
            if (
                redis_client.lpos(queue_name, task_id) is None
                and redis_client.lpos(processing_queue, task_id) is None
            ):
                redis_client.rpush(queue_name, task_id)
                notified += 1
    return {
        "recovered": recovered,
        "acknowledged": acknowledged,
        "notified": notified,
    }


def mark_task(
    db: Session,
    task: GenerationTask,
    status: str,
    progress: int,
    result_payload: dict | None = None,
    error_message: str = "",
) -> None:
    """更新任务状态、进度和结果。

    所有任务状态都集中通过这个函数落库，方便后续加审计日志或失败重试。
    """
    task.status = status
    task.progress = progress
    task.error_message = error_message
    if result_payload is not None:
        task.result_payload = result_payload
    db.commit()
    db.refresh(task)
    event_title = str((result_payload or {}).get("graph_status") or f"任务状态：{status}")
    emit_task_event(
        db,
        task,
        event_type="task_status",
        step_key=str((result_payload or {}).get("step_key") or f"task_{task.task_type}"),
        status="completed" if status == "completed" else ("failed" if status == "failed" else "running"),
        title=event_title,
        message=error_message,
        progress=progress,
        payload={"task_status": status},
    )


def build_simulated_chapter(context: dict) -> tuple[str, str, str]:
    """基于 ChapterContext 生成模拟章节。

    这里不是最终的小说生成能力，而是为了验证“上下文快照 -> 章节草稿”的数据闭环。
    真正接入 LLM 时，可以把 context 交给 PromptBuilder，再由 LLMClient 返回标题、摘要和正文。
    """
    novel = context["novel"]
    brief = novel.get("brief", {})
    target = context["target"]
    chapter_index = target["chapter_index"]
    recent_chapters = context["recent_chapters"]
    memories = context["memories"]
    foreshadowing = context["foreshadowing"]
    review_issues = context["review_issues"]
    guidance = context["generation_guidance"]
    chapter_word_range = guidance.get("chapter_word_range") or {"min": 2500, "max": 2800}

    title = f"第 {chapter_index} 章 灰塔回声"
    summary = (
        f"本章基于《{novel['title']}》的起始需求与上下文快照推进剧情，"
        f"承接最近 {len(recent_chapters)} 章，并参考 {len(memories)} 条结构化记忆。"
    )

    recent_text = "暂无最近章节，当前章节将承担建立主线悬念和世界规则的作用。"
    if recent_chapters:
        recent_text = "；".join(
            f"第 {chapter['chapter_index']} 章《{chapter['title'] or '未命名'}》"
            for chapter in recent_chapters
        )

    memory_text = "暂无结构化记忆。"
    if memories:
        memory_text = "、".join(memory["entity_name"] for memory in memories[:5])

    foreshadowing_text = "暂无待推进伏笔。"
    if foreshadowing:
        foreshadowing_text = "、".join(item["title"] for item in foreshadowing[:5])

    risk_text = "暂无开放风险。"
    if review_issues:
        risk_text = "；".join(issue["message"] for issue in review_issues[:3])

    content = "\n\n".join(
        [
            title,
            f"夜色压在{brief.get('worldview', novel.get('genre') or '灰塔边境城市')}上，像一层没有温度的玻璃。",
            f"本章目标：{guidance['chapter_goal']}",
            f"本章字数约束：{chapter_word_range.get('min', 2500)}-{chapter_word_range.get('max', 2800)} 字。",
            f"连续性承接：{recent_text}",
            f"本章需要参考的结构化记忆：{memory_text}",
            f"本章可推进的伏笔：{foreshadowing_text}",
            f"生成前风险提醒：{risk_text}",
            f"{brief.get('protagonist', '主角')}在新的场景中再次面对旧线索。系统没有让他突然获得答案，而是让他通过动作、观察和选择逐步逼近真相。",
            f"剧情继续向“{brief.get('plot_direction', novel.get('premise') or '主线悬念')}”推进，同时遵守风格约束：{context['constraints'].get('style_reference') or '保持克制、具体、少解释'}。",
            "这是基于 ChapterContextBuilder 生成的模拟章节草稿。后续接入 LLM 时，将把同一份上下文快照转换为 prompt，并保留当前快照用于追踪和复盘。",
        ]
    )
    return title, summary, content


def get_target_chapter_index(db: Session, task: GenerationTask, novel: Novel) -> int:
    """确定本次任务要生成或重写哪一章。"""
    if task.chapter_id:
        chapter = db.get(Chapter, task.chapter_id)
        if chapter is None or chapter.novel_id != novel.id:
            raise ValueError("Task chapter does not belong to the novel")
        return chapter.chapter_index

    task_input = (task.result_payload or {}).get("input", {})
    requested_index = task_input.get("chapter_index")
    if requested_index:
        return int(requested_index)

    return (db.scalar(select(func.max(Chapter.chapter_index)).where(Chapter.novel_id == novel.id)) or 0) + 1


def handle_generate_chapter(
    db: Session,
    task: GenerationTask,
    novel: Novel,
    defer_event_quality: bool = False,
) -> dict:
    """通过唯一 ChapterPipeline 执行单章生成。"""
    target_chapter_index = get_target_chapter_index(db, task, novel)
    task_input = (task.result_payload or {}).get("input", {})
    owner = db.get(User, novel.owner_id)
    preferences = owner.preferences if owner else {}
    llm_config = build_llm_config(preferences)
    review_llm_config = build_review_llm_config(preferences)
    chapter = db.get(Chapter, task.chapter_id) if task.chapter_id else None
    story_event_id = None
    if task_input.get("story_event_id"):
        try:
            story_event_id = UUID(str(task_input["story_event_id"]))
        except (TypeError, ValueError):
            story_event_id = None
    pipeline = run_chapter_pipeline(
        db=db,
        task=task,
        novel=novel,
        request=ChapterPipelineRequest(
            target_chapter_index=target_chapter_index,
            task_input=task_input,
            simulated_builder=build_simulated_chapter,
            llm_config=llm_config,
            review_llm_config=review_llm_config,
            existing_chapter=chapter,
            story_event_id=story_event_id,
            progress=int(task.progress or 0),
            force_title=bool(task_input.get("force_regenerate")),
            memory_expected_total=int(task_input.get("event_chapter_count") or 1),
            strict_quality_gate=bool(
                task_input.get(
                    "strict_quality_gate",
                    review_llm_config is not None,
                )
            ),
        ),
    )
    output = pipeline.to_output()
    output["event_quality"] = {}
    pipeline_failure = (pipeline.contract or {}).get("failure") or {}
    output["quality_revision_pause"] = (
        {
            "story_event_id": str(story_event_id or ""),
            "chapter_id": str(pipeline.chapter.id),
            "chapter_index": pipeline.chapter.chapter_index,
            "scope": "chapter",
            "reason": pipeline_failure.get("code") or "chapter_quality_revision_required",
            "stage": pipeline_failure.get("stage") or "chapter_quality_gate",
            "error": (
                pipeline_failure.get("message")
                or pipeline.chapter_review.get("error", "")
            ),
        }
        if pipeline.requires_quality_revision
        else {}
    )
    if defer_event_quality or pipeline.requires_word_revision or pipeline.requires_quality_revision:
        return output
    event_quality = {}
    if story_event_id is not None:
        story_event = db.get(StoryEvent, story_event_id)
        if story_event is not None and story_event.novel_id == novel.id:
            try:
                event_quality = review_story_event_quality(
                    db=db,
                    novel=novel,
                    story_event=story_event,
                    llm_config=review_llm_config,
                )
            except Exception as exc:
                db.rollback()
                event_quality = {"error": str(exc)}
    output["event_quality"] = event_quality
    return output


def _plan_to_generation_input(
    story_event: StoryEvent,
    plan: EventChapterPlan,
    base_input: dict,
    next_plan: EventChapterPlan | None = None,
) -> dict:
    """把事件章节计划转换为 generate_chapter 可读取的输入。"""
    return {
        **base_input,
        "source": base_input.get("source") or "continue_story_event",
        "story_event_id": str(story_event.id),
        "event_plan_id": str(plan.id),
        "chapter_index": plan.chapter_index,
        "event_chapter_count": story_event.planned_chapter_count,
        "force_regenerate": True,
        "story_event": compact_story_event_for_chapter(story_event.payload or {}),
        "chapter_plan": plan.payload or {
            "chapter_index": plan.chapter_index,
            "title": plan.title,
            "function": plan.function,
            "core_event": plan.core_event,
            "ending_hook": plan.ending_hook,
        },
        "next_chapter_boundary": compact_next_chapter_boundary(
            (
                next_plan.payload
                or {
                    "chapter_index": next_plan.chapter_index,
                    "title": next_plan.title,
                    "function": next_plan.function,
                    "core_event": next_plan.core_event,
                    "ending_hook": next_plan.ending_hook,
                }
            )
            if next_plan is not None
            else None
        ),
    }


def _apply_rolling_next_plan(
    db: Session,
    story_event: StoryEvent,
    current_plan: EventChapterPlan,
    next_plan: EventChapterPlan | None,
    chapter_progress: dict,
) -> None:
    """把章节实际越界后给出的安全重排同步到下一章计划。"""
    if next_plan is None or not chapter_progress.get("consumed_next_beats"):
        return
    revised = chapter_progress.get("revised_next_chapter_plan") or {}
    if not revised:
        return

    next_plan.title = revised.get("title") or next_plan.title
    next_plan.function = revised.get("function") or next_plan.function
    next_plan.core_event = revised.get("core_event") or next_plan.core_event
    next_plan.ending_hook = revised.get("ending_hook") or next_plan.ending_hook
    next_plan.payload = {
        **(next_plan.payload or {}),
        **revised,
        "rolling_revision": {
            "source_chapter_index": current_plan.chapter_index,
            "consumed_next_beats": chapter_progress.get("consumed_next_beats") or [],
        },
    }

    event_payload = dict(story_event.payload or {})
    chapter_plans = [dict(item) for item in (event_payload.get("chapter_plans") or [])]
    for index, item in enumerate(chapter_plans):
        if int(item.get("chapter_index") or 0) == next_plan.chapter_index:
            chapter_plans[index] = {**item, **revised}
            break
    if chapter_plans:
        story_event.payload = {**event_payload, "chapter_plans": chapter_plans}
    db.commit()


def handle_continue_story_event(db: Session, task: GenerationTask, novel: Novel) -> dict:
    """按已有 StoryEvent 章节计划，从指定章节继续生成。"""
    task_input = (task.result_payload or {}).get("input", {})
    story_event_id = task_input.get("story_event_id")
    if not story_event_id:
        raise ValueError("继续剧情事件任务缺少 story_event_id")

    story_event = db.get(StoryEvent, UUID(str(story_event_id)))
    if story_event is None or story_event.novel_id != novel.id:
        raise ValueError("剧情事件不存在，或不属于当前作品")

    from_chapter_index = int(task_input.get("from_chapter_index") or story_event.start_chapter_index or 1)
    quality_only = bool(task_input.get("quality_only"))
    plans = db.scalars(
        select(EventChapterPlan)
        .where(
            EventChapterPlan.story_event_id == story_event.id,
            EventChapterPlan.chapter_index >= from_chapter_index,
        )
        .order_by(EventChapterPlan.chapter_index.asc())
    ).all()
    if not plans and not quality_only:
        return {
            "agent": "continue_story_event",
            "story_event_id": str(story_event.id),
            "generated_chapters": [],
            "contract": agent_contract(
                "StoryPlanningAgent",
                "continue_story_event",
            ),
            "note": "指定章节之后没有可继续生成的章节计划。",
        }
    if quality_only:
        plans = []

    story_event.status = "generating"
    db.commit()

    generated = []
    memory_sync = []
    continuity_reviews = []
    auto_review_history = []
    word_revision_pause = None
    quality_revision_pause = None
    immediate_pause = None
    auto_run_id = task_input.get("auto_run_id")

    def pause_requested() -> bool:
        if not auto_run_id:
            return False
        try:
            auto_run = db.get(AutoNovelRun, UUID(str(auto_run_id)))
        except (TypeError, ValueError):
            return False
        return bool(auto_run and (auto_run.status == "paused" or (auto_run.payload or {}).get("pause_requested")))

    for offset, plan in enumerate(plans):
        if pause_requested():
            immediate_pause = {
                "story_event_id": str(story_event.id),
                "next_chapter_index": plan.chapter_index,
                "chapters_generated": len(generated),
            }
            break
        progress = 15 + int(75 * (offset / max(len(plans), 1)))
        next_plan = plans[offset + 1] if offset + 1 < len(plans) else None
        task.result_payload = {
            **(task.result_payload or {}),
            "input": _plan_to_generation_input(story_event, plan, task_input, next_plan),
            "graph_status": f"正在从第 {plan.chapter_index} 章继续生成剧情事件",
        }
        mark_task(db, task, "running", progress, result_payload=task.result_payload)

        output = handle_generate_chapter(db=db, task=task, novel=novel, defer_event_quality=True)
        if not output.get("requires_word_revision"):
            _apply_rolling_next_plan(
                db,
                story_event,
                plan,
                next_plan,
                output.get("chapter_progress") or {},
            )
        generated.append(
            {
                "chapter_id": output["chapter_id"],
                "chapter_index": output["chapter_index"],
                "title": output["title"],
                "word_count": output["word_count"],
                "chapter_progress": output.get("chapter_progress") or {},
            }
        )
        memory_sync.append(output.get("memory_sync", {}))
        continuity_reviews.append(output.get("continuity_review", {}))
        auto_review_history.extend((output.get("auto_review_handling") or {}).get("history", []))
        if pause_requested():
            if next_plan is not None:
                immediate_pause = {
                    "story_event_id": str(story_event.id),
                    "next_chapter_index": next_plan.chapter_index,
                    "chapters_generated": len(generated),
                }
                break
        if output.get("requires_word_revision"):
            word_revision_pause = {
                "story_event_id": str(story_event.id),
                "chapter_id": output["chapter_id"],
                "chapter_index": output["chapter_index"],
                "word_count": output["word_count"],
                "word_guard": output.get("word_guard") or {},
            }
            break
        if output.get("requires_quality_revision"):
            quality_revision_pause = output.get("quality_revision_pause") or {
                "story_event_id": str(story_event.id),
                "chapter_id": output["chapter_id"],
                "chapter_index": output["chapter_index"],
                "remaining_open": (output.get("auto_review_handling") or {}).get("remaining_open", 0),
                "max_attempts": TEST_FIRST_CHAPTER_QUALITY_REPAIR_ATTEMPTS,
            }
            break

    repaired_count = len([item for item in auto_review_history if item.get("status") == "resolved"])
    generated_count = db.scalar(
        select(func.count())
        .select_from(EventChapterPlan)
        .where(EventChapterPlan.story_event_id == story_event.id, EventChapterPlan.status.in_(["generated", "revised"]))
    )
    story_event.generated_chapter_count = generated_count or 0
    story_event.auto_repair_count = (story_event.auto_repair_count or 0) + repaired_count
    story_event.status = (
        "paused"
        if word_revision_pause or quality_revision_pause or immediate_pause
        else ("reviewing" if story_event.generated_chapter_count >= story_event.planned_chapter_count else "generating")
    )
    if immediate_pause:
        story_event.payload = {**(story_event.payload or {}), "immediate_pause": immediate_pause}
    if quality_revision_pause:
        story_event.payload = {**(story_event.payload or {}), "quality_revision_pause": quality_revision_pause}
    db.commit()
    quality_result = {}
    event_revision = {}
    if (
        not word_revision_pause
        and not immediate_pause
        and story_event.generated_chapter_count >= story_event.planned_chapter_count
    ):
        owner = db.get(User, novel.owner_id)
        preferences = owner.preferences if owner else {}
        writer_llm_config = build_llm_config(preferences)
        review_llm_config = build_review_llm_config(preferences)
        event_revision = review_and_repair_story_event(
            db=db,
            novel=novel,
            story_event=story_event,
            llm_config=review_llm_config,
            task=task,
            revision_llm_config=writer_llm_config,
        )
        quality_result = event_revision.get("final_review", {})
        story_event.auto_repair_count = (story_event.auto_repair_count or 0) + int(
            event_revision.get("repaired_chapter_count") or 0
        )
        story_event.remaining_open_risks = int(event_revision.get("remaining_open_risks") or 0)
        if story_event.remaining_open_risks:
            quality_revision_pause = {
                "story_event_id": str(story_event.id),
                "remaining_open": story_event.remaining_open_risks,
                "repair_rounds": event_revision.get("repair_rounds", 0),
                "max_attempts": event_revision.get("max_repair_rounds", 1),
                "scope": "event",
            }
            story_event.status = "paused"
            story_event.payload = {
                **(story_event.payload or {}),
                "quality_revision_pause": quality_revision_pause,
                "event_revision": event_revision,
            }
        else:
            story_event.status = "completed"
            story_event.payload = {
                **(story_event.payload or {}),
                "quality_revision_pause": {},
                "event_revision": event_revision,
            }
        revised_chapter_indexes = {
            int(chapter_index)
            for package in event_revision.get("repair_packages", [])
            if package.get("status") in {"resolved", "partial"}
            for chapter_index in package.get("chapter_indexes", [])
        }
        final_plans = db.scalars(
            select(EventChapterPlan)
            .where(
                EventChapterPlan.story_event_id == story_event.id,
                EventChapterPlan.chapter_id.is_not(None),
                EventChapterPlan.chapter_index.in_(revised_chapter_indexes),
            )
            .order_by(EventChapterPlan.chapter_index.asc())
        ).all() if revised_chapter_indexes else []
        final_chapters = [
            chapter
            for chapter in (db.get(Chapter, plan.chapter_id) for plan in final_plans)
            if chapter is not None
        ]
        background_tasks = enqueue_chapter_memory_tasks(
            db,
            novel=novel,
            chapters=final_chapters,
            source_task=task,
            story_event_id=story_event.id,
            source_progress=95,
            sync_reason="event_revision_resync",
            expected_total=story_event.planned_chapter_count,
        )
        memory_sync.extend(
            {
                "task_id": str(memory_task.id),
                "chapter_id": str(memory_task.chapter_id),
                "status": "queued",
                "background": True,
                "sync_reason": "event_revision_resync",
            }
            for memory_task in background_tasks
        )
        db.commit()

    if auto_run_id:
        auto_run = db.get(AutoNovelRun, UUID(str(auto_run_id)))
        if auto_run is not None and auto_run.novel_id == novel.id:
            auto_run.current_words = db.scalar(
                select(func.coalesce(func.sum(Chapter.word_count), 0)).where(Chapter.novel_id == novel.id)
            ) or 0
            auto_run.current_event_id = story_event.id
            auto_run.status = "paused"
            completed_test_run = (
                is_test_run(task_input)
                and not word_revision_pause
                and not quality_revision_pause
                and not immediate_pause
                and story_event.generated_chapter_count >= story_event.planned_chapter_count
            )
            auto_run.stage = (
                "word_revision_required"
                if word_revision_pause
                else (
                    "quality_revision_required"
                    if quality_revision_pause
                    else (
                        "paused_immediately"
                        if immediate_pause
                        else ("test_run_completed" if completed_test_run else "event_completed_waiting_next")
                    )
                )
            )
            auto_run.last_error = ""
            if word_revision_pause:
                auto_run.payload = {
                    **(auto_run.payload or {}),
                    "stop_reason": "chapter_word_revision_required",
                    "pause_requested": False,
                    "word_revision_pause": word_revision_pause,
                }
            elif quality_revision_pause:
                auto_run.payload = {
                    **(auto_run.payload or {}),
                    "stop_reason": "event_quality_revision_required",
                    "pause_requested": False,
                    "quality_revision_pause": quality_revision_pause,
                }
            elif immediate_pause:
                auto_run.payload = {
                    **(auto_run.payload or {}),
                    "stop_reason": "paused_immediately",
                    "pause_requested": False,
                    "immediate_pause": immediate_pause,
                }
            else:
                pending_event_number = int((auto_run.payload or {}).get("pending_event_number") or 0)
                if pending_event_number:
                    auto_run.produced_event_count = max(auto_run.produced_event_count, pending_event_number)
                auto_run.payload = {
                    **(auto_run.payload or {}),
                    "human_loop_status": "event_generated",
                    "pending_human_event_id": "",
                    "last_confirmed_event_id": str(story_event.id),
                    "pause_requested": False,
                    "stop_reason": "test_run_event_completed" if completed_test_run else "word_revision_completed",
                    "word_revision_pause": {},
                    "quality_revision_pause": {},
                    "pending_event_number": 0,
                }
            db.commit()

    return {
        "agent": "StoryPlanningAgent",
        "story_event_id": str(story_event.id),
        "from_chapter_index": from_chapter_index,
        "generated_chapters": generated,
        "memory_sync": memory_sync,
        "continuity_reviews": continuity_reviews,
        "auto_review_history": auto_review_history,
        "event_quality": quality_result,
        "event_revision": event_revision,
        "remaining_open_risks": story_event.remaining_open_risks,
        "requires_word_revision": bool(word_revision_pause),
        "word_revision_pause": word_revision_pause or {},
        "requires_quality_revision": bool(quality_revision_pause),
        "quality_revision_pause": quality_revision_pause or {},
        "pause_requested": bool(immediate_pause),
        "immediate_pause": immediate_pause or {},
        "contract": agent_contract(
            "StoryPlanningAgent",
            "continue_story_event",
            status=(
                "waiting"
                if word_revision_pause or quality_revision_pause or immediate_pause
                else "success"
            ),
        ),
    }


def handle_check_story_event_quality(db: Session, task: GenerationTask, novel: Novel) -> dict:
    """手动触发事件级质量审校，并刷新事件级风险。"""
    task_input = (task.result_payload or {}).get("input", {})
    story_event_id = task_input.get("story_event_id")
    if not story_event_id:
        raise ValueError("事件级审校任务缺少 story_event_id")

    story_event = db.get(StoryEvent, UUID(str(story_event_id)))
    if story_event is None or story_event.novel_id != novel.id:
        raise ValueError("剧情事件不存在，或不属于当前作品")

    owner = db.get(User, novel.owner_id)
    llm_config = build_review_llm_config(owner.preferences if owner else {})
    result = review_story_event_quality(db=db, novel=novel, story_event=story_event, llm_config=llm_config)
    return {
        "agent": "QualityAgent",
        "story_event_id": str(story_event.id),
        **result,
        "contract": agent_contract(
            "QualityAgent",
            "story_event_quality",
            generation_mode="llm" if llm_config is not None else "rules",
        ),
    }


def handle_build_story_bible(db: Session, task: GenerationTask, novel: Novel) -> dict:
    """根据起始需求文档生成或刷新作品圣经。"""
    task_input = (task.result_payload or {}).get("input", {})
    owner = db.get(User, novel.owner_id)
    llm_config = build_llm_config(owner.preferences if owner else {})
    story_bible, generation_mode = build_or_refresh_story_bible(
        db=db,
        novel=novel,
        llm_config=llm_config,
        extra_input=task_input,
    )
    return {
        "agent": "StoryPlanningAgent",
        "story_bible_id": str(story_bible.id),
        "version": story_bible.version,
        "status": story_bible.status,
        "generation_mode": generation_mode,
        "summary": story_bible.summary,
        "contract": agent_contract(
            "StoryPlanningAgent",
            "story_bible",
            generation_mode=generation_mode,
        ),
    }


def handle_analyze_sample(db: Session, task: GenerationTask, novel: Novel | None = None) -> dict:
    """最多十路并行拆解原作，生成经验文档并建立经验卡索引。"""
    task_input = (task.result_payload or {}).get("input", {})
    analysis_id = task_input.get("analysis_id")
    if not analysis_id:
        raise ValueError("样本分析任务缺少 analysis_id")

    analysis = db.get(SampleAnalysis, UUID(str(analysis_id)))
    if analysis is None:
        raise ValueError("样本分析记录不存在")

    analysis.status = "running"
    analysis.error_message = ""
    analysis.chunk_count = 0
    analysis.analyzed_chunk_count = 0
    analysis.summary = "正在拆分样本，准备并行总结剧情与表达经验。"
    analysis.report = {
        "schema_version": "sample_experience.v1",
        "stage": "preparing_experience_analysis",
        "task_id": str(task.id),
        "sample": {
            "title": analysis.sample_title,
            "genre": analysis.source_genre or (novel.genre if novel else ""),
        },
    }
    db.commit()
    owner = db.get(User, analysis.owner_id)
    # 样本拆解属于分析任务，使用事件规划/审校模型；未单配时才兼容复用正文模型。
    llm_config = build_review_llm_config(owner.preferences if owner else {})
    if llm_config is None:
        raise ValueError("样本经验总结需要配置事件规划/质量审校模型 API")

    def on_part_progress(completed: int, total: int) -> None:
        analysis.analyzed_chunk_count = completed
        analysis.chunk_count = total
        analysis.summary = f"并行经验总结中：{completed}/{total} 份完成。"
        analysis.report = {
            **(analysis.report or {}),
            "stage": "analyzing_experience_parts",
            "experience_progress": {
                "completed": completed,
                "total": total,
                "model": llm_config.model,
            },
        }
        db.commit()
        task.result_payload = {
            **(task.result_payload or {}),
            "graph_status": f"样本经验并行总结中：{completed}/{total}",
        }
        mark_task(
            db,
            task,
            "running",
            min(72, 12 + int(60 * completed / max(total, 1))),
        )

    source_genre = analysis.source_genre or (novel.genre if novel else "")
    experience_document = build_sample_experience_document(
        sample_title=analysis.sample_title,
        source_genre=source_genre,
        source_object_key=analysis.source_object_key,
        llm_config=llm_config,
        progress_callback=on_part_progress,
    )

    analysis.source_word_count = int(experience_document.get("source_char_count") or 0)
    analysis.chapter_count = int(experience_document.get("source_chapter_count") or 0)
    analysis.chunk_count = int(experience_document.get("part_count") or 1)
    analysis.analyzed_chunk_count = analysis.chunk_count
    analysis.metrics = {}
    analysis.summary = "经验文档已生成，正在通过远程 Embedding API 建立经验卡索引。"
    reference_profile = {
        "available": True,
        "model": llm_config.model,
        "summary": str(experience_document.get("overview") or "")[:300],
        "language_rules": (
            experience_document.get("expression_principles") or []
        )[:12],
        "anti_ai_rules": (experience_document.get("anti_patterns") or [])[:12],
    }
    analysis.report = {
        "schema_version": "sample_experience.v1",
        "analysis_mode": "parallel_llm_experience_document",
        "stage": "indexing_experiences",
        "sample": {
            "title": analysis.sample_title,
            "genre": source_genre,
            "word_count": analysis.source_word_count,
            "chapter_count": analysis.chapter_count,
            "chunk_count": analysis.chunk_count,
        },
        "reference_profile": reference_profile,
        "experience_document": experience_document,
        "experience_summary": {
            "model": experience_document.get("model", ""),
            "part_count": experience_document.get("part_count", 0),
            "plot_experience_count": len(
                experience_document.get("plot_experiences") or []
            ),
            "expression_experience_count": len(
                experience_document.get("expression_experiences") or []
            ),
            "failed_part_count": len(
                experience_document.get("failed_parts") or []
            ),
        },
        "rag_index": {"status": "running", "passage_count": 0},
    }
    analysis.error_message = ""
    db.commit()

    def on_index_progress(completed: int, total: int) -> None:
        analysis.summary = f"精彩表达片段索引中：{completed}/{total}"
        analysis.report = {
            **(analysis.report or {}),
            "stage": "indexing_experiences",
            "rag_index": {
                "status": "running",
                "completed": completed,
                "total": total,
            },
        }
        db.commit()
        mark_task(
            db,
            task,
            "running",
            min(97, 90 + int(7 * completed / max(total, 1))),
        )

    try:
        rag_index = index_sample_experiences(
            db,
            analysis=analysis,
            experience_document=experience_document,
            preferences=owner.preferences if owner else {},
            progress_callback=on_index_progress,
        )
    except Exception as exc:
        db.rollback()
        analysis = db.get(SampleAnalysis, UUID(str(analysis_id)))
        rag_index = {
            "status": "failed",
            "reason": str(exc),
            "passage_count": 0,
            "embedding_model": "",
        }
    analysis.status = "completed"
    completed_report = {
        **(analysis.report or {}),
        "stage": "completed",
        "rag_index": rag_index,
    }
    analysis.summary = (
        f"经验文档已生成：{len(experience_document.get('plot_experiences') or [])} 条剧情经验，"
        f"{len(experience_document.get('expression_experiences') or [])} 条表达经验。"
    )
    analysis.report = completed_report
    db.commit()
    db.refresh(analysis)

    return {
        "agent": "SampleAnalysisAgent",
        "analysis_id": str(analysis.id),
        "status": analysis.status,
        "source_word_count": analysis.source_word_count,
        "chunk_count": analysis.chunk_count,
        "llm_strategy_available": True,
        "analysis_model": llm_config.model,
        "experience_summary": completed_report.get("experience_summary") or {},
        "rag_index": rag_index,
        "summary": analysis.summary,
        "contract": agent_contract(
            "SampleAnalysisAgent",
            "sample_analysis",
            generation_mode="llm",
        ),
    }


def handle_retry_event_revision(db: Session, task: GenerationTask, novel: Novel) -> dict:
    """复用既有事件修订蓝图，只补跑失败章节。"""
    task_input = (task.result_payload or {}).get("input") or {}
    source_task_id = task_input.get("source_task_id")
    story_event_id = task_input.get("story_event_id")
    if not source_task_id or not story_event_id:
        raise ValueError("事件修订重试任务缺少源任务或故事事件")
    source_task = db.get(GenerationTask, UUID(str(source_task_id)))
    story_event = db.get(StoryEvent, UUID(str(story_event_id)))
    if source_task is None or source_task.novel_id != novel.id:
        raise ValueError("事件修订源任务不存在")
    if story_event is None or story_event.novel_id != novel.id:
        raise ValueError("故事事件不存在")
    owner = db.get(User, novel.owner_id)
    preferences = owner.preferences if owner else {}
    output = retry_failed_event_revision_chapters(
        db,
        novel=novel,
        story_event=story_event,
        source_task=source_task,
        chapter_indexes=[int(index) for index in task_input.get("chapter_indexes", [])],
        revision_llm_config=build_llm_config(preferences),
        review_llm_config=build_review_llm_config(preferences),
    )
    applied_indexes = output.get("applied_chapter_indexes") or []
    if applied_indexes:
        chapters = db.scalars(
            select(Chapter).where(
                Chapter.novel_id == novel.id,
                Chapter.chapter_index.in_(applied_indexes),
            )
        ).all()
        enqueue_chapter_memory_tasks(
            db,
            novel=novel,
            chapters=list(chapters),
            source_task=source_task,
            story_event_id=story_event.id,
            source_progress=95,
            sync_reason="event_revision_retry_resync",
            expected_total=story_event.generated_chapter_count or story_event.planned_chapter_count,
        )
    return output


def handle_sync_chapter_memory(db: Session, task: GenerationTask, novel: Novel) -> dict:
    """后台一次性同步单章结构化记忆与时间线。"""
    task_input = (task.result_payload or {}).get("input") or {}
    source_progress = max(0, min(int(task_input.get("source_progress") or 95), 100))
    chapter_id = task.chapter_id or task_input.get("chapter_id")
    if not chapter_id:
        raise ValueError("后台记忆任务缺少 chapter_id")
    chapter = db.get(Chapter, UUID(str(chapter_id)))
    if chapter is None or chapter.novel_id != novel.id:
        raise ValueError("后台记忆任务对应的章节不存在，或不属于当前作品")

    source_task_id = task_input.get("source_task_id")
    source_task = db.get(GenerationTask, UUID(str(source_task_id))) if source_task_id else None
    if source_task is not None:
        emit_task_event(
            db,
            source_task,
            event_type="memory_sync",
            step_key=f"chapter_{chapter.chapter_index}_memory_sync",
            status="running",
            title=f"正在后台同步第 {chapter.chapter_index} 章记忆",
            message="一次模型调用同时抽取结构化记忆与故事时间线",
            progress=source_progress,
            chapter_id=chapter.id,
            chapter_index=chapter.chapter_index,
            payload={
                "memory_task_id": str(task.id),
                "memory_batch_id": task_input.get("memory_batch_id", ""),
                "background": True,
                "sync_reason": task_input.get("sync_reason", ""),
            },
        )

    owner = db.get(User, novel.owner_id)
    base_llm_config = build_llm_config(owner.preferences if owner else {})
    llm_config = (
        replace(base_llm_config, timeout_seconds=180.0, max_retries=0)
        if base_llm_config is not None
        else None
    )
    story_event_id = None
    if task_input.get("story_event_id"):
        try:
            story_event_id = UUID(str(task_input["story_event_id"]))
        except (TypeError, ValueError):
            story_event_id = None
    result = sync_chapter_memory_bundle(
        db=db,
        novel=novel,
        chapter=chapter,
        llm_config=llm_config,
        story_event_id=story_event_id,
    )
    return {
        "agent": "MemoryAgent",
        "chapter_id": str(chapter.id),
        "chapter_index": chapter.chapter_index,
        "background": True,
        **result,
        "contract": agent_contract(
            "MemoryAgent",
            "chapter_memory_merge",
            generation_mode="llm" if llm_config is not None else "rules",
        ),
    }


def finalize_source_memory_progress(db: Session, memory_task: GenerationTask) -> None:
    """把后台子任务的最终状态镜像到原正文任务的实时看板。"""
    task_input = (memory_task.result_payload or {}).get("input") or {}
    source_task_id = task_input.get("source_task_id")
    batch_id = str(task_input.get("memory_batch_id") or "")
    source_progress = max(0, min(int(task_input.get("source_progress") or 95), 100))
    if not source_task_id or not batch_id:
        return
    source_task = db.get(GenerationTask, UUID(str(source_task_id)))
    chapter = db.get(Chapter, memory_task.chapter_id) if memory_task.chapter_id else None
    if source_task is None or chapter is None:
        return

    succeeded = memory_task.status == "completed"
    output = (memory_task.result_payload or {}).get("output") or {}
    emit_task_event(
        db,
        source_task,
        event_type="memory_sync",
        step_key=f"chapter_{chapter.chapter_index}_memory_sync",
        status="completed" if succeeded else "failed",
        title=(
            f"第 {chapter.chapter_index} 章记忆同步完成"
            if succeeded
            else f"第 {chapter.chapter_index} 章记忆同步失败"
        ),
        message=(
            f"写入 {int(output.get('memory_count') or 0)} 条记忆、"
            f"{int(output.get('timeline_count') or 0)} 条时间线"
            if succeeded
            else (memory_task.error_message or "后台模型调用失败，可单独重试本章")
        ),
        progress=source_progress,
        chapter_id=chapter.id,
        chapter_index=chapter.chapter_index,
        payload={
            "memory_task_id": str(memory_task.id),
            "memory_batch_id": batch_id,
            "background": True,
            "retryable": not succeeded,
            "memory_count": int(output.get("memory_count") or 0),
            "timeline_count": int(output.get("timeline_count") or 0),
            "error": memory_task.error_message or "",
            "sync_reason": task_input.get("sync_reason", ""),
        },
    )

    candidates = db.scalars(
        select(GenerationTask).where(
            GenerationTask.novel_id == memory_task.novel_id,
            GenerationTask.task_type == "sync_chapter_memory",
        )
    ).all()
    batch_tasks = [
        candidate
        for candidate in candidates
        if str(((candidate.result_payload or {}).get("input") or {}).get("memory_batch_id") or "") == batch_id
    ]
    expected_total = max(0, int(task_input.get("expected_total") or 0))
    aggregate = aggregate_memory_batch_progress(batch_tasks, expected_total)
    completed = aggregate["completed"]
    failed = aggregate["failed"]
    total = aggregate["total"]
    finished = completed + failed
    aggregate_status = "running"
    aggregate_title = "后台同步记忆与时间线"
    if finished >= total and total:
        aggregate_status = "completed" if not failed else "failed"
        aggregate_title = "后台记忆同步完成" if not failed else "后台记忆同步部分失败"
    emit_task_event(
        db,
        source_task,
        event_type="memory_sync",
        step_key="memory_sync",
        status=aggregate_status,
        title=aggregate_title,
        message=(
            f"{completed}/{total} 章完成，{failed} 章待重试"
            + (f"；{aggregate['resync_count']} 次重同步" if aggregate["resync_count"] else "")
            if total
            else "没有可同步章节"
        ),
        progress=source_progress,
        payload={
            "memory_batch_id": batch_id,
            "total": total,
            "completed": completed,
            "failed": failed,
            "pending": aggregate["pending"],
            "resync_count": aggregate["resync_count"],
            "background": True,
            "sync_reason": task_input.get("sync_reason", ""),
        },
    )


def execute_task(db: Session, task_id: str) -> bool:
    """按任务类型分发到对应处理器，并维护 queued/running/completed/failed 状态。"""
    parsed_task_id = UUID(task_id)
    task = db.get(GenerationTask, parsed_task_id)
    if task is None:
        print(f"Task not found: {task_id}")
        return False
    if not claim_queued_task(db, parsed_task_id):
        db.expire_all()
        task = db.get(GenerationTask, parsed_task_id)
        print(
            f"Task notification skipped: {task_id} "
            f"(status={task.status if task else 'missing'})"
        )
        return False
    db.expire_all()
    task = db.get(GenerationTask, parsed_task_id)
    if task is None:
        return False

    novel = db.get(Novel, task.novel_id) if task.novel_id else None
    if novel is None and task.task_type != "analyze_sample":
        mark_task(db, task, "failed", 100, error_message="Novel not found")
        return True

    mark_task(db, task, "running", 10)
    heartbeat = TaskHeartbeat(task.id)
    heartbeat.start()

    try:
        agent_name = TASK_AGENT_NAMES.get(task.task_type, "UnknownAgent")
        task.result_payload = {
            **(task.result_payload or {}),
            "input_contract": agent_input_contract(
                agent_name,
                task.task_type,
                task_id=str(task.id),
                novel_id=str(task.novel_id or ""),
                payload=((task.result_payload or {}).get("input") or {}),
            ),
        }
        db.commit()
        if task.task_type == "generate_chapter":
            output = handle_generate_chapter(db, task, novel)
        elif task.task_type == "generate_story_event":
            output = run_event_generation_graph(db=db, task=task, novel=novel)
        elif task.task_type == "produce_novel":
            output = run_novel_production_graph(db=db, task=task, novel=novel)
        elif task.task_type == "continue_story_event":
            output = handle_continue_story_event(db, task, novel)
        elif task.task_type == "check_story_event_quality":
            output = handle_check_story_event_quality(db, task, novel)
        elif task.task_type == "build_story_bible":
            output = handle_build_story_bible(db, task, novel)
        elif task.task_type == "analyze_sample":
            output = handle_analyze_sample(db, task, novel)
        elif task.task_type == "sync_chapter_memory":
            output = handle_sync_chapter_memory(db, task, novel)
        elif task.task_type == "retry_event_revision":
            output = handle_retry_event_revision(db, task, novel)
        else:
            raise ValueError(f"Unsupported task type: {task.task_type}")

        result_payload = {
            **(task.result_payload or {}),
            "agent": output.get("agent") or "NovelProductionAgent",
            "output": output,
        }
        output_contract = output.get("contract") or {}
        output_waiting = bool(
            output.get("deferred")
            or output.get("requires_word_revision")
            or output.get("requires_quality_revision")
            or output.get("pause_requested")
            or output_contract.get("status") == "waiting"
        )
        if output_waiting:
            mark_task(
                db,
                task,
                "waiting",
                min(95, max(10, int(task.progress or 0))),
                result_payload=result_payload,
            )
        else:
            mark_task(db, task, "completed", 100, result_payload=result_payload)
        if (
            task.task_type == "generate_story_event"
            and ((task.result_payload or {}).get("input") or {}).get("parent_task_id")
        ):
            complete_event_child_handoff(
                db,
                child_task=task,
                output=output,
            )
        if task.task_type == "sync_chapter_memory":
            try:
                finalize_source_memory_progress(db, task)
            except Exception as progress_exc:
                db.rollback()
                print(f"Memory progress mirror failed: {task_id} - {progress_exc}")
        print(
            f"Task {'waiting' if output_waiting else 'completed'}: "
            f"{task_id} ({task.task_type})"
        )
    except Exception as exc:
        db.rollback()
        task = db.get(GenerationTask, UUID(task_id))
        if task is not None:
            failure = (
                exc.failure
                if isinstance(exc, AgentExecutionError)
                else AgentFailure(
                    code=type(exc).__name__,
                    message=str(exc),
                    stage=task.task_type,
                    retryable=False,
                )
            )
            agent_name = TASK_AGENT_NAMES.get(task.task_type, "UnknownAgent")
            failure_output = {
                "agent": agent_name,
                "contract": agent_contract(
                    agent_name,
                    task.task_type,
                    status="failed",
                    failure=failure,
                ),
            }
            failure_payload = {
                **(task.result_payload or {}),
                "agent": agent_name,
                "output": failure_output,
            }
            if task.task_type == "produce_novel":
                auto_run_id = ((task.result_payload or {}).get("input") or {}).get("auto_run_id")
                if auto_run_id:
                    auto_run = db.get(AutoNovelRun, UUID(str(auto_run_id)))
                    if auto_run is not None:
                        auto_run.status = "failed"
                        auto_run.stage = "failed"
                        auto_run.last_error = str(exc)
            if task.task_type == "generate_story_event":
                story_event = db.scalar(select(StoryEvent).where(StoryEvent.task_id == task.id))
                if story_event is not None:
                    story_event.status = "failed"
                    story_event.payload = {**(story_event.payload or {}), "error": str(exc)}
            if task.task_type == "analyze_sample":
                analysis_id = ((task.result_payload or {}).get("input") or {}).get("analysis_id")
                if analysis_id:
                    analysis = db.get(SampleAnalysis, UUID(str(analysis_id)))
                    if analysis is not None:
                        analysis.status = "failed"
                        analysis.error_message = str(exc)
                        analysis.summary = "样本分析失败，请检查上传文件编码或 Worker 日志。"
                        analysis.report = {**(analysis.report or {}), "stage": "failed", "error": str(exc)}
            mark_task(
                db,
                task,
                "failed",
                100,
                result_payload=failure_payload,
                error_message=str(exc),
            )
            if (
                task.task_type == "generate_story_event"
                and ((task.result_payload or {}).get("input") or {}).get("parent_task_id")
            ):
                fail_event_child_handoff(
                    db,
                    child_task=task,
                    error=str(exc),
                )
            if task.task_type == "sync_chapter_memory":
                try:
                    finalize_source_memory_progress(db, task)
                except Exception as progress_exc:
                    db.rollback()
                    print(f"Memory progress mirror failed: {task_id} - {progress_exc}")
            if task.task_type == "retry_event_revision":
                retry_input = (task.result_payload or {}).get("input") or {}
                source_task_id = retry_input.get("source_task_id")
                source_task = (
                    db.get(GenerationTask, UUID(str(source_task_id)))
                    if source_task_id
                    else None
                )
                if source_task is not None:
                    source_output = (source_task.result_payload or {}).get("output") or {}
                    source_revision = source_output.get("event_revision") or {}
                    package_number_by_chapter = {
                        int(index): package_number
                        for package_number, package in enumerate(
                            source_revision.get("repair_packages", []),
                            start=1,
                        )
                        for index in package.get("requested_chapter_indexes", [])
                        if str(index).isdigit()
                    }
                    for chapter_index in retry_input.get("chapter_indexes", []):
                        emit_task_event(
                            db,
                            source_task,
                            event_type="revision_patch_stream",
                            step_key=(
                                f"revision_package_{package_number_by_chapter.get(int(chapter_index), 1)}"
                                f"_chapter_{chapter_index}"
                            ),
                            status="failed",
                            title=f"第 {chapter_index} 章事件补丁重试失败",
                            message=str(exc),
                            progress=94,
                            chapter_index=int(chapter_index),
                            payload={
                                "retryable": True,
                                "manual_retry": True,
                                "retry_task_id": str(task.id),
                            },
                        )
        print(f"Task failed: {task_id} - {exc}")
    finally:
        heartbeat.stop()
    return True


def consume_once(redis_client: Redis, queue_name: str | None = None) -> bool:
    """可靠消费一个任务：移入 processing，终态落库后再确认删除。"""
    selected_queue = queue_name or settings.agent_task_queue
    processing_queue = processing_queue_name(selected_queue)
    try:
        task_id = redis_client.blmove(
            selected_queue,
            processing_queue,
            timeout=5,
            src="LEFT",
            dest="RIGHT",
        )
    except RedisTimeoutError:
        return False

    if task_id is None:
        return False

    if isinstance(task_id, bytes):
        task_id = task_id.decode("utf-8")
    acknowledged = False
    try:
        with SessionLocal() as db:
            execute_task(db, task_id)
        acknowledged = True
    finally:
        if acknowledged:
            redis_client.lrem(processing_queue, 1, task_id)
    return True


def run_worker(once: bool, queue_name: str | None = None) -> None:
    """启动 Worker 主循环。"""
    redis_client = get_redis_client()
    selected_queue = queue_name or settings.agent_task_queue
    print(f"NovelForge worker listening on queue: {selected_queue}")
    recovery = recover_worker_queue(redis_client, selected_queue)
    if any(recovery.values()):
        print(f"Queue recovery: {recovery}")

    if once:
        consumed = consume_once(redis_client, selected_queue)
        if not consumed:
            print("No queued task found.")
        return

    last_recovery = time.monotonic()
    while True:
        consume_once(redis_client, selected_queue)
        if (
            time.monotonic() - last_recovery
            >= max(5, int(settings.task_recovery_interval_seconds))
        ):
            recovery = recover_worker_queue(redis_client, selected_queue)
            if any(recovery.values()):
                print(f"Queue recovery: {recovery}")
            last_recovery = time.monotonic()


def main() -> None:
    """命令行入口，支持常驻消费或只消费一次。"""
    parser = argparse.ArgumentParser(description="NovelForge worker")
    parser.add_argument("--once", action="store_true", help="Process one queued task and exit.")
    parser.add_argument(
        "--queue",
        choices=("agent", "memory"),
        default="agent",
        help="选择正文任务队列或后台记忆队列。",
    )
    args = parser.parse_args()
    queue_name = settings.memory_task_queue if args.queue == "memory" else settings.agent_task_queue
    run_worker(once=args.once, queue_name=queue_name)


if __name__ == "__main__":
    main()
