"""统一章节流水线：上下文、正文、审校、轻量事实和后台完整记忆。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.event_chapter_plan import EventChapterPlan
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.models.story_event import StoryEvent
from app.services.agent_contracts import AgentFailure, agent_contract
from app.services.agent_orchestrator import enqueue_chapter_memory_tasks
from app.services.chapter_context_builder import build_chapter_context
from app.services.chapter_fact_delta import sync_chapter_fact_delta
from app.services.chapter_progress import normalize_chapter_progress
from app.services.chapter_review_cycle import review_and_revise_chapter_once
from app.services.chapter_word_guard import (
    attach_word_guard_to_context,
    build_calibrated_chapter_word_target,
    build_model_calibration_key,
    build_word_guard_report,
    count_chapter_words,
    generate_chapter_with_word_guard,
    get_context_chapter_word_range,
)
from app.services.llm_client import LLMClient, LLMConfig, LLMRequestCancelledError
from app.services.paragraph_formatter import format_chapter_paragraphs
from app.services.prompt_builder import build_chapter_prompt
from app.services.reference_overlap_guard import build_reference_overlap_report
from app.services.sample_rag import (
    ChapterReferenceRequirementError,
    build_chapter_reference_pack,
)
from app.services.meme_rag import (
    build_chapter_meme_pack,
    build_final_meme_usage,
    find_repeated_meme_phrases,
    find_reused_event_meme_phrases,
)
from app.services.meme_usage_enforcer import (
    ensure_minimum_meme_usage,
    repair_repeated_meme_usage,
)
from app.services.task_events import (
    ChapterPreviewPublisher,
    ModelThinkingPublisher,
    emit_task_event,
)


SimulatedChapterBuilder = Callable[[dict[str, Any]], tuple[str, str, str]]


@dataclass(slots=True)
class ChapterPipelineRequest:
    target_chapter_index: int
    task_input: dict[str, Any]
    simulated_builder: SimulatedChapterBuilder
    llm_config: LLMConfig | None
    review_llm_config: LLMConfig | None
    context: dict[str, Any] | None = None
    existing_chapter: Chapter | None = None
    story_event_id: UUID | None = None
    progress: int = 0
    force_title: bool = True
    memory_expected_total: int = 1
    memory_sync_reason: str = "chapter_review_complete"
    strict_quality_gate: bool = True
    defer_review: bool = False


@dataclass(slots=True)
class ChapterPipelineResult:
    chapter: Chapter
    context: dict[str, Any]
    draft_content: str
    generation_mode: str
    llm_model: str
    word_guard: dict[str, Any]
    chapter_progress: dict[str, Any]
    chapter_review: dict[str, Any]
    reference_pack: dict[str, Any]
    reference_overlap: dict[str, Any]
    memory_tasks: list[GenerationTask]
    fact_delta_id: str
    requires_word_revision: bool
    requires_quality_revision: bool
    contract: dict[str, Any]

    def to_output(self) -> dict[str, Any]:
        review = self.chapter_review
        return {
            "chapter_id": str(self.chapter.id),
            "chapter_index": self.chapter.chapter_index,
            "title": self.chapter.title,
            "word_count": self.chapter.word_count,
            "agent": "ChapterWritingAgent",
            "generation_mode": self.generation_mode,
            "llm_model": self.llm_model,
            "word_guard": self.word_guard,
            "chapter_progress": self.chapter_progress,
            "chapter_review_cycle": review,
            "continuity_review": {
                "scope": "chapter_internal",
                "issues": review.get("issue_count", 0),
                "suggestions": review.get("suggestions", []),
                "chapter_review_cycle": review,
            },
            "auto_review_handling": {
                "attempted": review.get("issue_count", 0),
                "resolved": review.get("resolved_count", 0),
                "skipped": review.get("deferred_count", 0),
                "remaining_open": review.get("deferred_count", 0),
                "history": [review] if review else [],
                "cycle_count": review.get("cycle_count", 0),
            },
            "memory_sync": {
                "created": len(self.memory_tasks),
                "status": "queued" if self.memory_tasks else "deduplicated",
                "background": True,
                "task_ids": [str(task.id) for task in self.memory_tasks],
                "fact_delta_id": self.fact_delta_id,
                "fact_delta_status": "completed" if self.fact_delta_id else "skipped",
            },
            "context_stats": self.context.get("stats", {}),
            "expression_reference": {
                "status": self.reference_pack.get("status", "skipped"),
                "reference_count": len(self.reference_pack.get("references") or []),
                "total_chars": self.reference_pack.get("total_chars", 0),
                "embedding_model": self.reference_pack.get("embedding_model", ""),
                "reason": self.reference_pack.get("reason", ""),
                "overlap_guard": self.reference_overlap,
            },
            "requires_word_revision": self.requires_word_revision,
            "requires_quality_revision": self.requires_quality_revision,
            "contract": self.contract,
        }


def generate_chapter_content(
    *,
    context: dict[str, Any],
    llm_config: LLMConfig | None,
    simulated_builder: SimulatedChapterBuilder,
    preview_publisher: ChapterPreviewPublisher | None = None,
    thinking_publisher: ModelThinkingPublisher | None = None,
) -> tuple[str, str, str, str, str, dict[str, Any], dict[str, Any]]:
    """所有章节入口共享的正文生成和字数护栏。"""
    word_range = get_context_chapter_word_range(context)
    if llm_config is None:
        title, summary, content = simulated_builder(context)
        content = format_chapter_paragraphs(content)
        word_guard = {
            **build_word_guard_report(content, word_range),
            "enforced": False,
            "accepted": True,
            "needs_revision": False,
        }
        return title, summary, content, "simulation", "", word_guard, {}

    model_calibration_key = build_model_calibration_key(llm_config.base_url, llm_config.model)
    length_target = build_calibrated_chapter_word_target(
        word_range,
        context.get("recent_chapters"),
        model_calibration_key,
    )
    prompt_word_range = {
        "min": length_target["prompt_target_min"],
        "max": length_target["prompt_target_max"],
        "unit": "字",
    }

    def reset_stream(attempt: int) -> None:
        if preview_publisher is not None:
            preview_publisher.reset(attempt)
        if thinking_publisher is not None:
            thinking_publisher.start(attempt=attempt)

    llm_result, word_guard = generate_chapter_with_word_guard(
        llm_client=LLMClient(llm_config),
        prompt_messages=build_chapter_prompt(
            context,
            chapter_word_range_override=prompt_word_range,
        ),
        word_range=word_range,
        initial_target_range=prompt_word_range,
        stream_callback=(
            (lambda delta, attempt: preview_publisher.append(delta, attempt=attempt))
            if preview_publisher is not None
            else None
        ),
        stream_reset_callback=(
            reset_stream
            if preview_publisher is not None or thinking_publisher is not None
            else None
        ),
        activity_callback=(
            (
                lambda activity, attempt: thinking_publisher.append_activity(
                    {**activity, "generation_attempt": attempt}
                )
            )
            if thinking_publisher is not None
            else None
        ),
    )
    word_guard = {
        **word_guard,
        "model": llm_config.model,
        "model_calibration_key": model_calibration_key,
        "length_target": length_target,
    }
    return (
        llm_result["title"],
        llm_result["summary"],
        llm_result["content"],
        "llm",
        llm_config.model,
        word_guard,
        llm_result.get("chapter_progress") or {},
    )


def sync_event_plan_after_pipeline(
    db: Session,
    *,
    task_input: dict[str, Any],
    chapter: Chapter,
    status: str,
) -> None:
    story_event_id = task_input.get("story_event_id")
    event_plan_id = task_input.get("event_plan_id")
    if not story_event_id and not event_plan_id:
        return
    statement = select(EventChapterPlan).where(
        EventChapterPlan.chapter_index == chapter.chapter_index
    )
    if event_plan_id:
        statement = select(EventChapterPlan).where(
            EventChapterPlan.id == UUID(str(event_plan_id))
        )
    elif story_event_id:
        statement = statement.where(
            EventChapterPlan.story_event_id == UUID(str(story_event_id))
        )
    plan = db.scalar(statement.limit(1))
    if plan is None:
        return
    plan.chapter_id = chapter.id
    plan.title = chapter.title
    plan.status = status
    plan.payload = {
        **(plan.payload or {}),
        "generated_word_count": chapter.word_count,
        "word_guard": (chapter.context_snapshot or {}).get("word_guard", {}),
        "chapter_progress": (chapter.context_snapshot or {}).get("chapter_progress", {}),
        "last_generated_chapter_id": str(chapter.id),
        "last_generation_source": task_input.get("source", "chapter_pipeline"),
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
        story_event.status = (
            "completed"
            if story_event.planned_chapter_count
            and story_event.generated_chapter_count >= story_event.planned_chapter_count
            else "generating"
        )
    db.commit()


def run_chapter_pipeline(
    *,
    db: Session,
    task: GenerationTask,
    novel: Novel,
    request: ChapterPipelineRequest,
) -> ChapterPipelineResult:
    """执行唯一的章节生产主链，供单章、续写和事件 Graph 共同调用。"""
    context = request.context or build_chapter_context(
        db=db,
        novel=novel,
        target_chapter_index=request.target_chapter_index,
        task_input=request.task_input,
    )
    try:
        reference_pack = build_chapter_reference_pack(
            db,
            novel=novel,
            context=context,
        )
    except LLMRequestCancelledError:
        raise
    except ChapterReferenceRequirementError:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise ChapterReferenceRequirementError(
            f"本章样本参考检索失败，已停止正文生成：{exc}"
        ) from exc
    context = {**context, "expression_reference_pack": reference_pack}
    try:
        meme_reference_pack = build_chapter_meme_pack(
            db,
            novel=novel,
            context=context,
            story_event_id=request.story_event_id,
            cancel_check=(
                request.review_llm_config.cancel_check
                if request.review_llm_config is not None
                else (
                    request.llm_config.cancel_check
                    if request.llm_config is not None
                    else None
                )
            ),
        )
    except LLMRequestCancelledError:
        raise
    except Exception as exc:
        db.rollback()
        meme_reference_pack = {
            "status": "degraded",
            "reason": f"热梗 RAG 检索失败，已跳过：{exc}",
            "references": [],
        }
    context = {**context, "meme_reference_pack": meme_reference_pack}
    preview = ChapterPreviewPublisher(db, task, request.target_chapter_index)
    thinking = (
        ModelThinkingPublisher(
            task,
            source_step_key=f"chapter_{request.target_chapter_index}_draft",
            model_role="writer",
            model=request.llm_config.model,
            title=f"正文模型 · 第 {request.target_chapter_index} 章初稿",
            chapter_index=request.target_chapter_index,
        )
        if request.llm_config is not None
        else None
    )
    if thinking is not None:
        thinking.start()
    try:
        title, summary, content, generation_mode, llm_model, word_guard, raw_progress = (
            generate_chapter_content(
                context=context,
                llm_config=request.llm_config,
                simulated_builder=request.simulated_builder,
                preview_publisher=preview,
                thinking_publisher=thinking,
            )
        )
    except LLMRequestCancelledError:
        if thinking is not None:
            thinking.finish(status="paused")
        raise
    except Exception:
        if thinking is not None:
            thinking.finish(status="failed")
        raise
    else:
        if thinking is not None:
            thinking.finish()
    chapter_progress = normalize_chapter_progress(
        raw_progress,
        summary=summary,
        content=content,
        current_plan=request.task_input.get("chapter_plan") or {},
        next_plan=request.task_input.get("next_chapter_boundary") or {},
    )
    meme_enforcement = ensure_minimum_meme_usage(
        content=content,
        reference_pack=meme_reference_pack,
        chapter_progress=chapter_progress,
        llm_config=request.llm_config,
    )
    content = meme_enforcement["content"]
    chapter_progress = meme_enforcement["chapter_progress"]
    refreshed_word_guard = build_word_guard_report(
        content,
        get_context_chapter_word_range(context),
        attempt=int(word_guard.get("selected_attempt") or word_guard.get("attempt") or 1),
    )
    word_guard = {
        **word_guard,
        **refreshed_word_guard,
        "meme_enforcement": {
            key: value
            for key, value in meme_enforcement.items()
            if key not in {"content", "chapter_progress", "usage"}
        },
    }
    summary = chapter_progress.get("actual_summary") or summary
    context = attach_word_guard_to_context(context, word_guard)
    context = {**context, "chapter_progress": chapter_progress}
    requires_word_revision = bool(word_guard.get("needs_revision"))
    chapter = request.existing_chapter or db.scalar(
        select(Chapter).where(
            Chapter.novel_id == novel.id,
            Chapter.chapter_index == request.target_chapter_index,
        )
    )
    if chapter is None:
        chapter = Chapter(
            novel_id=novel.id,
            chapter_index=request.target_chapter_index,
            title=title,
            status="needs_word_revision" if requires_word_revision else "done",
            summary=summary,
            content=content,
            word_count=count_chapter_words(content),
            context_snapshot=context,
        )
        db.add(chapter)
    else:
        if request.force_title or not chapter.title:
            chapter.title = title
        chapter.status = "needs_word_revision" if requires_word_revision else "done"
        chapter.summary = summary
        chapter.content = content
        chapter.word_count = count_chapter_words(content)
        chapter.context_snapshot = context
    novel.current_chapter_index = max(
        novel.current_chapter_index,
        chapter.chapter_index,
    )
    db.commit()
    db.refresh(chapter)
    preview.finish(
        chapter.content or "",
        chapter_id=chapter.id,
        attempt=int(word_guard.get("selected_attempt") or word_guard.get("attempt") or 1),
    )
    emit_task_event(
        db,
        task,
        event_type="step",
        step_key=f"chapter_{chapter.chapter_index}_draft",
        status="completed",
        title=f"第 {chapter.chapter_index} 章已生成",
        message=f"《{chapter.title}》· {chapter.word_count} 字",
        progress=request.progress,
        chapter_id=chapter.id,
        chapter_index=chapter.chapter_index,
        payload={"word_guard": word_guard, "chapter_progress": chapter_progress},
    )
    sync_event_plan_after_pipeline(
        db,
        task_input=request.task_input,
        chapter=chapter,
        status="word_revision_required" if requires_word_revision else "generated",
    )

    chapter_review: dict[str, Any] = {
        "status": "skipped",
        "cycle_count": 0,
        "issue_count": 0,
        "resolved_count": 0,
        "deferred_count": 0,
        "suggestions": [],
        "reason": "word_revision_required" if requires_word_revision else "",
        "error": "",
    }
    memory_tasks: list[GenerationTask] = []
    fact_delta_id = ""
    reference_overlap: dict[str, Any] = {
        "status": "skipped",
        "has_risky_overlap": False,
        "match_count": 0,
        "matches": [],
        "rule": "",
    }
    meme_requirement_failed = False
    reused_event_memes: list[str] = []
    repeated_chapter_memes: list[str] = []
    event_meme_reuse_failed = False
    chapter_meme_repeat_failed = False
    meme_deduplication: dict[str, Any] = {
        "status": "not_required",
        "content": "",
        "phrases": [],
        "paragraph_indexes": [],
        "reason": "",
    }
    if not requires_word_revision:
        if request.defer_review:
            chapter_review = {
                **chapter_review,
                "status": "queued",
                "reason": "parallel_review_pipeline",
            }
            emit_task_event(
                db,
                task,
                event_type="chapter_review",
                step_key=f"chapter_{chapter.chapter_index}_review",
                status="pending",
                title=f"第 {chapter.chapter_index} 章已进入并行审校",
                message="正文生成继续推进；深度思考模型在独立流水线中审校本章。",
                progress=min(76, max(1, request.progress + 1)),
                chapter_id=chapter.id,
                chapter_index=chapter.chapter_index,
                payload={"parallel": True},
            )
        else:
            chapter_review = review_and_revise_chapter_once(
                db=db,
                novel=novel,
                chapter=chapter,
                task=task,
                reviewer_config=request.review_llm_config,
                writer_config=request.llm_config,
                story_event_id=request.story_event_id,
                progress=request.progress,
            )
            if chapter_review.get("status") == "applied":
                db.refresh(chapter)
                sync_event_plan_after_pipeline(
                    db,
                    task_input=request.task_input,
                    chapter=chapter,
                    status="generated",
                )
        db.refresh(chapter)
        meme_enforcement = ensure_minimum_meme_usage(
            content=chapter.content or "",
            reference_pack=meme_reference_pack,
            chapter_progress=chapter_progress,
            llm_config=request.llm_config,
        )
        chapter_progress = meme_enforcement["chapter_progress"]
        if meme_enforcement["content"] != (chapter.content or ""):
            chapter.content = meme_enforcement["content"]
            chapter.word_count = count_chapter_words(chapter.content)
            chapter.context_snapshot = {
                **(chapter.context_snapshot or {}),
                "chapter_progress": chapter_progress,
            }
            db.commit()
            db.refresh(chapter)
        # 热梗是可选表达资源。零使用不再判失败，准确和自然优先于数量。
        meme_requirement_failed = False
        candidate_phrases = [
            str(item.get("phrase") or "").strip()
            for item in (meme_reference_pack.get("references") or [])
            if str(item.get("phrase") or "").strip()
        ]
        repeated_chapter_memes = find_repeated_meme_phrases(
            chapter.content or "",
            candidate_phrases,
        )
        if repeated_chapter_memes:
            meme_deduplication = repair_repeated_meme_usage(
                content=chapter.content or "",
                phrases=repeated_chapter_memes,
                llm_config=request.llm_config,
            )
            if meme_deduplication["content"] != (chapter.content or ""):
                chapter.content = meme_deduplication["content"]
                chapter.word_count = count_chapter_words(chapter.content)
                db.commit()
                db.refresh(chapter)
            repeated_chapter_memes = find_repeated_meme_phrases(
                chapter.content or "",
                candidate_phrases,
            )
        chapter_meme_repeat_failed = bool(repeated_chapter_memes)
        reused_event_memes = find_reused_event_meme_phrases(
            chapter.content or "",
            meme_reference_pack.get("event_used_phrases") or [],
        )
        event_meme_reuse_failed = bool(reused_event_memes)
        reference_overlap = build_reference_overlap_report(
            chapter.content or "",
            reference_pack,
        )
        if (
            not request.defer_review
            and
            not reference_overlap.get("has_risky_overlap")
            and not meme_requirement_failed
            and not event_meme_reuse_failed
            and not chapter_meme_repeat_failed
        ):
            fact_delta = sync_chapter_fact_delta(
                db,
                novel=novel,
                chapter=chapter,
                chapter_progress=chapter_progress,
            )
            fact_delta_id = str(fact_delta.id) if fact_delta is not None else ""
            emit_task_event(
                db,
                task,
                event_type="fact_delta",
                step_key=f"chapter_{chapter.chapter_index}_fact_delta",
                status="completed",
                title=f"第 {chapter.chapter_index} 章轻量事实已同步",
                message="后续章节可立即读取；完整记忆与时间线继续在后台合并",
                progress=min(96, max(0, request.progress + 2)),
                chapter_id=chapter.id,
                chapter_index=chapter.chapter_index,
                payload={"memory_id": fact_delta_id, "lightweight": True},
            )
            memory_tasks = enqueue_chapter_memory_tasks(
                db,
                novel=novel,
                chapters=[chapter],
                source_task=task,
                story_event_id=request.story_event_id,
                source_progress=min(96, max(0, request.progress + 2)),
                sync_reason=request.memory_sync_reason,
                expected_total=request.memory_expected_total,
            )

    review_failed = chapter_review.get("status") == "failed"
    overlap_failed = bool(reference_overlap.get("has_risky_overlap"))
    db.refresh(chapter)
    meme_usage = (
        build_final_meme_usage(
            meme_reference_pack,
            chapter_progress,
            chapter.content or "",
        )
        if not requires_word_revision
        else {
            "candidate_count": len(meme_reference_pack.get("references") or []),
            "adopted_count": 0,
            "adopted": [],
            "status": "pending_word_revision",
        }
    )
    if overlap_failed:
        chapter.status = "needs_quality_revision"
    if meme_requirement_failed:
        chapter.status = "needs_quality_revision"
    if event_meme_reuse_failed:
        chapter.status = "needs_quality_revision"
    chapter.context_snapshot = {
        **(chapter.context_snapshot or {}),
        "expression_reference": {
            "status": reference_pack.get("status", "skipped"),
            "reference_count": len(reference_pack.get("references") or []),
            "total_chars": reference_pack.get("total_chars", 0),
            "embedding_model": reference_pack.get("embedding_model", ""),
            "overlap_guard": reference_overlap,
        },
        "meme_reference": {
            "status": meme_reference_pack.get("status", "skipped"),
            "reason": meme_reference_pack.get("reason", ""),
            "candidate_count": len(meme_reference_pack.get("references") or []),
            "retrieved_count": int(meme_reference_pack.get("retrieved_count") or 0),
            "filtered_low_relevance_count": int(
                meme_reference_pack.get("filtered_low_relevance_count") or 0
            ),
            "qualified_count": int(
                meme_reference_pack.get("qualified_count") or 0
            ),
            "scene_rerank_status": meme_reference_pack.get(
                "scene_rerank_status",
                "not_run",
            ),
            "scene_rerank_candidate_count": int(
                meme_reference_pack.get("scene_rerank_candidate_count") or 0
            ),
            "scene_rejected_count": int(
                meme_reference_pack.get("scene_rejected_count") or 0
            ),
            "scene_fit_threshold": int(
                meme_reference_pack.get("scene_fit_threshold") or 0
            ),
            "relevance_threshold": meme_reference_pack.get(
                "relevance_threshold",
                {},
            ),
            "event_used_phrases": meme_reference_pack.get(
                "event_used_phrases",
                [],
            ),
            "reused_event_phrases": reused_event_memes,
            "repeated_chapter_phrases": repeated_chapter_memes,
            "deduplication": {
                key: value
                for key, value in meme_deduplication.items()
                if key != "content"
            },
            "embedding_model": meme_reference_pack.get("embedding_model", ""),
            "usage": meme_usage,
            "enforcement": {
                key: value
                for key, value in meme_enforcement.items()
                if key not in {"content", "chapter_progress", "usage"}
            },
        },
    }
    db.commit()
    requires_quality_revision = bool(
        meme_requirement_failed
        or event_meme_reuse_failed
        or chapter_meme_repeat_failed
        or (
            not request.defer_review
            and request.strict_quality_gate
            and (review_failed or overlap_failed)
        )
    )
    if not requires_word_revision and not requires_quality_revision:
        emit_task_event(
            db,
            task,
            event_type="chapter_meme",
            step_key=f"chapter_{chapter.chapter_index}_meme",
            status="completed",
            title=f"第 {chapter.chapter_index} 章热梗匹配完成",
            message=(
                "最终采用：" + "、".join(item["phrase"] for item in meme_usage["adopted"])
                if meme_usage["adopted"]
                else meme_reference_pack.get("reason") or "本章未采用热梗"
            ),
            progress=min(96, max(0, request.progress + 2)),
            chapter_id=chapter.id,
            chapter_index=chapter.chapter_index,
            payload={
                "meme_usage": meme_usage,
                "retrieved_count": int(meme_reference_pack.get("retrieved_count") or 0),
                "filtered_low_relevance_count": int(
                    meme_reference_pack.get("filtered_low_relevance_count") or 0
                ),
                "qualified_count": int(
                    meme_reference_pack.get("qualified_count") or 0
                ),
                "scene_rerank_status": meme_reference_pack.get(
                    "scene_rerank_status",
                    "not_run",
                ),
                "scene_rerank_candidate_count": int(
                    meme_reference_pack.get("scene_rerank_candidate_count") or 0
                ),
                "scene_rejected_count": int(
                    meme_reference_pack.get("scene_rejected_count") or 0
                ),
                "scene_fit_threshold": int(
                    meme_reference_pack.get("scene_fit_threshold") or 0
                ),
                "deduplication": {
                    key: value
                    for key, value in meme_deduplication.items()
                    if key != "content"
                },
                "relevance_threshold": meme_reference_pack.get(
                    "relevance_threshold",
                    {},
                ),
            },
        )
    failure = (
        AgentFailure(
            code=(
                "event_meme_reused"
                if event_meme_reuse_failed
                else "chapter_meme_repeated"
                if chapter_meme_repeat_failed
                else "meme_requirement_failed"
                if meme_requirement_failed
                else "reference_overlap_detected"
                if overlap_failed
                else "chapter_review_failed"
            ),
            message=(
                "本章复用了同一剧情事件其它章节已经采用的热梗："
                + "、".join(reused_event_memes)
                if event_meme_reuse_failed
                else "本章同一热梗重复出现且自动改写失败："
                + "、".join(repeated_chapter_memes)
                if chapter_meme_repeat_failed
                else "本章已检索到热梗候选，但正文模型未能自然落地至少一条"
                if meme_requirement_failed
                else "生成正文与样本参考存在过长的连续相同文本"
                if overlap_failed
                else str(chapter_review.get("error") or "章节审校失败")
            ),
            stage=(
                "event_meme_reuse_guard"
                if event_meme_reuse_failed
                else "meme_deduplication"
                if chapter_meme_repeat_failed
                else "meme_usage_enforcement"
                if meme_requirement_failed
                else "reference_overlap_guard"
                if overlap_failed
                else "chapter_review"
            ),
            retryable=True,
            details={"chapter_id": str(chapter.id)},
        )
        if (
            review_failed
            or overlap_failed
            or meme_requirement_failed
            or event_meme_reuse_failed
            or chapter_meme_repeat_failed
        )
        else None
    )
    status = (
        "degraded"
        if (
            review_failed
            or overlap_failed
            or meme_requirement_failed
            or event_meme_reuse_failed
            or chapter_meme_repeat_failed
        )
        else "success"
    )
    contract = agent_contract(
        "ChapterWritingAgent",
        "chapter_pipeline",
        status=status,
        generation_mode=generation_mode,
        degraded=(
            review_failed
            or overlap_failed
            or meme_requirement_failed
            or event_meme_reuse_failed
            or chapter_meme_repeat_failed
        ),
        failure=failure,
    )
    return ChapterPipelineResult(
        chapter=chapter,
        context=context,
        draft_content=content,
        generation_mode=generation_mode,
        llm_model=llm_model,
        word_guard=word_guard,
        chapter_progress=chapter_progress,
        chapter_review=chapter_review,
        reference_pack=reference_pack,
        reference_overlap=reference_overlap,
        memory_tasks=memory_tasks,
        fact_delta_id=fact_delta_id,
        requires_word_revision=requires_word_revision,
        requires_quality_revision=requires_quality_revision,
        contract=contract,
    )
