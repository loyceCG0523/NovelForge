"""剧情事件级质量审校服务。

章节连续性审校只看单章是否违背上下文；EventQualityChecker 站在 4-12 章事件层面，
检查事件是否闭环、节奏是否升级、人物推进是否成立，以及结尾是否兑现事件目标。
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.event_chapter_plan import EventChapterPlan
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue
from app.models.story_event import StoryEvent
from app.services.llm_client import LLMClient, LLMConfig, build_context_token_budget
from app.services.pacing_plan import is_closing_event
from app.services.task_events import ModelThinkingPublisher, emit_task_event


EVENT_QUALITY_SOURCE = "event_quality_checker"
EVENT_QUALITY_NOTE_STATUS = "quality_note"
ALLOWED_EVENT_ISSUE_TYPES = {
    "event_closure",
    "event_pacing",
    "event_conflict",
    "event_character_arc",
    "event_character_consistency",
    "event_logic",
    "event_timeline",
    "event_resource_state",
    "event_foreshadowing",
    "event_repetition",
    "event_plan_consistency",
    "event_supporting_element_dominance",
    "event_genre_delivery",
    "event_meme_fit",
    "event_prose_style",
}
ALLOWED_SEVERITIES = {"low", "medium", "high"}


def _compact_chapter(chapter: Chapter | None) -> dict[str, Any]:
    """整理事件审校章节材料；保留完整正文才能判断跨章因果。"""
    if chapter is None:
        return {}
    content = chapter.content or ""
    return {
        "id": str(chapter.id),
        "chapter_index": chapter.chapter_index,
        "title": chapter.title,
        "summary": chapter.summary,
        "word_count": chapter.word_count,
        "content": content,
    }


def _load_event_material(db: Session, story_event: StoryEvent) -> tuple[list[EventChapterPlan], list[dict[str, Any]], list[ReviewIssue]]:
    """读取事件审校需要的章节计划、章节正文摘要和已有章节风险。"""
    plans = db.scalars(
        select(EventChapterPlan)
        .where(EventChapterPlan.story_event_id == story_event.id)
        .order_by(EventChapterPlan.chapter_index.asc())
    ).all()
    chapter_ids = [plan.chapter_id for plan in plans if plan.chapter_id]
    chapters_by_id = {}
    if chapter_ids:
        chapters = db.scalars(select(Chapter).where(Chapter.id.in_(chapter_ids))).all()
        chapters_by_id = {chapter.id: chapter for chapter in chapters}

    chapter_material = [
        {
            "plan": {
                "id": str(plan.id),
                "chapter_index": plan.chapter_index,
                "title": plan.title,
                "function": plan.function,
                "core_event": plan.core_event,
                "ending_hook": plan.ending_hook,
                "status": plan.status,
            },
            "chapter": _compact_chapter(chapters_by_id.get(plan.chapter_id)) if plan.chapter_id else {},
        }
        for plan in plans
    ]

    chapter_issues = []
    if chapter_ids:
        chapter_issues = db.scalars(
            select(ReviewIssue)
            .where(ReviewIssue.novel_id == story_event.novel_id, ReviewIssue.chapter_id.in_(chapter_ids))
            .order_by(ReviewIssue.updated_at.desc())
        ).all()
    return plans, chapter_material, chapter_issues


def build_event_quality_prompt(
    novel: Novel,
    story_event: StoryEvent,
    chapter_material: list[dict[str, Any]],
    chapter_issues: list[ReviewIssue],
) -> list[dict[str, str]]:
    """构造事件级质量审校 prompt。"""
    payload = story_event.payload or {}
    compact_issues = [
        {
            "chapter_id": str(issue.chapter_id) if issue.chapter_id else None,
            "issue_type": issue.issue_type,
            "severity": issue.severity,
            "status": issue.status,
            "message": issue.message,
        }
        for issue in chapter_issues[:20]
    ]
    event_input = {
        "novel": {
            "title": novel.title,
            "genre": novel.genre,
            "premise": novel.premise,
        },
        "hard_facts": {
            "characters": (novel.brief or {}).get("characters", []),
            "story_era": (novel.brief or {}).get("story_era", ""),
            "story_location": (novel.brief or {}).get("story_location", ""),
            "worldview": (novel.brief or {}).get("worldview", ""),
            "forbidden_content": (novel.brief or {}).get("forbidden_content", ""),
        },
        "story_event": {
            "title": story_event.title,
            "goal": story_event.goal,
            "core_conflict": story_event.core_conflict,
            "genre_alignment": payload.get("genre_alignment", ""),
            "dramatic_escalation": payload.get("dramatic_escalation", ""),
            "major_reversal": payload.get("major_reversal", ""),
            "reader_payoff": payload.get("reader_payoff", ""),
            "narrative_contract": payload.get("narrative_contract", {}),
            "planning_quality": payload.get("planning_quality", {}),
            "next_event_hook": story_event.next_event_hook,
            "completion_criteria": payload.get("completion_criteria", []),
            "planned_chapter_count": story_event.planned_chapter_count,
            "generated_chapter_count": story_event.generated_chapter_count,
        },
        "chapter_material": chapter_material,
        "chapter_level_issues": compact_issues,
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 NovelForge 的 EventQualityChecker。你不负责润色单章，"
                "只判断一组章节作为一个完整剧情事件是否成立。只输出 JSON，不要输出 Markdown。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请审校这个剧情事件的整体质量。\n"
                "重点检查：事件起因是否明确，冲突是否逐章升级，是否存在重复空转；"
                "对照 narrative_contract 检查事件是否兑现本书主类型承诺、转折和阶段回报；"
                "职业、技能、身份、设定名词或日常流程若不是主类型核心，只能承担能力、压力、代价或翻盘工具，"
                "不能连续主导章节，也不能成为人物固定口癖、感情比喻或主要笑点；流程完成本身不算读者回报。"
                "若主类型是喜剧，逐章指出实际成立的因果型喜剧节拍；职业术语反差、网络热梗、轻微吐槽和旁白评价不计数，"
                "不能因为计划声称‘笑点密集’就判定兑现。"
                "逐条核对正文使用的网络热梗是否符合真实含义、人物关系、熟悉程度、情绪、语域和当前话题，"
                "并且有自然铺垫、对方回应或剧情后果；任何一项不成立都应建议删除。"
                "若多章持续出现提纲式短句、动作逐条罗列、全员完整书面对白、缺少自然停顿改口或句间承接，"
                "或对白只有台词清单而没有‘刺激—人物化理解/回避—可见反应—对方接招—局面变化’，"
                "旁白直接宣布情绪而没有关系微动作证据，或把甲的台词与乙的反应、心理、判断、发言塞进同一段，"
                "应作为事件级语言风格问题指出。"
                "检查相邻章节是否由上一章未完回应、动作或后果接入下一章第一拍；若反复总结式收尾后另起炉灶，"
                "也应判为事件级节奏问题。此规则适用于所有题材，不预设具体类型。"
                "人物关系/动机是否推进，人物行为是否符合人物档案、职业能力、基本常识和安全意识；"
                "检查每个物品或资源由谁持有、谁能使用、状态如何变化，不能只检查名称是否重复；"
                "检查时间线、因果链和现实设备用途，不能把不会做饭、不善社交等局部弱点泛化为低常识或低专业能力；"
                "伏笔是否被推进或回收，结尾是否兑现事件目标，"
                "是否留下合理的下一事件钩子。\n"
                "输出格式必须为：\n"
                "{\n"
                '  "scores": {"closure": 0-100, "pacing": 0-100, "character_arc": 0-100, "foreshadowing": 0-100, "overall": 0-100},\n'
                '  "summary": "一句话总结事件质量",\n'
                '  "strengths": ["优点"],\n'
                '  "issues": [\n'
                "    {\n"
                '      "issue_type": "event_closure|event_pacing|event_conflict|event_character_arc|event_character_consistency|event_logic|event_timeline|event_resource_state|event_foreshadowing|event_repetition|event_plan_consistency|event_supporting_element_dominance|event_genre_delivery|event_meme_fit|event_prose_style",\n'
                '      "severity": "low|medium|high",\n'
                '      "message": "给用户看的简短问题说明",\n'
                '      "evidence": "问题依据，指出章节或计划",\n'
                '      "suggestion": "建议如何修复",\n'
                '      "affected_chapter_indexes": [1, 2]\n'
                "    }\n"
                "  ],\n"
                '  "repair_strategy": "建议优先修复方式"\n'
                "}\n\n"
                f"事件材料：\n{event_input}"
            ),
        },
    ]


def _score(value: Any, default: int = 75) -> int:
    """把模型评分裁剪到 0-100。"""
    try:
        return max(0, min(int(value), 100))
    except (TypeError, ValueError):
        return default


def normalize_event_quality_result(raw: dict[str, Any]) -> dict[str, Any]:
    """清洗事件级审校结果，保证可安全落库。"""
    raw_scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
    scores = {
        "closure": _score(raw_scores.get("closure")),
        "pacing": _score(raw_scores.get("pacing")),
        "character_arc": _score(raw_scores.get("character_arc")),
        "foreshadowing": _score(raw_scores.get("foreshadowing")),
        "overall": _score(raw_scores.get("overall")),
    }

    normalized_issues: list[dict[str, Any]] = []
    raw_issues = raw.get("issues") if isinstance(raw.get("issues"), list) else []
    for issue in raw_issues:
        if not isinstance(issue, dict):
            continue
        message = str(issue.get("message") or "").strip()
        if not message:
            continue
        issue_type = str(issue.get("issue_type") or "event_plan_consistency").strip()
        if issue_type not in ALLOWED_EVENT_ISSUE_TYPES:
            issue_type = "event_plan_consistency"
        severity = str(issue.get("severity") or "medium").strip().lower()
        if severity not in ALLOWED_SEVERITIES:
            severity = "medium"
        affected = issue.get("affected_chapter_indexes")
        if not isinstance(affected, list):
            affected = []
        normalized_issues.append(
            {
                "issue_type": issue_type,
                "severity": severity,
                "message": message,
                "evidence": str(issue.get("evidence") or ""),
                "suggestion": str(issue.get("suggestion") or ""),
                "affected_chapter_indexes": [int(item) for item in affected if str(item).isdigit()],
            }
        )

    return {
        "scores": scores,
        "summary": str(raw.get("summary") or "事件级审校已完成。"),
        "strengths": raw.get("strengths") if isinstance(raw.get("strengths"), list) else [],
        "issues": normalized_issues[:12],
        "repair_strategy": str(raw.get("repair_strategy") or ""),
    }


def _build_rule_based_quality_result(
    story_event: StoryEvent,
    plans: list[EventChapterPlan],
    chapter_material: list[dict[str, Any]],
    chapter_issues: list[ReviewIssue],
) -> dict[str, Any]:
    """无 LLM 时执行基础规则审校，保证事件页有可解释的质量状态。"""
    issues: list[dict[str, Any]] = []
    generated_count = len([item for item in chapter_material if item.get("chapter")])
    if story_event.planned_chapter_count and generated_count < story_event.planned_chapter_count:
        issues.append(
            {
                "issue_type": "event_closure",
                "severity": "high",
                "message": f"事件计划共 {story_event.planned_chapter_count} 章，目前只生成 {generated_count} 章，事件尚未闭环。",
                "evidence": "章节计划存在未生成章节。",
                "suggestion": "从第一个待生成章节继续生成，或重跑整个事件。",
                "affected_chapter_indexes": [plan.chapter_index for plan in plans if not plan.chapter_id],
            }
        )

    functions = [plan.function for plan in plans if plan.function]
    if len(functions) >= 4 and len(set(functions)) <= 2:
        issues.append(
            {
                "issue_type": "event_pacing",
                "severity": "medium",
                "message": "多章章节功能过于集中，可能存在节奏重复或冲突升级不足。",
                "evidence": f"章节功能分布：{'、'.join(functions)}",
                "suggestion": "重写事件计划，让章节承担铺垫、升级、反转、收束等不同功能。",
                "affected_chapter_indexes": [plan.chapter_index for plan in plans],
            }
        )

    if (
        story_event.generated_chapter_count >= story_event.planned_chapter_count
        and not story_event.next_event_hook
        and not is_closing_event(story_event.payload)
    ):
        issues.append(
            {
                "issue_type": "event_closure",
                "severity": "low",
                "message": "事件已生成完成，但缺少下一事件钩子。",
                "evidence": "StoryEvent.next_event_hook 为空。",
                "suggestion": "在收束章补充一个自然引出的下一阶段问题。",
                "affected_chapter_indexes": [story_event.end_chapter_index] if story_event.end_chapter_index else [],
            }
        )

    open_chapter_issues = [issue for issue in chapter_issues if issue.status in {"open", "system_deferred"}]
    if open_chapter_issues:
        issues.append(
            {
                "issue_type": "event_plan_consistency",
                "severity": "medium",
                "message": f"事件中仍有 {len(open_chapter_issues)} 条章节级系统处理项，可能影响整体闭环。",
                "evidence": "章节连续性审校仍存在系统处理中事项。",
                "suggestion": "由系统优先重跑或修订影响最大的章节，再重新执行事件级审校。",
                "affected_chapter_indexes": [],
            }
        )

    penalty = min(45, len(issues) * 12)
    return {
        "scores": {
            "closure": 100 - (25 if generated_count < (story_event.planned_chapter_count or generated_count) else 0),
            "pacing": 82 - (18 if any(issue["issue_type"] == "event_pacing" for issue in issues) else 0),
            "character_arc": 76,
            "foreshadowing": 74,
            "overall": max(40, 82 - penalty),
        },
        "summary": "已完成事件级规则审校；配置 LLM 后可获得更细的剧情质量判断。",
        "strengths": ["章节计划和正文已形成可审校的事件结构。"] if generated_count else [],
        "issues": issues,
        "repair_strategy": "优先生成缺失章节并处理系统审校项，再检查事件收束章。",
    }


def check_event_quality(
    db: Session,
    novel: Novel,
    story_event: StoryEvent,
    llm_config: LLMConfig | None,
    task: GenerationTask | None = None,
) -> dict[str, Any]:
    """执行事件级质量审校，返回质量报告。"""
    plans, chapter_material, chapter_issues = _load_event_material(db, story_event)
    if llm_config is None:
        return normalize_event_quality_result(
            _build_rule_based_quality_result(story_event, plans, chapter_material, chapter_issues)
        )

    messages = build_event_quality_prompt(
        novel,
        story_event,
        chapter_material,
        chapter_issues,
    )
    context_budget = build_context_token_budget(llm_config, messages)
    prompt_chars = sum(len(str(message.get("content") or "")) for message in messages)
    timeout_seconds = 60
    output_chars = 0
    first_delta_at: float | None = None
    started_at = time.monotonic()

    def on_raw_delta(delta: str) -> None:
        nonlocal output_chars, first_delta_at
        if first_delta_at is None:
            first_delta_at = time.monotonic()
        output_chars += len(delta)

    last_activity_notice_at = 0.0
    review_thinking = (
        ModelThinkingPublisher(
            task,
            source_step_key="event_review_stream",
            model_role="reviewer",
            model=llm_config.model,
            title="审校模型 · 事件整体复检",
        )
        if task is not None
        else None
    )
    if review_thinking is not None:
        review_thinking.start()

    def on_activity(activity: dict[str, Any]) -> None:
        nonlocal last_activity_notice_at
        if review_thinking is not None:
            review_thinking.append_activity(activity)
        if task is None:
            return
        now = time.monotonic()
        if now - last_activity_notice_at < 10:
            return
        last_activity_notice_at = now
        emit_task_event(
            db,
            task,
            event_type="review",
            step_key="event_review_stream",
            status="running",
            title="深度思考模型正在执行事件总审",
            message=(
                "连接持续活跃；"
                f"已接收思考 {int(activity.get('reasoning_chars') or 0)} 字符、"
                f"最终输出 {int(activity.get('output_chars') or 0)} 字符"
            ),
            progress=79,
            payload={"stream_activity": activity},
        )

    client = LLMClient(
        replace(
            llm_config,
            timeout_seconds=float(timeout_seconds),
            total_timeout_seconds=None,
            max_retries=(
                0
                if prompt_chars > 30000
                else min(llm_config.max_retries, 1)
            ),
        )
    )
    try:
        _, parsed = client.complete_json(
            messages,
            stream=True,
            on_raw_delta=on_raw_delta,
            on_activity=on_activity,
        )
    except Exception:
        if review_thinking is not None:
            review_thinking.finish(status="failed")
        raise
    else:
        if review_thinking is not None:
            review_thinking.finish()
    report = normalize_event_quality_result(parsed)
    report["request_telemetry"] = {
        **context_budget,
        "streaming": True,
        "prompt_chars": prompt_chars,
        "activity_timeout_seconds": timeout_seconds,
        "first_token_timeout_seconds": timeout_seconds,
        "total_timeout_seconds": None,
        "first_delta_seconds": (
            round(first_delta_at - started_at, 3)
            if first_delta_at is not None
            else None
        ),
        "output_chars": output_chars,
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
        "max_retries": (
            0
            if prompt_chars > 30000
            else min(llm_config.max_retries, 1)
        ),
        "transport": client.last_request_telemetry,
    }
    return report


def sync_event_quality_issues(
    db: Session,
    novel: Novel,
    story_event: StoryEvent,
    llm_config: LLMConfig | None,
    task: GenerationTask | None = None,
) -> dict[str, Any]:
    """同步事件级质量记录，并把质量报告写回 StoryEvent.payload。"""
    report = check_event_quality(
        db=db,
        novel=novel,
        story_event=story_event,
        llm_config=llm_config,
        task=task,
    )
    existing_issues = db.scalars(
        select(ReviewIssue).where(
            ReviewIssue.novel_id == novel.id,
            ReviewIssue.chapter_id.is_(None),
            ReviewIssue.issue_type.like("event_%"),
        )
    ).all()
    for issue in existing_issues:
        payload = issue.payload or {}
        if (
            issue.status in {"open", EVENT_QUALITY_NOTE_STATUS}
            and payload.get("source") == EVENT_QUALITY_SOURCE
            and payload.get("story_event_id") == str(story_event.id)
        ):
            db.delete(issue)

    new_issues = [
        ReviewIssue(
            novel_id=novel.id,
            chapter_id=None,
            issue_type=item["issue_type"],
            severity=item["severity"],
            status="open" if item["severity"] == "high" else EVENT_QUALITY_NOTE_STATUS,
            message=item["message"],
            payload={
                "source": EVENT_QUALITY_SOURCE,
                "auto_generated": True,
                "requires_user_action": item["severity"] == "high",
                "story_event_id": str(story_event.id),
                "evidence": item["evidence"],
                "suggestion": item["suggestion"],
                "affected_chapter_indexes": item["affected_chapter_indexes"],
            },
        )
        for item in report["issues"]
    ]
    db.add_all(new_issues)

    story_event.payload = {
        **(story_event.payload or {}),
        "quality_report": report,
    }
    db.commit()
    for issue in new_issues:
        db.refresh(issue)

    chapter_ids = [
        plan.chapter_id
        for plan in db.scalars(select(EventChapterPlan).where(EventChapterPlan.story_event_id == story_event.id)).all()
        if plan.chapter_id
    ]
    open_chapter_risks = 0
    if chapter_ids:
        open_chapter_risks = db.scalar(
            select(func.count())
            .select_from(ReviewIssue)
            .where(
                ReviewIssue.novel_id == novel.id,
                ReviewIssue.chapter_id.in_(chapter_ids),
                ReviewIssue.status.in_(["open", "system_deferred"]),
            )
        ) or 0
    blocking_event_risks = len([issue for issue in new_issues if issue.status == "open"])
    story_event.remaining_open_risks = open_chapter_risks + blocking_event_risks
    db.commit()

    return {
        "quality_report": report,
        "created_issues": len(new_issues),
        "remaining_open_risks": story_event.remaining_open_risks,
    }
