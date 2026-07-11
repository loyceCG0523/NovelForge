"""NovelForge 后台任务 Worker。

API 负责创建任务并写入 Redis 队列：Worker 独立消费队列并执行耗时的 Agent 工作。
当前版本使用模拟生成逻辑打通闭环，后续接入 LLM/LangGraph 时，优先替换各个
handle_* 函数内部实现，而不是改变任务队列和任务状态协议。
"""

import argparse
import sys
from pathlib import Path
from uuid import UUID

from redis import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import func, select
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
from app.models.review_issue import ReviewIssue  # noqa: E402
from app.models.sample_analysis import SampleAnalysis  # noqa: E402
from app.models.story_event import StoryEvent  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.agents.sample_analysis_agent import analyze_sample_chunks, summarize_sample_report  # noqa: E402
from app.services.agents.chapter_writing_agent import build_chapter_agent_result  # noqa: E402
from app.services.agents.memory_agent import sync_memory_after_chapter  # noqa: E402
from app.services.agents.quality_agent import (  # noqa: E402
    review_chapter_continuity_only,
    review_chapter_quality,
    review_story_event_quality,
    revise_chapter_with_quality_agent,
)
from app.services.agents.story_planning_agent import build_or_refresh_story_bible  # noqa: E402
from app.services.chapter_context_builder import build_chapter_context  # noqa: E402
from app.services.llm_client import LLMClient, build_llm_config  # noqa: E402
from app.services.object_storage import iter_text_object_chunks  # noqa: E402
from app.services.prompt_builder import build_chapter_prompt  # noqa: E402
from worker.graphs.event_generation_graph import run_event_generation_graph  # noqa: E402
from worker.graphs.novel_production_graph import run_novel_production_graph  # noqa: E402


def get_redis_client() -> Redis:
    """创建 Redis 客户端；队列名由统一配置读取，避免 API 和 Worker 不一致。"""
    return Redis.from_url(settings.redis_url, decode_responses=True)


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
    chapter_word_range = guidance.get("chapter_word_range") or {"min": 2000, "max": 3000}

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
            f"本章字数约束：{chapter_word_range.get('min', 2000)}-{chapter_word_range.get('max', 3000)} 字。",
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


def sync_event_plan_after_chapter(db: Session, task_input: dict, chapter: Chapter, status: str = "generated") -> None:
    """章节重跑后同步剧情事件章节计划，保证事件看板状态实时更新。"""
    story_event_id = task_input.get("story_event_id")
    event_plan_id = task_input.get("event_plan_id")
    if not story_event_id and not event_plan_id:
        return

    statement = select(EventChapterPlan).where(EventChapterPlan.chapter_index == chapter.chapter_index)
    if event_plan_id:
        statement = select(EventChapterPlan).where(EventChapterPlan.id == UUID(str(event_plan_id)))
    elif story_event_id:
        statement = statement.where(EventChapterPlan.story_event_id == UUID(str(story_event_id)))

    plan = db.scalar(statement.limit(1))
    if plan is None:
        return

    plan.chapter_id = chapter.id
    plan.title = chapter.title
    plan.status = status
    plan.payload = {
        **(plan.payload or {}),
        "generated_word_count": chapter.word_count,
        "last_generated_chapter_id": str(chapter.id),
        "last_generation_source": task_input.get("source", "worker"),
    }

    story_event = db.get(StoryEvent, plan.story_event_id)
    if story_event is not None:
        generated_count = db.scalar(
            select(func.count())
            .select_from(EventChapterPlan)
            .where(
                EventChapterPlan.story_event_id == story_event.id,
                EventChapterPlan.status.in_(["generated", "revised"]),
            )
        )
        story_event.generated_chapter_count = generated_count or 0
        if story_event.generated_chapter_count >= story_event.planned_chapter_count and story_event.planned_chapter_count:
            story_event.status = "completed"
        else:
            story_event.status = "generating"
    db.commit()


def handle_generate_chapter(db: Session, task: GenerationTask, novel: Novel) -> dict:
    """执行章节生成任务，并把上下文快照写回章节记录。"""
    target_chapter_index = get_target_chapter_index(db, task, novel)
    task_input = (task.result_payload or {}).get("input", {})
    context = build_chapter_context(
        db=db,
        novel=novel,
        target_chapter_index=target_chapter_index,
        task_input=task_input,
    )

    owner = db.get(User, novel.owner_id)
    llm_config = build_llm_config(owner.preferences if owner else {})
    generation_mode = "simulation"
    llm_model = ""

    if llm_config is None:
        title, summary, content = build_simulated_chapter(context)
    else:
        prompt_messages = build_chapter_prompt(context)
        llm_result = LLMClient(llm_config).generate_chapter(prompt_messages)
        title = llm_result["title"]
        summary = llm_result["summary"]
        content = llm_result["content"]
        generation_mode = "llm"
        llm_model = llm_config.model

    chapter = db.get(Chapter, task.chapter_id) if task.chapter_id else None
    # 允许 Worker 幂等运行：如果目标章节已存在，就更新草稿；不存在则创建新章节。
    if chapter is None:
        chapter = db.scalar(
            select(Chapter).where(
                Chapter.novel_id == novel.id,
                Chapter.chapter_index == target_chapter_index,
            )
        )

    if chapter is None:
        chapter = Chapter(
            novel_id=novel.id,
            chapter_index=target_chapter_index,
            title=title,
            status="done",
            summary=summary,
            content=content,
            word_count=len(content),
            context_snapshot=context,
        )
        db.add(chapter)
    else:
        chapter.title = title if task_input.get("force_regenerate") else (chapter.title or title)
        chapter.status = "done"
        chapter.summary = summary
        chapter.content = content
        chapter.word_count = len(content)
        chapter.context_snapshot = context

    novel.current_chapter_index = max(novel.current_chapter_index, chapter.chapter_index)
    db.commit()
    db.refresh(chapter)
    sync_event_plan_after_chapter(db, task_input, chapter, "generated")

    memory_sync = {"created": 0, "error": ""}
    try:
        memory_items = sync_memory_after_chapter(
            db=db,
            novel=novel,
            chapter=chapter,
            llm_config=llm_config,
        )
        memory_sync["created"] = len(memory_items)
    except Exception as exc:
        db.rollback()
        memory_sync["error"] = str(exc)

    continuity_review, auto_review_handling = review_chapter_quality(
        db=db,
        novel=novel,
        chapter=chapter,
        context=context,
        llm_config=llm_config,
        task_id=str(task.id),
        source="generate_chapter",
        max_attempts=2,
    )

    event_quality = {}
    if task_input.get("story_event_id"):
        story_event = db.get(StoryEvent, UUID(str(task_input["story_event_id"])))
        if story_event is not None and story_event.novel_id == novel.id:
            try:
                event_quality = review_story_event_quality(
                    db=db,
                    novel=novel,
                    story_event=story_event,
                    llm_config=llm_config,
                )
            except Exception as exc:
                db.rollback()
                event_quality = {"error": str(exc)}

    return {
        "chapter_id": str(chapter.id),
        "chapter_index": chapter.chapter_index,
        "title": chapter.title,
        "word_count": chapter.word_count,
        **build_chapter_agent_result(generation_mode=generation_mode, llm_model=llm_model),
        "memory_sync": memory_sync,
        "continuity_review": continuity_review,
        "auto_review_handling": auto_review_handling,
        "event_quality": event_quality,
        "context_stats": context["stats"],
    }


def handle_revise_chapter(db: Session, task: GenerationTask, novel: Novel) -> dict:
    """根据指定 ReviewIssue 修订章节，并重新触发连续性审校。"""
    if task.chapter_id is None:
        raise ValueError("修订任务缺少 chapter_id")

    task_input = (task.result_payload or {}).get("input", {})
    issue_id = task_input.get("issue_id")
    if not issue_id:
        raise ValueError("修订任务缺少 issue_id")

    chapter = db.get(Chapter, task.chapter_id)
    if chapter is None or chapter.novel_id != novel.id:
        raise ValueError("修订任务对应的章节不存在，或不属于当前作品")

    issue = db.get(ReviewIssue, UUID(str(issue_id)))
    if issue is None or issue.novel_id != novel.id or issue.chapter_id != chapter.id:
        raise ValueError("修订任务对应的审校问题不存在，或不属于当前章节")

    owner = db.get(User, novel.owner_id)
    llm_config = build_llm_config(owner.preferences if owner else {})
    if llm_config is None:
        raise RuntimeError("未配置 LLM API Key，无法自动修正文稿")

    context = build_chapter_context(
        db=db,
        novel=novel,
        target_chapter_index=chapter.chapter_index,
        task_input={
            **task_input,
            "revision_issue_id": str(issue.id),
            "revision_issue_message": issue.message,
        },
    )
    revision = revise_chapter_with_quality_agent(
        novel=novel,
        chapter=chapter,
        issue=issue,
        context=context,
        llm_config=llm_config,
    )

    chapter.title = revision["title"]
    chapter.summary = revision["summary"]
    chapter.content = revision["content"]
    chapter.word_count = len(revision["content"])
    chapter.status = "done"
    chapter.context_snapshot = context

    issue.status = "resolved"
    issue.payload = {
        **(issue.payload or {}),
        "resolved_by": "QualityAgent",
        "resolved_by_task_id": str(task.id),
        "revision_note": revision["revision_note"],
    }
    db.commit()
    db.refresh(chapter)
    db.refresh(issue)

    refreshed_context = build_chapter_context(
        db=db,
        novel=novel,
        target_chapter_index=chapter.chapter_index,
        task_input={
            **task_input,
            "source": "post_revision_continuity_review",
            "revision_issue_id": str(issue.id),
        },
    )
    continuity_review = review_chapter_continuity_only(
        db=db,
        novel=novel,
        chapter=chapter,
        context=refreshed_context,
        llm_config=llm_config,
    )

    return {
        "agent": "QualityAgent",
        "chapter_id": str(chapter.id),
        "chapter_index": chapter.chapter_index,
        "issue_id": str(issue.id),
        "word_count": chapter.word_count,
        "generation_mode": "llm",
        "llm_model": llm_config.model,
        "revision_note": revision["revision_note"],
        "continuity_review": continuity_review,
    }


def _plan_to_generation_input(story_event: StoryEvent, plan: EventChapterPlan, base_input: dict) -> dict:
    """把事件章节计划转换为 generate_chapter 可读取的输入。"""
    return {
        **base_input,
        "source": base_input.get("source") or "continue_story_event",
        "story_event_id": str(story_event.id),
        "event_plan_id": str(plan.id),
        "chapter_index": plan.chapter_index,
        "force_regenerate": True,
        "story_event": story_event.payload or {},
        "chapter_plan": plan.payload or {
            "chapter_index": plan.chapter_index,
            "title": plan.title,
            "function": plan.function,
            "core_event": plan.core_event,
            "ending_hook": plan.ending_hook,
        },
    }


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
    plans = db.scalars(
        select(EventChapterPlan)
        .where(
            EventChapterPlan.story_event_id == story_event.id,
            EventChapterPlan.chapter_index >= from_chapter_index,
        )
        .order_by(EventChapterPlan.chapter_index.asc())
    ).all()
    if not plans:
        return {
            "agent": "continue_story_event",
            "story_event_id": str(story_event.id),
            "generated_chapters": [],
            "note": "指定章节之后没有可继续生成的章节计划。",
        }

    story_event.status = "generating"
    db.commit()

    generated = []
    memory_sync = []
    continuity_reviews = []
    auto_review_history = []
    for offset, plan in enumerate(plans):
        progress = 15 + int(75 * (offset / max(len(plans), 1)))
        task.result_payload = {
            **(task.result_payload or {}),
            "input": _plan_to_generation_input(story_event, plan, task_input),
            "graph_status": f"正在从第 {plan.chapter_index} 章继续生成剧情事件",
        }
        mark_task(db, task, "running", progress, result_payload=task.result_payload)

        output = handle_generate_chapter(db=db, task=task, novel=novel)
        generated.append(
            {
                "chapter_id": output["chapter_id"],
                "chapter_index": output["chapter_index"],
                "title": output["title"],
                "word_count": output["word_count"],
            }
        )
        memory_sync.append(output.get("memory_sync", {}))
        continuity_reviews.append(output.get("continuity_review", {}))
        auto_review_history.extend((output.get("auto_review_handling") or {}).get("history", []))

    open_issue_count = db.scalar(
        select(func.count())
        .select_from(ReviewIssue)
        .join(EventChapterPlan, EventChapterPlan.chapter_id == ReviewIssue.chapter_id)
        .where(EventChapterPlan.story_event_id == story_event.id, ReviewIssue.status == "open")
    )
    repaired_count = len([item for item in auto_review_history if item.get("status") == "resolved"])
    generated_count = db.scalar(
        select(func.count())
        .select_from(EventChapterPlan)
        .where(EventChapterPlan.story_event_id == story_event.id, EventChapterPlan.status.in_(["generated", "revised"]))
    )
    story_event.generated_chapter_count = generated_count or 0
    story_event.remaining_open_risks = open_issue_count or 0
    story_event.auto_repair_count = (story_event.auto_repair_count or 0) + repaired_count
    story_event.status = "completed" if story_event.generated_chapter_count >= story_event.planned_chapter_count else "generating"
    db.commit()
    owner = db.get(User, novel.owner_id)
    llm_config = build_llm_config(owner.preferences if owner else {})
    quality_result = review_story_event_quality(db=db, novel=novel, story_event=story_event, llm_config=llm_config)

    return {
        "agent": "StoryPlanningAgent",
        "story_event_id": str(story_event.id),
        "from_chapter_index": from_chapter_index,
        "generated_chapters": generated,
        "memory_sync": memory_sync,
        "continuity_reviews": continuity_reviews,
        "auto_review_history": auto_review_history,
        "event_quality": quality_result,
        "remaining_open_risks": story_event.remaining_open_risks,
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
    llm_config = build_llm_config(owner.preferences if owner else {})
    result = review_story_event_quality(db=db, novel=novel, story_event=story_event, llm_config=llm_config)
    return {
        "agent": "QualityAgent",
        "story_event_id": str(story_event.id),
        **result,
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
    }


def handle_analyze_sample(db: Session, task: GenerationTask, novel: Novel | None = None) -> dict:
    """异步分析大体量样本文本，并回写结构化风格报告。"""
    task_input = (task.result_payload or {}).get("input", {})
    analysis_id = task_input.get("analysis_id")
    if not analysis_id:
        raise ValueError("样本分析任务缺少 analysis_id")

    analysis = db.get(SampleAnalysis, UUID(str(analysis_id)))
    if analysis is None:
        raise ValueError("样本分析记录不存在")

    analysis.status = "running"
    analysis.error_message = ""
    analysis.summary = "SampleAnalysisAgent 正在分片读取样本并抽取工程特征。"
    analysis.report = {
        **(analysis.report or {}),
        "stage": "running",
        "task_id": str(task.id),
    }
    db.commit()
    owner = db.get(User, analysis.owner_id)
    llm_config = build_llm_config(owner.preferences if owner else {})

    def on_chunk(chunk_index: int, _chunk_report: dict) -> None:
        analysis.analyzed_chunk_count = chunk_index
        analysis.chunk_count = max(analysis.chunk_count, chunk_index)
        analysis.summary = f"已完成 {chunk_index} 个文本分片的风格指标抽取。"
        db.commit()
        task.result_payload = {
            **(task.result_payload or {}),
            "graph_status": f"样本分片分析中：{chunk_index} 个分片",
        }
        mark_task(db, task, "running", min(90, 15 + chunk_index))

    chunks = iter_text_object_chunks(analysis.source_object_key)
    source_genre = analysis.source_genre or (novel.genre if novel else "")
    report = analyze_sample_chunks(
        sample_title=analysis.sample_title,
        source_genre=source_genre,
        chunks=chunks,
        progress_callback=on_chunk,
        llm_config=llm_config,
    )

    analysis.status = "completed"
    analysis.source_word_count = report["sample"]["word_count"]
    analysis.chapter_count = report["sample"]["chapter_count"]
    analysis.chunk_count = report["sample"]["chunk_count"]
    analysis.analyzed_chunk_count = report["sample"]["chunk_count"]
    analysis.summary = summarize_sample_report(report)
    analysis.metrics = report["transferable_style_vector"]
    analysis.report = report
    analysis.error_message = ""
    db.commit()
    db.refresh(analysis)

    return {
        "agent": "SampleAnalysisAgent",
        "analysis_id": str(analysis.id),
        "status": analysis.status,
        "source_word_count": analysis.source_word_count,
        "chunk_count": analysis.chunk_count,
        "llm_strategy_available": bool((report.get("llm_style_strategy") or {}).get("available")),
        "summary": analysis.summary,
    }


def execute_task(db: Session, task_id: str) -> None:
    """按任务类型分发到对应处理器，并维护 queued/running/completed/failed 状态。"""
    task = db.get(GenerationTask, UUID(task_id))
    if task is None:
        print(f"Task not found: {task_id}")
        return

    novel = db.get(Novel, task.novel_id) if task.novel_id else None
    if novel is None and task.task_type != "analyze_sample":
        mark_task(db, task, "failed", 100, error_message="Novel not found")
        return

    mark_task(db, task, "running", 10)

    try:
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
        elif task.task_type == "revise_chapter":
            output = handle_revise_chapter(db, task, novel)
        else:
            output = {
                "task_type": task.task_type,
                "note": "Unknown task type handled by simulation placeholder.",
            }

        result_payload = {
            **(task.result_payload or {}),
            "agent": output.get("agent") or "NovelProductionAgent",
            "output": output,
        }
        mark_task(db, task, "completed", 100, result_payload=result_payload)
        print(f"Task completed: {task_id} ({task.task_type})")
    except Exception as exc:
        db.rollback()
        task = db.get(GenerationTask, UUID(task_id))
        if task is not None:
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
            mark_task(db, task, "failed", 100, error_message=str(exc))
        print(f"Task failed: {task_id} - {exc}")


def consume_once(redis_client: Redis) -> bool:
    """从 Redis 队列消费一个任务；没有任务时返回 False，便于 --once 测试。"""
    try:
        item = redis_client.blpop(settings.agent_task_queue, timeout=5)
    except RedisTimeoutError:
        return False

    if item is None:
        return False

    _, task_id = item
    if isinstance(task_id, bytes):
        task_id = task_id.decode("utf-8")
    with SessionLocal() as db:
        execute_task(db, task_id)
    return True


def run_worker(once: bool) -> None:
    """启动 Worker 主循环。"""
    redis_client = get_redis_client()
    print(f"NovelForge worker listening on queue: {settings.agent_task_queue}")

    if once:
        consumed = consume_once(redis_client)
        if not consumed:
            print("No queued task found.")
        return

    while True:
        consume_once(redis_client)


def main() -> None:
    """命令行入口，支持常驻消费或只消费一次。"""
    parser = argparse.ArgumentParser(description="NovelForge worker")
    parser.add_argument("--once", action="store_true", help="Process one queued task and exit.")
    args = parser.parse_args()
    run_worker(once=args.once)


if __name__ == "__main__":
    main()
