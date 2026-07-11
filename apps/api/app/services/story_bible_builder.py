"""作品圣经生成器。

它把用户的起始需求文档升级为全书级约束，后续章节生成和剧情事件规划都应优先读取它。
"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.novel import Novel
from app.models.sample_analysis import SampleAnalysis
from app.models.story_bible import StoryBible
from app.services.llm_client import LLMClient, LLMConfig


STORY_BIBLE_SCHEMA_VERSION = "story_bible.v1"


def build_fallback_story_bible(novel: Novel, sample_style_references: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """没有 LLM 配置时，根据起始需求生成保守可用的作品圣经草案。"""
    brief = novel.brief or {}
    protagonist = brief.get("protagonist") or "主角"
    sample_style_references = sample_style_references or []
    sample_style_rules = _build_sample_style_rules(sample_style_references)
    return {
        "schema_version": STORY_BIBLE_SCHEMA_VERSION,
        "positioning": {
            "work_type": brief.get("work_type") or "长篇小说",
            "genre": novel.genre,
            "target_readers": brief.get("target_readers") or "偏好强剧情推进和持续悬念的网文读者",
            "core_selling_points": _split_to_list(brief.get("selling_points")) or ["强冲突", "连续悬念", "人物关系张力"],
            "reader_expectation": brief.get("reader_expectation") or "每个剧情事件都带来明确爽点、反转或关系变化",
        },
        "protagonist": {
            "name": _guess_name(protagonist),
            "identity": protagonist,
            "personality": brief.get("protagonist_personality") or "目标明确，有行动力，但存在需要成长的短板",
            "goal": brief.get("protagonist_goal") or brief.get("plot_direction") or novel.premise,
            "flaw": brief.get("protagonist_flaw") or "容易被既有判断限制",
            "growth_arc": brief.get("protagonist_growth_arc") or "从被事件推着走，逐步成长为能主动选择和承担后果的人",
        },
        "main_characters": _build_default_characters(brief, protagonist),
        "world_rules": {
            "background": brief.get("worldview") or novel.premise,
            "rules": _split_to_list(brief.get("world_rules")) or ["所有关键设定必须在后续章节保持一致"],
            "constraints": _split_to_list(brief.get("world_constraints")) or [],
        },
        "main_plot": {
            "premise": novel.premise,
            "long_term_goal": brief.get("plot_direction") or novel.premise,
            "core_conflict": brief.get("core_conflict") or "主角目标与外部压力持续冲突",
            "volume_direction": _split_to_list(brief.get("volume_direction")) or [],
            "long_term_foreshadowing": _split_to_list(brief.get("long_term_foreshadowing")) or [],
        },
        "style_rules": {
            "style_reference": brief.get("style_reference") or "具体、克制、重行动和场景细节",
            "sample_style_references": sample_style_references,
            "sample_style_rules": sample_style_rules,
            "anti_ai_rules": [
                "避免模板化转折句",
                "避免空泛情绪词",
                "避免解释性独白",
                "避免用总结替代场景行动",
            ],
            "forbidden_content": _split_to_list(brief.get("forbidden_content")),
        },
        "generation_policy": {
            "chapter_word_min": _safe_int(brief.get("chapter_word_min"), 2000),
            "chapter_word_max": _safe_int(brief.get("chapter_word_max"), 3000),
            "event_chapter_count": 8,
            "automation_strategy": brief.get("automation_strategy") or "低风险自动推进，高风险记录并自动尝试修复",
        },
    }


def build_story_bible_prompt(
    novel: Novel,
    extra_input: dict | None = None,
    sample_style_references: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """构建作品圣经生成 Prompt，要求模型只返回 JSON。"""
    brief = novel.brief or {}
    sample_style_references = sample_style_references or []
    payload = {
        "task": "build_story_bible",
        "novel": {
            "title": novel.title,
            "genre": novel.genre,
            "target_words": novel.target_words,
            "premise": novel.premise,
            "brief": brief,
        },
        "sample_style_references": sample_style_references,
        "sample_reference_policy": {
            "priority": "high",
            "usage": "把样本报告作为文风、节奏、对白、伏笔密度、反 AI 规则的强约束。",
            "forbidden": "不得复制样本人物、剧情、设定名词或原文表达。",
        },
        "extra_input": extra_input or {},
        "required_schema": build_fallback_story_bible(novel, sample_style_references),
    }
    return [
        {
            "role": "system",
            "content": "\n".join(
                [
                    "你是 NovelForge 的作品圣经 Agent。",
                    "你的任务是把用户的起始需求文档整理成全书级结构化设定。",
                    "输出必须是 JSON 对象，必须覆盖 required_schema 的所有一级字段。",
                    "不要写散文式说明，不要输出 Markdown，不要输出 JSON 以外的文字。",
                ]
            ),
        },
        {
            "role": "user",
            "content": "\n".join(
                [
                    "请生成一份可直接约束长篇小说自动创作的作品圣经。",
                    "如果 sample_style_references 不为空，必须把其中的量化指标和 LLM 风格策略沉淀到 style_rules、generation_policy 和人物对白约束中。",
                    "人物设定要关注身份、年龄/阶段、性格、家庭/背景、目标、缺陷、成长线；不要把普通事件误当成人物设定。",
                    "规则要具体，可被后续章节生成器执行。",
                    json.dumps(payload, ensure_ascii=False, indent=2),
                ]
            ),
        },
    ]


def normalize_story_bible(
    raw: dict[str, Any],
    novel: Novel,
    sample_style_references: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """清洗模型输出，保证关键结构总是存在。"""
    fallback = build_fallback_story_bible(novel, sample_style_references)
    normalized = {**fallback, **(raw or {})}
    for key, fallback_value in fallback.items():
        if not normalized.get(key):
            normalized[key] = fallback_value
    normalized["schema_version"] = STORY_BIBLE_SCHEMA_VERSION
    return normalized


def generate_story_bible_content(
    novel: Novel,
    llm_config: LLMConfig | None,
    extra_input: dict | None = None,
    sample_style_references: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], str]:
    """生成作品圣经内容，返回内容和生成模式。"""
    sample_style_references = sample_style_references or []
    if llm_config is None:
        return build_fallback_story_bible(novel, sample_style_references), "fallback"

    try:
        _, parsed = LLMClient(llm_config).complete_json(
            build_story_bible_prompt(novel, extra_input, sample_style_references)
        )
        return normalize_story_bible(parsed, novel, sample_style_references), "llm"
    except Exception as exc:
        content = build_fallback_story_bible(novel, sample_style_references)
        content["builder_error"] = str(exc)
        return content, "fallback_after_llm_error"


def upsert_story_bible(
    db: Session,
    novel: Novel,
    content: dict[str, Any],
    source: str,
    summary: str | None = None,
) -> StoryBible:
    """创建或更新一部作品的 StoryBible。"""
    story_bible = db.scalar(select(StoryBible).where(StoryBible.novel_id == novel.id))
    if story_bible is None:
        story_bible = StoryBible(novel_id=novel.id)
        db.add(story_bible)
    else:
        story_bible.version += 1

    story_bible.status = "active"
    story_bible.source = source
    story_bible.content = content
    story_bible.summary = summary or build_story_bible_summary(content, novel)
    db.commit()
    db.refresh(story_bible)
    return story_bible


def get_or_build_story_bible_context(db: Session, novel: Novel) -> dict[str, Any]:
    """给上下文构建器读取的轻量 StoryBible 视图。"""
    story_bible = db.scalar(select(StoryBible).where(StoryBible.novel_id == novel.id))
    if story_bible is None:
        content = build_fallback_story_bible(novel)
        return {
            "status": "implicit",
            "source": "fallback_from_brief",
            "summary": build_story_bible_summary(content, novel),
            "content": content,
            "locked_fields": {},
        }
    return {
        "id": str(story_bible.id),
        "status": story_bible.status,
        "source": story_bible.source,
        "version": story_bible.version,
        "summary": story_bible.summary,
        "content": story_bible.content or {},
        "locked_fields": story_bible.locked_fields or {},
    }


def get_sample_style_reference_context(db: Session, novel: Novel, limit: int = 3) -> list[dict[str, Any]]:
    """读取当前作品已完成样本报告，压缩成规划和生成可复用的风格参考。"""
    selected_ids = _parse_sample_reference_ids((novel.brief or {}).get("sample_reference_ids"))
    if selected_ids:
        sample_analyses = db.scalars(
            select(SampleAnalysis)
            .where(
                SampleAnalysis.owner_id == novel.owner_id,
                SampleAnalysis.id.in_(selected_ids),
                SampleAnalysis.status.in_(["completed", "active"]),
            )
            .order_by(SampleAnalysis.updated_at.desc())
            .limit(limit)
        ).all()
    else:
        sample_analyses = db.scalars(
            select(SampleAnalysis)
            .where(
                SampleAnalysis.novel_id == novel.id,
                SampleAnalysis.status.in_(["completed", "active"]),
            )
            .order_by(SampleAnalysis.updated_at.desc())
            .limit(limit)
        ).all()

    references = []
    for item in sample_analyses:
        report = item.report or {}
        strategy = report.get("llm_style_strategy") or {}
        references.append(
            {
                "id": str(item.id),
                "sample_title": item.sample_title,
                "source_genre": item.source_genre,
                "summary": item.summary,
                "source_word_count": item.source_word_count,
                "chapter_count": item.chapter_count,
                "transferable_style_vector": report.get("transferable_style_vector", item.metrics or {}),
                "style_fingerprint": report.get("style_fingerprint", {}),
                "dialogue_style": report.get("dialogue_style", {}),
                "pacing_model": report.get("pacing_model", {}),
                "llm_style_strategy": {
                    "available": bool(strategy.get("available")),
                    "style_summary": strategy.get("style_summary", ""),
                    "generation_guidelines": strategy.get("generation_guidelines", []),
                    "anti_ai_guidelines": strategy.get("anti_ai_guidelines", []),
                    "dialogue_guidelines": strategy.get("dialogue_guidelines", []),
                    "pacing_guidelines": strategy.get("pacing_guidelines", []),
                    "risk_notes": strategy.get("risk_notes", []),
                },
            }
        )
    return references


def _parse_sample_reference_ids(value: Any) -> list[UUID]:
    """从作品 brief 中解析用户选择的样本报告 ID。"""
    if not isinstance(value, list):
        return []
    parsed = []
    for item in value:
        try:
            parsed.append(UUID(str(item)))
        except (TypeError, ValueError):
            continue
    return parsed


def build_story_bible_summary(content: dict[str, Any], novel: Novel) -> str:
    """生成一句可在界面和任务结果中展示的作品圣经摘要。"""
    positioning = content.get("positioning") or {}
    main_plot = content.get("main_plot") or {}
    protagonist = content.get("protagonist") or {}
    selling_points = positioning.get("core_selling_points") or []
    if isinstance(selling_points, list):
        selling_points_text = "、".join(str(item) for item in selling_points[:3])
    else:
        selling_points_text = str(selling_points)
    return (
        f"《{novel.title}》以{positioning.get('genre') or novel.genre or '未分类题材'}为基础，"
        f"主角{protagonist.get('name') or protagonist.get('identity') or '待定'}围绕"
        f"“{main_plot.get('long_term_goal') or novel.premise}”推进，核心卖点：{selling_points_text or '待补充'}。"
    )


def _split_to_list(value: Any) -> list[str]:
    """把 brief 中的自由文本拆成短列表。"""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not value:
        return []
    text = str(value)
    for separator in ["\n", "；", ";", "，", ","]:
        if separator in text:
            return [item.strip() for item in text.split(separator) if item.strip()]
    return [text.strip()] if text.strip() else []


def _build_sample_style_rules(sample_style_references: list[dict[str, Any]]) -> list[str]:
    """把样本报告转成 fallback StoryBible 也能读取的短规则。"""
    rules = []
    for reference in sample_style_references[:3]:
        title = reference.get("sample_title") or "样本"
        vector = reference.get("transferable_style_vector") or {}
        strategy = reference.get("llm_style_strategy") or {}
        if vector:
            rules.append(
                f"参考《{title}》的工程风格：句长均值约 {vector.get('target_sentence_avg', '-')} 字，"
                f"对白占比约 {vector.get('dialogue_ratio', '-')}，"
                f"冲突间隔约 {vector.get('conflict_interval_chars', '-')} 字。"
            )
        if strategy.get("style_summary"):
            rules.append(f"参考《{title}》风格策略：{strategy['style_summary']}")
        for item in (strategy.get("anti_ai_guidelines") or [])[:2]:
            rules.append(f"反 AI 约束：{item}")
    return rules[:10]


def _safe_int(value: Any, default: int) -> int:
    """把字数配置安全转为 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _guess_name(text: str) -> str:
    """从主角描述里粗略取一个可显示名字。"""
    cleaned = str(text or "").strip()
    if not cleaned:
        return "主角"
    for separator in ["，", ",", "。", "；", ";", " "]:
        if separator in cleaned:
            return cleaned.split(separator)[0].strip() or "主角"
    return cleaned[:12]


def _build_default_characters(brief: dict, protagonist: str) -> list[dict[str, str]]:
    """根据起始需求补一个最小人物表。"""
    characters = [
        {
            "name": _guess_name(protagonist),
            "role": "主角",
            "identity": protagonist,
            "function": "承担主线目标和成长线",
        }
    ]
    supporting = brief.get("main_characters")
    for item in _split_to_list(supporting):
        characters.append({"name": item[:12], "role": "主要人物", "identity": item, "function": "推动关系和冲突"})
    return characters
