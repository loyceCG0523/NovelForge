"""按章节人物关系和语言意图检索已审核热梗。"""

from __future__ import annotations

import json
import re
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.event_chapter_plan import EventChapterPlan
from app.models.meme_entry import MemeEntry
from app.models.novel import Novel
from app.models.user import User
from app.services.embedding_client import EmbeddingClient, build_embedding_config
from app.services.llm_client import (
    LLMClient,
    LLMRequestCancelledError,
    build_review_llm_config,
)
from app.services.meme_library import (
    index_visible_meme_entries,
    meme_embedding_model_key,
    visible_meme_filters,
)


MIN_MEME_VECTOR_SIMILARITY = 0.40
MIN_MEME_SCENE_FIT_SCORE = 92
MAX_MEME_RERANK_CANDIDATES = 12


def _compact_json(value: Any, max_chars: int) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:max_chars]
    except (TypeError, ValueError):
        return str(value or "")[:max_chars]


def build_chapter_meme_query(novel: Novel, context: dict[str, Any]) -> str:
    task_input = (context.get("target") or {}).get("task_input") or {}
    plan = task_input.get("chapter_plan") or {}
    recent_chapters = context.get("recent_chapters") or []
    ending_state = (
        ((recent_chapters[-1].get("chapter_progress") or {}).get("ending_state") or {})
        if recent_chapters
        else {}
    )
    profiles = [
        item
        for item in ((context.get("constraints") or {}).get("characters") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    plan_text = json.dumps(plan, ensure_ascii=False)
    interaction_contexts = [
        item
        for item in (plan.get("interaction_contexts") or [])
        if isinstance(item, dict)
    ][:4]
    active_names = [
        str(item).strip()
        for item in (plan.get("participants") or [])
        if str(item).strip()
    ]
    for interaction in interaction_contexts:
        for name in interaction.get("characters") or []:
            normalized_name = str(name).strip()
            if normalized_name and normalized_name not in active_names:
                active_names.append(normalized_name)
    if not active_names:
        active_names = [
            str(item.get("name") or "").strip()
            for item in profiles
            if str(item.get("name") or "").strip() in plan_text
        ]
    if not active_names:
        active_names = [
            str(name).strip()
            for name in (ending_state.get("characters") or {}).keys()
            if str(name).strip()
        ][:4]
    active_profiles = [
        {
            key: item.get(key)
            for key in ("name", "age", "detailed_setting")
            if item.get(key) not in (None, "", [], {})
        }
        for item in profiles
        if str(item.get("name") or "").strip() in active_names
    ][:4]
    conditions = {
        "genre": novel.genre,
        "story_era": (context.get("constraints") or {}).get("story_era", ""),
        "active_characters": active_profiles,
        "chapter_character_beats": plan.get("character_beats") or [],
        "chapter_core_event": plan.get("core_event") or "",
        "dramatic_turn": plan.get("dramatic_turn") or "",
        "reader_payoff": plan.get("reader_payoff") or "",
        "participants": active_names,
        "interaction_contexts": interaction_contexts,
        "characters_at_previous_ending": ending_state.get("characters") or {},
        "unfinished_interactions": ending_state.get("open_actions") or [],
    }
    return (
        "检索可被主动设计成一段剧情对话的中文网络表达。只匹配三个核心维度："
        "原词与真实含义、当前人物关系与熟悉程度、当前情绪/语言目的/适用场景。"
        "不要按职业名词或剧情名词表面匹配。候选必须能通过一段简短铺垫形成真实语言动作，"
        "使用后还要有对方回应或剧情后果。"
        f"\n当前可设计的互动条件：{_compact_json(conditions, 1800)}"
    )[:2400]


def _rrf(rank: int, constant: int = 40) -> float:
    return 1 / (constant + rank)


def meets_meme_relevance_threshold(
    *,
    vector_similarity: float,
) -> bool:
    """召回阶段只保留宽松的向量底线，混合分仅用于候选排序。"""
    return vector_similarity >= MIN_MEME_VECTOR_SIMILARITY


def build_meme_scene_fit_prompt(
    *,
    query_text: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Ask a small reviewer call to validate meaning and scene independently."""
    payload = {
        "chapter_interaction": query_text,
        "candidates": [
            {
                "entry_id": item.get("entry_id", ""),
                "phrase": item.get("phrase", ""),
                "meaning": item.get("meaning", ""),
                "suitable_scenes": item.get("suitable_scenes", ""),
            }
            for item in candidates[:MAX_MEME_RERANK_CANDIDATES]
        ],
    }
    return [
        {
            "role": "system",
            "content": (
                "你是小说热梗场景规划器，只输出 JSON。逐条独立判断三个硬条件："
                "原词真实含义是否与语言动作一致；说话人与听话人的关系是否适用；"
                "情绪强度和场景是否适用。不能因为梗流行、词面相近或能勉强解释就通过。"
                "允许为合格候选主动设计一段不改变核心剧情的微场景，但必须包含铺垫、原词、"
                "对方回应和剧情/关系后果。若需要改变人物性格、转移当前话题、凭空制造争执、"
                "让不熟的人突然使用熟人口吻，或命中 suitable_scenes 的禁用条件，必须拒绝。"
                "默认结论是拒绝；只有无需改动当前话题、人物口吻和关系距离，原词本身就比普通表达更自然时才通过。"
                "职业名词相似、能制造一句吐槽、角色知道互联网或理论上说得通，都不是通过理由。"
            ),
        },
        {
            "role": "user",
            "content": (
                "返回："
                '{"results":[{"entry_id":"","eligible":true,"fit_score":0,'
                '"meaning_fit":true,"relationship_fit":true,"emotion_fit":true,'
                '"speaker":"","listener":"","relationship":"","emotion":"",'
                '"speech_act":"","required_setup":"","scene_anchor":"","response":"",'
                '"plot_consequence":"","reason":""}]}\n'
                "fit_score 为0—100；三项硬条件全部成立且分数至少92才可 eligible=true。"
                "required_setup 必须描述热梗出现前已经发生的具体互动，不能只是“自然聊天”。\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
        },
    ]


def apply_meme_scene_fit_results(
    candidates: list[dict[str, Any]],
    parsed: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    """Keep only candidates that pass all semantic and scene hard gates."""
    result_by_id = {
        str(item.get("entry_id") or ""): item
        for item in (parsed.get("results") or [])
        if isinstance(item, dict) and str(item.get("entry_id") or "")
    }
    accepted: list[dict[str, Any]] = []
    rejected = 0
    for candidate in candidates:
        result = result_by_id.get(str(candidate.get("entry_id") or ""), {})
        try:
            fit_score = int(result.get("fit_score") or 0)
        except (TypeError, ValueError):
            fit_score = 0
        hard_fit = all(
            result.get(key) is True
            for key in ("meaning_fit", "relationship_fit", "emotion_fit")
        )
        scene_complete = all(
            str(result.get(key) or "").strip()
            for key in (
                "speaker",
                "speech_act",
                "required_setup",
                "scene_anchor",
                "response",
                "plot_consequence",
            )
        )
        if (
            result.get("eligible") is not True
            or fit_score < MIN_MEME_SCENE_FIT_SCORE
            or not hard_fit
            or not scene_complete
        ):
            rejected += 1
            continue
        accepted.append(
            {
                **candidate,
                "scene_fit_score": fit_score,
                "scene_fit": {
                    key: str(result.get(key) or "").strip()
                    for key in (
                        "speaker",
                        "listener",
                        "relationship",
                        "emotion",
                        "speech_act",
                        "required_setup",
                        "scene_anchor",
                        "response",
                        "plot_consequence",
                        "reason",
                    )
                },
            }
        )
    accepted.sort(
        key=lambda item: (
            int(item.get("scene_fit_score") or 0),
            float(item.get("retrieval_score") or 0),
        ),
        reverse=True,
    )
    return accepted, rejected


def collect_adopted_meme_phrases(chapters: list[Chapter]) -> list[str]:
    """读取同一事件其它章节最终实际保留的热梗。"""
    phrases: list[str] = []
    seen: set[str] = set()
    for chapter in chapters:
        usage = (
            ((chapter.context_snapshot or {}).get("meme_reference") or {}).get(
                "usage"
            )
            or {}
        )
        for item in usage.get("adopted") or []:
            phrase = str((item or {}).get("phrase") or "").strip()
            key = phrase.casefold()
            if not phrase or key in seen:
                continue
            seen.add(key)
            phrases.append(phrase)
    return phrases


def get_event_used_meme_phrases(
    db: Session,
    *,
    novel: Novel,
    target_chapter_index: int,
    story_event_id: UUID | str | None,
) -> list[str]:
    """获取当前事件除目标章外已经采用的热梗。"""
    if not story_event_id:
        return []
    try:
        event_id = UUID(str(story_event_id))
    except (TypeError, ValueError):
        return []
    chapter_indexes = list(
        db.scalars(
            select(EventChapterPlan.chapter_index).where(
                EventChapterPlan.story_event_id == event_id,
                EventChapterPlan.chapter_index != target_chapter_index,
            )
        ).all()
    )
    if not chapter_indexes:
        return []
    chapters = list(
        db.scalars(
            select(Chapter).where(
                Chapter.novel_id == novel.id,
                Chapter.chapter_index.in_(chapter_indexes),
            )
        ).all()
    )
    return collect_adopted_meme_phrases(chapters)


def find_reused_event_meme_phrases(
    content: str,
    used_phrases: list[str] | None,
) -> list[str]:
    """检查正文是否重新使用了本事件其它章节已经采用的热梗。"""
    return [
        phrase
        for phrase in (used_phrases or [])
        if meme_phrase_used(phrase, content)
    ]


def find_repeated_meme_phrases(
    content: str,
    phrases: list[str] | None,
) -> list[str]:
    """检查同一章内是否重复出现同一个固定或模板热梗。"""
    return [
        phrase
        for phrase in (phrases or [])
        if meme_phrase_usage_count(phrase, content) > 1
    ]


def build_chapter_meme_pack(
    db: Session,
    *,
    novel: Novel,
    context: dict[str, Any],
    limit: int = 5,
    story_event_id: UUID | str | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    owner = db.get(User, novel.owner_id)
    if owner is None:
        return {"status": "unavailable", "reason": "作品所有者不存在", "references": []}
    config = build_embedding_config(owner.preferences)
    if config is None:
        return {
            "status": "unavailable",
            "reason": "用户未开启 Qwen Embedding，或配置不完整",
            "references": [],
        }

    index_result = index_visible_meme_entries(db, owner=owner)
    query_text = build_chapter_meme_query(novel, context)
    query_vector = EmbeddingClient(config).embed(
        [query_text],
        text_type="query",
        instruct=(
            "Retrieve verified Chinese internet expressions by speaker relationship, "
            "conversational intent, emotion, and scene."
        ),
    )[0]
    target_chapter_index = int(
        ((context.get("target") or {}).get("chapter_index") or 0)
    )
    task_input = (context.get("target") or {}).get("task_input") or {}
    event_used_phrases = get_event_used_meme_phrases(
        db,
        novel=novel,
        target_chapter_index=target_chapter_index,
        story_event_id=story_event_id or task_input.get("story_event_id"),
    )
    filters = [
        *visible_meme_filters(owner.id),
        MemeEntry.embedding_model == meme_embedding_model_key(config.model),
        MemeEntry.embedding.is_not(None),
    ]
    if event_used_phrases:
        filters.append(~MemeEntry.phrase.in_(event_used_phrases))
    distance = MemeEntry.embedding.cosine_distance(query_vector)
    vector_rows = db.execute(
        select(MemeEntry, distance.label("distance"))
        .where(*filters)
        .order_by(distance)
        .limit(30)
    ).all()
    lexical_score = func.similarity(MemeEntry.retrieval_text, query_text[:900])
    lexical_rows = db.execute(
        select(MemeEntry, lexical_score.label("lexical_score"))
        .where(*filters)
        .order_by(desc(lexical_score))
        .limit(20)
    ).all()

    merged: dict[str, dict[str, Any]] = {}
    for rank, (entry, vector_distance) in enumerate(vector_rows, start=1):
        distance_value = (
            float(vector_distance)
            if vector_distance is not None
            else 1.0
        )
        merged[str(entry.id)] = {
            "entry": entry,
            "score": _rrf(rank),
            "vector_distance": distance_value,
            "vector_similarity": 1.0 - distance_value,
            "lexical_score": 0.0,
        }
    for rank, (entry, lexical_value) in enumerate(lexical_rows, start=1):
        item = merged.setdefault(
            str(entry.id),
            {
                "entry": entry,
                "score": 0.0,
                "vector_distance": 1.0,
                "vector_similarity": 0.0,
                "lexical_score": 0.0,
            },
        )
        item["score"] += _rrf(rank)
        item["lexical_score"] = float(lexical_value or 0)
    for item in merged.values():
        entry = item["entry"]
        if entry.source_type == "user":
            item["score"] += 0.008

    qualified = [
        item
        for item in merged.values()
        if meets_meme_relevance_threshold(
            vector_similarity=item["vector_similarity"],
        )
    ]
    retrieval_candidates = []
    for item in sorted(qualified, key=lambda value: value["score"], reverse=True):
        entry = item["entry"]
        retrieval_candidates.append(
            {
                "entry_id": str(entry.id),
                "phrase": entry.phrase,
                "meaning": entry.meaning[:260],
                "suitable_scenes": entry.suitable_scenes[:360],
                "source_type": entry.source_type,
                "retrieval_score": round(item["score"], 6),
                "vector_similarity": round(item["vector_similarity"], 6),
                "lexical_score": round(item["lexical_score"], 6),
            }
        )
        if len(retrieval_candidates) >= MAX_MEME_RERANK_CANDIDATES:
            break
    references: list[dict[str, Any]] = []
    scene_rerank_status = "not_run"
    scene_rerank_error = ""
    scene_rejected_count = 0
    if retrieval_candidates:
        review_config = build_review_llm_config(owner.preferences)
        if review_config is None:
            scene_rerank_status = "unavailable"
            scene_rerank_error = "未配置可用的正文或审校模型，无法执行热梗场景复排"
        else:
            review_config.cancel_check = cancel_check
            try:
                _, scene_fit_payload = LLMClient(review_config).complete_json(
                    build_meme_scene_fit_prompt(
                        query_text=query_text,
                        candidates=retrieval_candidates,
                    ),
                    temperature=0.1,
                )
                scene_fitted, scene_rejected_count = apply_meme_scene_fit_results(
                    retrieval_candidates,
                    scene_fit_payload,
                )
                references = scene_fitted[: max(1, min(limit, 8))]
                scene_rerank_status = "completed"
            except LLMRequestCancelledError:
                raise
            except Exception as exc:
                scene_rerank_status = "failed"
                scene_rerank_error = str(exc)
    return {
        "status": "completed" if references else "empty",
        "reason": (
            ""
            if references
            else scene_rerank_error
            or "当前章节没有同时通过真实含义、人物关系、情绪与场景复检的热梗"
        ),
        "query": query_text,
        "references": references,
        "embedding_model": config.model,
        "indexed_now": int(index_result.get("indexed") or 0),
        "retrieved_count": len(merged),
        "filtered_low_relevance_count": len(merged) - len(qualified),
        "qualified_count": len(qualified),
        "scene_rerank_status": scene_rerank_status,
        "scene_rerank_candidate_count": len(retrieval_candidates),
        "scene_rejected_count": scene_rejected_count,
        "scene_fit_threshold": MIN_MEME_SCENE_FIT_SCORE,
        "relevance_threshold": {
            "min_vector_similarity": MIN_MEME_VECTOR_SIMILARITY,
            "fusion_score_mode": "rank_only",
            "recall_limit": MAX_MEME_RERANK_CANDIDATES,
        },
        "event_used_phrases": event_used_phrases,
        "event_used_count": len(event_used_phrases),
    }


def build_final_meme_usage(
    reference_pack: dict[str, Any] | None,
    chapter_progress: dict[str, Any] | None,
    content: str,
) -> dict[str, Any]:
    """只记录审校后正文中真正保留的候选热梗。"""
    references = [
        item
        for item in ((reference_pack or {}).get("references") or [])
        if isinstance(item, dict) and str(item.get("phrase") or "").strip()
    ]
    plan_by_phrase = {
        str(item.get("phrase") or "").strip(): item
        for item in ((chapter_progress or {}).get("meme_usage_plan") or [])
        if isinstance(item, dict) and str(item.get("phrase") or "").strip()
    }

    adopted = []
    for reference in references:
        phrase = str(reference.get("phrase") or "").strip()
        usage_plan = plan_by_phrase.get(phrase) or {}
        if str(usage_plan.get("decision") or "").strip().lower() != "use":
            continue
        if not meme_phrase_used(phrase, content):
            continue
        adopted.append(
            {
                "entry_id": reference.get("entry_id", ""),
                "phrase": phrase,
                    "speaker": usage_plan.get("speaker", ""),
                    "listener": usage_plan.get("listener", ""),
                    "speech_act": usage_plan.get(
                        "speech_act",
                        "",
                    ),
                    "scene_anchor": usage_plan.get("scene_anchor", ""),
                    "plot_consequence": usage_plan.get(
                        "plot_consequence",
                        "",
                    ),
                "source_type": reference.get("source_type", ""),
            }
        )
    return {
        "candidate_count": len(references),
        "adopted_count": len(adopted),
        "adopted": adopted,
    }


def meme_phrase_used(phrase: str, content: str | None) -> bool:
    """判断固定热梗或带“×”槽位的模板是否已真实进入正文。"""
    return meme_phrase_usage_count(phrase, content) > 0


def meme_phrase_usage_count(phrase: str, content: str | None) -> int:
    """统计固定热梗或带“×”槽位模板在正文中的实际出现次数。"""
    phrase = str(phrase or "").strip()
    if not phrase:
        return 0
    if "×" not in phrase:
        return (content or "").count(phrase)
    pattern = re.escape(phrase).replace("×", r"[^，。！？\s]{1,10}")
    return len(re.findall(pattern, content or ""))
