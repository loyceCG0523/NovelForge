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
from app.services.pacing_plan import build_default_pacing_plan, normalize_pacing_plan
from app.services.tone_pacing_contract import (
    build_tone_pacing_contract,
    resolve_event_chapter_count,
)


STORY_BIBLE_SCHEMA_VERSION = "story_bible.v1"


def _schema_outline(value: Any) -> Any:
    """只保留 JSON 结构和类型，避免把回退文案重复塞进提示词。"""
    if isinstance(value, dict):
        return {key: _schema_outline(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_schema_outline(value[0])] if value else []
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def build_fallback_narrative_contract(novel: Novel, brief: dict[str, Any]) -> dict[str, Any]:
    """Build a genre-adaptive contract without hard-coding one book's plot engines."""
    selling_points = _split_to_list(brief.get("selling_points"))
    plot_directions = _split_to_list(brief.get("plot_direction"))
    volume_directions = _split_to_list(brief.get("volume_direction"))
    primary_promise = (
        brief.get("reader_expectation")
        or "、".join(selling_points[:3])
        or brief.get("plot_direction")
        or novel.premise
        or novel.genre
        or "持续提供与作品主类型一致的冲突升级和阶段回报"
    )
    core_plot_engines = [
        item
        for item in [*plot_directions, *volume_directions, brief.get("core_conflict")]
        if str(item or "").strip()
    ][:8]
    if not core_plot_engines:
        core_plot_engines = [
            f"围绕“{novel.genre or '作品主类型'}”的核心矛盾推进",
            "由人物目标、选择、代价和关系变化共同驱动事件",
        ]
    return {
        "primary_genre": novel.genre,
        "primary_reader_promise": str(primary_promise).strip(),
        "core_plot_engines": core_plot_engines,
        "supporting_element_policy": (
            "职业、技能、身份、设定名词和生活流程默认只作为能力、压力或代价来源；"
            "只有用户明确把它设为主类型或核心卖点时，才可连续主导事件。"
        ),
        "payoff_patterns": selling_points[:6]
        or ["局势反转", "目标取得阶段进展", "关系或身份状态发生明确变化"],
        "event_requirements": [
            "每个事件必须服务主类型承诺，不能被单一辅助元素带偏",
            "必须包含可感知的升级、人物主动选择、转折和阶段回报",
            "候选方向应使用不同剧情驱动力，不能只更换地点、道具或专业流程",
            "连续章节不得重复同一种低变化冲突结构",
        ],
    }


def build_fallback_story_bible(novel: Novel, sample_style_references: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """没有 LLM 配置时，根据起始需求生成保守可用的作品圣经草案。"""
    brief = novel.brief or {}
    structured_characters = _normalize_brief_characters(brief)
    protagonist_record = next((item for item in structured_characters if item.get("is_protagonist")), None)
    protagonist = (protagonist_record or {}).get("name") or brief.get("protagonist") or "主角"
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
            "name": (protagonist_record or {}).get("name") or _guess_name(protagonist),
            "identity": _character_identity(protagonist_record) if protagonist_record else protagonist,
            "gender": (protagonist_record or {}).get("gender", ""),
            "age": (protagonist_record or {}).get("age", ""),
            "occupation": (protagonist_record or {}).get("occupation", ""),
            "personality": (protagonist_record or {}).get("detailed_setting") or brief.get("protagonist_personality") or "目标明确，有行动力，但存在需要成长的短板",
            "goal": (protagonist_record or {}).get("goal") or brief.get("protagonist_goal") or brief.get("plot_direction") or novel.premise,
            "flaw": brief.get("protagonist_flaw") or "容易被既有判断限制",
            "growth_arc": brief.get("protagonist_growth_arc") or "从被事件推着走，逐步成长为能主动选择和承担后果的人",
        },
        "main_characters": structured_characters or _build_default_characters(brief, protagonist),
        "world_rules": {
            "background": brief.get("worldview") or novel.premise,
            "story_era": brief.get("story_era") or "未明确",
            "story_location": brief.get("story_location") or "",
            "era_constraints": [
                "科技、通信、交通、职业、制度、物价、服饰和社会观念必须符合故事年代",
                "涉及真实时代事实时优先使用生成前检索资料，不得出现时代尚未存在的事物",
            ],
            "rules": _split_to_list(brief.get("world_rules")) or ["所有关键设定必须在后续章节保持一致"],
            "constraints": _split_to_list(brief.get("world_constraints")) or [],
        },
        "main_plot": {
            "premise": novel.premise,
            "long_term_goal": brief.get("plot_direction") or novel.premise,
            "core_conflict": brief.get("core_conflict") or "主角目标与外部压力持续冲突",
            "volume_direction": _split_to_list(brief.get("volume_direction")) or [],
            "long_term_foreshadowing": _split_to_list(brief.get("long_term_foreshadowing")) or [],
            "planned_events": _build_system_planned_events(novel, brief, structured_characters),
        },
        "narrative_contract": build_fallback_narrative_contract(novel, brief),
        "pacing_plan": build_default_pacing_plan(novel.target_words),
        "style_rules": {
            "style_reference": brief.get("style_reference") or "具体、克制、重行动和场景细节",
            "tone_pacing_contract": build_tone_pacing_contract(novel.genre, brief),
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
            "chapter_word_min": _safe_int(brief.get("chapter_word_min"), 2500),
            "chapter_word_max": _safe_int(brief.get("chapter_word_max"), 2800),
            "event_chapter_count": resolve_event_chapter_count(brief),
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
    required_schema = _schema_outline(
        build_fallback_story_bible(novel, sample_style_references)
    )
    # 详细节奏默认值由 normalize_pacing_plan 统一补齐，避免把机械字段重复塞给模型。
    required_schema["pacing_plan"] = {
        "global_phases": [],
        "completion_criteria": [],
        "ending_policy": {},
    }
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
            "priority": "supporting",
            "usage": "只把样本短规则用于语言表达；剧情和原文表达由后续 RAG 按当前场景检索。",
            "forbidden": "不得复制样本人物、剧情、设定名词或原文表达。",
        },
        "extra_input": extra_input or {},
        "required_schema": required_schema,
    }
    return [
        {
            "role": "system",
            "content": "\n".join(
                [
                    "你是 NovelForge 作品圣经 Agent，把用户需求整理为可执行的全书结构化设定。",
                    "仅输出 JSON，覆盖 required_schema 全部一级字段；不写散文、Markdown 或解释。",
                ]
            ),
        },
        {
            "role": "user",
            "content": "\n".join(
                [
                    "生成可直接约束自动创作的作品圣经。样本只沉淀少量可执行语言规则，不推导句长、节奏或伏笔指标。",
                    "人物覆盖身份、阶段、性格、背景、目标、缺陷和成长线；普通事件不算人物设定，brief.characters 不得删并或改关键事实。",
                    "story_era 写入 world_rules，明确时代可用/不可用的科技、制度、职业、交通、通信和生活细节。",
                    "事件由 premise、plot_direction、人物目标和关系自动规划；所有规则必须具体可执行。",
                    "必须生成 narrative_contract：区分主类型承诺、核心剧情驱动力与辅助元素。"
                    "职业、技能、身份和设定名词不能因为出现频繁就自动成为主线；是否能主导剧情只由用户题材、核心卖点和长期目标决定。",
                    "narrative_contract 必须给出适合该作品类型的阶段回报方式，不得写死都市、恋爱、职场、玄幻或悬疑中的任一种模板。",
                    "若作品明确是喜剧主导，必须把基调落实为喜剧节拍、反流水账和事件推进约束，不能只写“轻松幽默”。",
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
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
        elif isinstance(fallback_value, dict) and isinstance(normalized[key], dict):
            normalized[key] = {**fallback_value, **normalized[key]}

    # 用户在起始需求中确认的年代、人物和事件是源事实，不能被模型遗漏或改写。
    brief = novel.brief or {}
    structured_characters = _normalize_brief_characters(brief)
    if structured_characters:
        generated_characters = normalized.get("main_characters") if isinstance(normalized.get("main_characters"), list) else []
        structured_keys = {item.get("key") for item in structured_characters if item.get("key")}
        structured_names = {item.get("name") for item in structured_characters if item.get("name")}
        extras = [
            item for item in generated_characters
            if isinstance(item, dict)
            and item.get("key") not in structured_keys
            and item.get("name") not in structured_names
        ]
        normalized["main_characters"] = structured_characters + extras
    world_rules = normalized.setdefault("world_rules", {})
    world_rules["story_era"] = str(brief.get("story_era") or "").strip()
    world_rules["story_location"] = str(brief.get("story_location") or "").strip()
    planned_events = _normalize_brief_events(brief)
    if planned_events:
        main_plot = normalized.setdefault("main_plot", {})
        generated_events = main_plot.get("planned_events") if isinstance(main_plot.get("planned_events"), list) else []
        fixed_keys = {item.get("key") for item in planned_events if item.get("key")}
        fixed_titles = {item.get("title") for item in planned_events if item.get("title")}
        extras = [
            item for item in generated_events
            if isinstance(item, dict)
            and item.get("key") not in fixed_keys
            and item.get("title") not in fixed_titles
        ]
        main_plot["planned_events"] = planned_events + extras
    elif not normalized.setdefault("main_plot", {}).get("planned_events"):
        normalized["main_plot"]["planned_events"] = _build_system_planned_events(
            novel,
            brief,
            structured_characters,
        )
    normalized["pacing_plan"] = normalize_pacing_plan(normalized.get("pacing_plan"), novel.target_words)
    fallback_contract = build_fallback_narrative_contract(novel, brief)
    generated_contract = (
        normalized.get("narrative_contract")
        if isinstance(normalized.get("narrative_contract"), dict)
        else {}
    )
    normalized["narrative_contract"] = {
        **fallback_contract,
        **generated_contract,
        "primary_genre": str(
            generated_contract.get("primary_genre")
            or fallback_contract["primary_genre"]
        ).strip(),
        "primary_reader_promise": str(
            generated_contract.get("primary_reader_promise")
            or fallback_contract["primary_reader_promise"]
        ).strip(),
        "core_plot_engines": _split_to_list(
            generated_contract.get("core_plot_engines")
        )
        or fallback_contract["core_plot_engines"],
        "payoff_patterns": _split_to_list(
            generated_contract.get("payoff_patterns")
        )
        or fallback_contract["payoff_patterns"],
        "event_requirements": _split_to_list(
            generated_contract.get("event_requirements")
        )
        or fallback_contract["event_requirements"],
    }
    generation_policy = normalized.setdefault("generation_policy", {})
    generation_policy["chapter_word_min"] = _safe_int(brief.get("chapter_word_min"), 2500)
    generation_policy["chapter_word_max"] = _safe_int(brief.get("chapter_word_max"), 2800)
    generation_policy["event_chapter_count"] = resolve_event_chapter_count(brief)
    generation_policy["automation_strategy"] = (
        brief.get("automation_strategy")
        or generation_policy.get("automation_strategy")
        or "低风险自动推进，高风险记录并自动尝试修复"
    )
    style_rules = normalized.setdefault("style_rules", {})
    style_rules["tone_pacing_contract"] = build_tone_pacing_contract(novel.genre, brief)
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
    content = normalize_story_bible(story_bible.content or {}, novel)
    return {
        "id": str(story_bible.id),
        "status": story_bible.status,
        "source": story_bible.source,
        "version": story_bible.version,
        "summary": story_bible.summary,
        "content": content,
        "locked_fields": story_bible.locked_fields or {},
    }


def get_sample_style_reference_context(db: Session, novel: Novel, limit: int = 3) -> list[dict[str, Any]]:
    """只暴露短语言原则；剧情和表达经验由双通道 RAG 独立检索。"""
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
        profile = report.get("reference_profile") or {}
        if not profile:
            # 兼容 v3 历史报告，但只迁移少量可执行规则，绝不再透传整份量化数据。
            strategy = report.get("llm_style_strategy") or {}
            language_rules = [
                *(strategy.get("dialogue_guidelines") or []),
                *(strategy.get("generation_guidelines") or []),
            ]
            profile = {
                "available": bool(strategy.get("available")),
                "model": strategy.get("model", ""),
                "overall_evaluation": strategy.get("style_summary", ""),
                "language_principles": [str(value)[:220] for value in language_rules[:4]],
                "avoid_errors": [
                    str(value)[:220]
                    for value in (strategy.get("anti_ai_guidelines") or [])[:3]
                ],
            }
        references.append(
            {
                "id": str(item.id),
                "sample_title": item.sample_title,
                "reference_profile": profile,
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


def _character_identity(character: dict[str, Any] | None) -> str:
    if not character:
        return ""
    parts = [character.get("age"), character.get("occupation"), character.get("detailed_setting")]
    return "；".join(str(item).strip() for item in parts if str(item or "").strip())


def _normalize_brief_characters(brief: dict[str, Any]) -> list[dict[str, Any]]:
    raw = brief.get("characters")
    if not isinstance(raw, list):
        return []
    characters = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            continue
        characters.append(
            {
                "key": str(item.get("key") or ""),
                "name": str(item.get("name") or "").strip(),
                "gender": str(item.get("gender") or "").strip(),
                "age": str(item.get("age") or "").strip(),
                "occupation": str(item.get("occupation") or "").strip(),
                "is_protagonist": bool(item.get("is_protagonist")),
                "goal": str(item.get("goal") or "").strip(),
                "detailed_setting": str(item.get("detailed_setting") or "").strip(),
                "role": "主角" if item.get("is_protagonist") else "主要人物",
                "identity": _character_identity(item),
            }
        )
    return characters


def _normalize_brief_events(brief: dict[str, Any]) -> list[dict[str, Any]]:
    raw = brief.get("planned_events")
    if not isinstance(raw, list):
        return []
    events = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        events.append(
            {
                "key": str(item.get("key") or ""),
                "title": str(item.get("title") or "").strip(),
                "event_type": str(item.get("event_type") or "").strip(),
                "story_stage": str(item.get("story_stage") or "").strip(),
                "planned_time": str(item.get("planned_time") or "").strip(),
                "summary": str(item.get("summary") or "").strip(),
                "participant_character_ids": item.get("participant_character_ids") if isinstance(item.get("participant_character_ids"), list) else [],
                "detailed_setting": str(item.get("detailed_setting") or "").strip(),
            }
        )
    return events


def _build_system_planned_events(
    novel: Novel,
    brief: dict[str, Any],
    characters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """无 LLM 时也由系统根据需求与人物生成开篇事件种子。"""
    legacy_events = _normalize_brief_events(brief)
    if legacy_events:
        return legacy_events
    participant_ids = [item.get("key") for item in characters if item.get("key")][:6]
    protagonist_names = [item.get("name") for item in characters if item.get("is_protagonist") and item.get("name")]
    protagonist_text = "、".join(protagonist_names) or "主角"
    direction = str(brief.get("plot_direction") or novel.premise or "推动主线冲突").strip()
    return [
        {
            "key": "system-opening-event",
            "title": "开篇触发事件",
            "event_type": "opening_incident",
            "story_stage": "opening",
            "planned_time": "故事开篇",
            "summary": f"围绕{protagonist_text}的当前处境建立核心冲突，并开始推进：{direction}",
            "participant_character_ids": participant_ids,
            "detailed_setting": "由系统在事件规划阶段结合时代资料、作品节奏和人物目标进一步细化。",
            "source": "system_generated",
        }
    ]


def _build_sample_style_rules(sample_style_references: list[dict[str, Any]]) -> list[str]:
    """把样本报告转成 fallback StoryBible 也能读取的短规则。"""
    rules = []
    for reference in sample_style_references[:3]:
        profile = reference.get("reference_profile") or {}
        for item in (
            profile.get("language_principles") or profile.get("language_rules") or []
        )[:2]:
            rules.append(str(item)[:220])
        for item in (
            profile.get("avoid_errors") or profile.get("anti_ai_rules") or []
        )[:1]:
            rules.append(str(item)[:220])
    return rules[:6]


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
