"""经验文档双通道 RAG：规划前检索剧情经验，写作前检索表达经验。"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.foreshadowing import Foreshadowing
from app.models.novel import Novel
from app.models.sample_passage import SamplePassage
from app.models.user import User
from app.services.embedding_client import EmbeddingClient, build_embedding_config
from app.services.story_bible_builder import get_sample_style_reference_context


def _compact_json(value: Any, max_chars: int = 1000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value or "")
    return text[:max_chars]


def build_expression_query(novel: Novel, context: dict[str, Any]) -> str:
    """语言检索只关心人物怎么相处和说话，不重复搜索既定情节目标。"""
    task_input = (context.get("target") or {}).get("task_input") or {}
    plan = task_input.get("chapter_plan") or {}
    recent_chapters = context.get("recent_chapters") or []
    latest_progress = (
        (recent_chapters[-1].get("chapter_progress") or {})
        if recent_chapters
        else {}
    )
    ending_state = latest_progress.get("ending_state") or {}
    memories = [
        {
            "entity": item.get("entity_name"),
            "type": item.get("memory_type"),
            "status": (item.get("payload") or {}).get("current_status"),
            "facts": (item.get("payload") or {}).get("recent_facts", [])[:2],
        }
        for item in (context.get("memories") or [])[:10]
    ]
    language_need = {
        "character_relationship_or_change": plan.get("character_beats") or [],
        "characters_at_previous_ending": ending_state.get("characters") or {},
        "unfinished_interactions": ending_state.get("open_actions") or [],
        "recent_relationship_facts": memories,
        "era": (context.get("constraints") or {}).get("story_era", ""),
        "location": ending_state.get("location", ""),
    }
    return (
        "检索优秀中文小说经验库中可迁移的生活化话术与表达技巧。重点匹配人物关系、"
        "权力差、熟悉程度、隐瞒或试探意图；优先称呼变化、答非所问、停顿、打断、"
        "口头习惯、共享常识、琐碎动作和没有说破的潜台词。不要按剧情事件关键词匹配。"
        f"\n当前人物互动条件：{_compact_json(language_need, 1500)}"
    )[:2200]


def build_plot_design_query(
    db: Session,
    *,
    novel: Novel,
    planning_input: dict[str, Any],
) -> str:
    """用当前未解决状态寻找不同因果机制，而不是搜索已经预设的下一事件。"""
    latest = list(
        reversed(
            db.scalars(
                select(Chapter)
                .where(Chapter.novel_id == novel.id)
                .order_by(Chapter.chapter_index.desc())
                .limit(5)
            ).all()
        )
    )
    chapter_states = []
    for chapter in latest:
        snapshot = chapter.context_snapshot or {}
        progress = snapshot.get("chapter_progress") or {}
        chapter_states.append(
            {
                "chapter": chapter.chapter_index,
                "summary": progress.get("actual_summary") or chapter.summary,
                "ending_state": progress.get("ending_state") or {},
            }
        )
    foreshadowing = db.scalars(
        select(Foreshadowing)
        .where(
            Foreshadowing.novel_id == novel.id,
            Foreshadowing.status.in_(["planted", "pending", "active"]),
        )
        .order_by(Foreshadowing.updated_at.desc())
        .limit(12)
    ).all()
    current_state = {
        "premise": novel.premise,
        "confirmed_characters": (novel.brief or {}).get("characters") or [],
        "recent_story_state": chapter_states,
        "unresolved_foreshadowing": [
            {"title": item.title, "description": item.description}
            for item in foreshadowing
        ],
        "production_phase": (
            (planning_input.get("production_pacing") or {}).get("pacing_state")
            or {}
        ).get("phase", ""),
    }
    return (
        "检索优秀长篇小说经验库中可以拓展剧情可能性的结构化剧情经验。不要寻找相同题材、"
        "相同事件或相同章尾钩子；要寻找可迁移的因果机制：意外触发来自谁、人物怎样主动"
        "选择、阻力如何换轨、关系怎样改变行动、信息如何延迟或错位、转折付出什么代价、"
        "后果如何开启新方向。优先非直线升级和多角色相互作用。"
        f"\n当前故事尚未解决的状态：{_compact_json(current_state, 2600)}"
    )[:3400]


def _rrf(rank: int, constant: int = 60) -> float:
    return 1 / (constant + rank)


def _selected_sample_ids(db: Session, novel: Novel) -> list[UUID]:
    sample_ids: list[UUID] = []
    for item in get_sample_style_reference_context(db, novel):
        try:
            sample_ids.append(UUID(str(item.get("id"))))
        except (TypeError, ValueError):
            continue
    return sample_ids


def _empty_pack(status: str, reason: str, channel: str) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "channel": channel,
        "query": "",
        "references": [],
        "total_chars": 0,
    }


def _retrieve_reference_pack(
    db: Session,
    *,
    novel: Novel,
    preferences: dict,
    query_text: str,
    channel: str,
    limit: int,
    max_chars: int,
    max_excerpt_chars: int,
) -> dict[str, Any]:
    config = build_embedding_config(preferences)
    if config is None:
        return _empty_pack(
            "unavailable",
            "用户未开启 Embedding，或配置不完整",
            channel,
        )
    sample_ids = _selected_sample_ids(db, novel)
    if not sample_ids:
        return _empty_pack("skipped", "当前作品未选择可用样本", channel)
    if not query_text:
        return _empty_pack("skipped", "当前故事状态不足，无法检索", channel)

    instruct = (
        "Retrieve structured plot experiences with transferable causal mechanisms, character "
        "choices, reversals, and consequences."
        if channel == "plot"
        else
        "Retrieve structured Chinese fiction expression experiences matching "
        "relationship, emotion, scene, and speech-act conditions."
    )
    query_vector = EmbeddingClient(config).embed(
        [query_text],
        text_type="query",
        instruct=instruct,
    )[0]
    filters = [
        SamplePassage.owner_id == novel.owner_id,
        SamplePassage.sample_analysis_id.in_(sample_ids),
        SamplePassage.embedding_model == config.model,
    ]
    if channel == "plot":
        filters.append(SamplePassage.passage_type == "plot_experience")
    else:
        filters.append(SamplePassage.passage_type == "expression_experience")

    distance = SamplePassage.embedding.cosine_distance(query_vector)
    vector_rows = db.execute(
        select(SamplePassage, distance.label("distance"))
        .where(*filters)
        .order_by(distance)
        .limit(50 if channel == "plot" else 40)
    ).all()
    lexical_score = func.similarity(SamplePassage.content, query_text[:700])
    lexical_rows = db.execute(
        select(SamplePassage, lexical_score.label("lexical_score"))
        .where(*filters)
        .order_by(desc(lexical_score))
        .limit(20)
    ).all()

    merged: dict[str, dict[str, Any]] = {}
    for rank, (passage, vector_distance) in enumerate(vector_rows, start=1):
        merged[str(passage.id)] = {
            "passage": passage,
            "score": _rrf(rank),
            "vector_distance": float(vector_distance or 0),
            "lexical_score": 0.0,
        }
    for rank, (passage, lexical_value) in enumerate(lexical_rows, start=1):
        entry = merged.setdefault(
            str(passage.id),
            {
                "passage": passage,
                "score": 0.0,
                "vector_distance": 1.0,
                "lexical_score": 0.0,
            },
        )
        entry["score"] += _rrf(rank)
        entry["lexical_score"] = float(lexical_value or 0)
    for entry in merged.values():
        passage_type = entry["passage"].passage_type or "expression_experience"
        type_bonus = (
            0.012
            if channel == "language"
            and passage_type == "expression_experience"
            else 0
        )
        entry["score"] += (
            float(entry["passage"].quality_score or 0) * 0.015
            + type_bonus
        )

    ranked = sorted(merged.values(), key=lambda item: item["score"], reverse=True)
    selected = []
    total_chars = 0
    type_counts: dict[str, int] = {}
    sample_counts: dict[str, int] = {}
    for entry in ranked:
        passage = entry["passage"]
        passage_kind = passage.passage_type or "expression_experience"
        experience = (passage.metadata_payload or {}).get("experience") or {}
        diversity_kind = (
            str(experience.get("category") or "expression")
            if channel == "language"
            else passage_kind
        )
        sample_id = str(passage.sample_analysis_id)
        if channel == "language" and type_counts.get(diversity_kind, 0) >= 4:
            continue
        if sample_counts.get(sample_id, 0) >= (2 if channel == "plot" else 4):
            continue
        excerpt = (passage.content or "").strip()[:max_excerpt_chars]
        if not excerpt or total_chars + len(excerpt) > max_chars:
            continue
        selected.append(
            {
                "passage_id": str(passage.id),
                "sample_analysis_id": sample_id,
                "chapter_index": passage.chapter_index,
                "passage_type": passage_kind,
                "experience_category": diversity_kind,
                "excerpt": excerpt,
                "technique": passage.technique_summary,
                "mechanism": (
                    experience
                ),
                "experience": experience,
                "quality_score": passage.quality_score,
                "retrieval_score": round(entry["score"], 6),
            }
        )
        total_chars += len(excerpt)
        type_counts[diversity_kind] = type_counts.get(diversity_kind, 0) + 1
        sample_counts[sample_id] = sample_counts.get(sample_id, 0) + 1
        if len(selected) >= max(1, limit):
            break

    return {
        "status": "completed" if selected else "empty",
        "reason": "" if selected else f"样本尚未建立可用{channel}索引",
        "channel": channel,
        "query": query_text,
        "references": selected,
        "total_chars": total_chars,
        "embedding_model": config.model,
        "sample_count": len(sample_ids),
    }


def build_chapter_reference_pack(
    db: Session,
    *,
    novel: Novel,
    context: dict[str, Any],
    limit: int = 10,
    max_chars: int = 2200,
) -> dict[str, Any]:
    """正文生成前只检索结构化的生活化表达经验。"""
    owner = db.get(User, novel.owner_id)
    return _retrieve_reference_pack(
        db,
        novel=novel,
        preferences=owner.preferences if owner else {},
        query_text=build_expression_query(novel, context),
        channel="language",
        limit=limit,
        max_chars=max_chars,
        max_excerpt_chars=520,
    )


def build_plot_design_reference_pack(
    db: Session,
    *,
    novel: Novel,
    planning_input: dict[str, Any],
    preferences: dict | None = None,
    limit: int = 4,
    max_chars: int = 4800,
) -> dict[str, Any]:
    """剧情事件规划前检索结构化剧情经验，用来展开多方向候选。"""
    owner = db.get(User, novel.owner_id) if preferences is None else None
    resolved_preferences = (
        preferences
        if preferences is not None
        else (owner.preferences if owner else {})
    )
    if build_embedding_config(resolved_preferences) is None:
        return _empty_pack(
            "unavailable",
            "用户未开启 Embedding，或配置不完整",
            "plot",
        )
    return _retrieve_reference_pack(
        db,
        novel=novel,
        preferences=resolved_preferences,
        query_text=build_plot_design_query(
            db,
            novel=novel,
            planning_input=planning_input,
        ),
        channel="plot",
        limit=limit,
        max_chars=max_chars,
        max_excerpt_chars=1200,
    )
