"""建立样本经验卡索引；保留旧片段提取函数仅用于历史报告兼容。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.sample_analysis import SampleAnalysis
from app.models.sample_passage import SamplePassage
from app.services.embedding_client import EmbeddingClient, build_embedding_config
from app.services.object_storage import iter_text_object_chunks


CHAPTER_HEADING_RE = re.compile(
    r"^\s*(?:第[零一二三四五六七八九十百千万\d]+章|Chapter\s+\d+|CHAPTER\s+\d+)"
)
SENTENCE_END_RE = re.compile(r"(?<=[。！？!?；;])")
METAPHOR_MARKERS = ("像", "仿佛", "如同", "好似", "宛如", "似的")
SENSORY_MARKERS = (
    "光", "影", "颜色", "声音", "脚步", "气味", "冷", "热", "湿", "疼", "触",
)
INNER_MARKERS = ("想", "意识到", "心里", "觉得", "明白", "记起")
ACTION_MARKERS = ("冲", "抓", "推", "跑", "抬", "转身", "站起", "按住", "躲")
PLOT_TRIGGER_MARKERS = ("突然", "这时", "消息", "通知", "发现", "撞见", "被迫", "意外")
PLOT_OBSTACLE_MARKERS = ("但是", "却", "偏偏", "阻止", "拒绝", "失败", "来不及", "不能")
PLOT_CHOICE_MARKERS = ("决定", "选择", "答应", "拒绝", "只好", "宁愿", "必须", "打算")
PLOT_REVERSAL_MARKERS = ("原来", "没想到", "竟然", "反而", "真相", "误会", "其实")
PLOT_CONSEQUENCE_MARKERS = ("因此", "结果", "从此", "失去", "付出", "惹来", "导致", "留下")


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _split_long_paragraph(text: str, target: int = 360, maximum: int = 620) -> list[str]:
    if len(text) <= maximum:
        return [text]
    sentences = [item.strip() for item in SENTENCE_END_RE.split(text) if item.strip()]
    passages: list[str] = []
    buffer = ""
    for sentence in sentences:
        if buffer and len(buffer) + len(sentence) > target:
            passages.append(buffer)
            buffer = sentence
        else:
            buffer += sentence
    if buffer:
        passages.append(buffer)
    return passages


def _passage_type(content: str) -> str:
    dialogue_chars = sum(len(item) for item in re.findall(r"“[^”]+”", content))
    if dialogue_chars / max(len(content), 1) >= 0.3:
        return "dialogue"
    if any(marker in content for marker in METAPHOR_MARKERS):
        return "metaphor"
    if sum(content.count(marker) for marker in SENSORY_MARKERS) >= 3:
        return "description"
    if sum(content.count(marker) for marker in INNER_MARKERS) >= 2:
        return "inner_monologue"
    if sum(content.count(marker) for marker in ACTION_MARKERS) >= 3:
        return "action"
    return "narration"


def _score_passage(content: str, passage_type: str) -> float:
    length_score = 1 - min(abs(len(content) - 260) / 500, 0.8)
    punctuation_variety = len({char for char in content if char in "，。！？；：——……“”"})
    marker_score = min(
        1.0,
        (
            sum(content.count(marker) for marker in METAPHOR_MARKERS)
            + sum(content.count(marker) for marker in SENSORY_MARKERS)
            + sum(content.count(marker) for marker in INNER_MARKERS)
        )
        / 8,
    )
    type_bonus = 0.15 if passage_type != "narration" else 0
    score = 0.45 * length_score + 0.25 * min(punctuation_variety / 6, 1) + 0.3 * marker_score + type_bonus
    return round(min(max(score, 0), 1), 4)


def _technique_summary(content: str, passage_type: str) -> str:
    techniques = []
    if any(marker in content for marker in METAPHOR_MARKERS):
        techniques.append("通过比喻建立可感知意象")
    if sum(content.count(marker) for marker in SENSORY_MARKERS) >= 3:
        techniques.append("组合多种感官细节")
    if "……" in content or "——" in content:
        techniques.append("利用停顿或打断制造潜台词")
    if passage_type == "dialogue":
        techniques.append("用对话轮次体现关系和意图")
    elif passage_type == "action":
        techniques.append("用连续动作推进张力")
    elif passage_type == "inner_monologue":
        techniques.append("用心理反应贴近人物视角")
    return "；".join(techniques[:3]) or "通过具体细节替代抽象说明"


def _plot_window_score(content: str) -> float:
    marker_groups = (
        PLOT_TRIGGER_MARKERS,
        PLOT_OBSTACLE_MARKERS,
        PLOT_CHOICE_MARKERS,
        PLOT_REVERSAL_MARKERS,
        PLOT_CONSEQUENCE_MARKERS,
    )
    covered = sum(
        1 for markers in marker_groups if any(marker in content for marker in markers)
    )
    dialogue_bonus = 0.12 if len(re.findall(r"“[^”]+”", content)) >= 2 else 0
    return round(min(1.0, 0.28 + covered * 0.14 + dialogue_bonus), 4)


def _plot_windows(chunk: str) -> list[str]:
    """保留局部完整因果链，而不是只截取一句反转或一个章尾钩子。"""
    text = re.sub(r"\n{3,}", "\n\n", chunk).strip()
    windows: list[str] = []
    start = 0
    target = 1600
    overlap = 260
    while start < len(text):
        proposed_end = min(len(text), start + target)
        end = proposed_end
        if proposed_end < len(text):
            search_from = start + 900
            boundary = max(
                text.rfind(mark, search_from, proposed_end)
                for mark in ("\n\n", "。", "！", "？")
            )
            if boundary >= search_from:
                end = boundary + (2 if text.startswith("\n\n", boundary) else 1)
        window = text[start:end].strip()
        if len(window) >= 700:
            windows.append(window)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return windows


def _dialogue_exchange_candidates(paragraphs: list[str]) -> list[str]:
    """组合相邻段落，保留称呼、打断、回避和动作反应形成的完整话术回合。"""
    candidates: list[str] = []
    for start in range(len(paragraphs)):
        buffer: list[str] = []
        for end in range(start, min(len(paragraphs), start + 5)):
            buffer.append(paragraphs[end])
            content = "\n".join(buffer)
            if len(content) > 700:
                break
            dialogue_chars = sum(
                len(item) for item in re.findall(r"“([^”]+)”", content)
            )
            turns = len(re.findall(r"“[^”]+”", content))
            if (
                turns >= 2
                and 120 <= len(content) <= 700
                and dialogue_chars / max(len(content), 1) >= 0.2
            ):
                candidates.append(content)
                break
    return candidates


def extract_sample_passage_candidates(
    analysis: SampleAnalysis,
) -> list[dict[str, Any]]:
    """按自然段提取候选，每个 8000 字分片只保留最有表达价值的少量片段。"""
    candidates: list[dict[str, Any]] = []
    chapter_index = 0
    passage_index = 0
    max_passages = max(100, int(settings.sample_rag_max_passages))
    for chunk_index, chunk in enumerate(
        iter_text_object_chunks(analysis.source_object_key),
        start=1,
    ):
        local_candidates = []
        chunk_paragraphs = [
            line.strip()
            for line in chunk.splitlines()
            if line.strip() and not CHAPTER_HEADING_RE.match(line.strip())
        ]
        pending = ""
        for line in chunk.splitlines():
            paragraph = line.strip()
            if not paragraph:
                continue
            if CHAPTER_HEADING_RE.match(paragraph):
                chapter_index += 1
                continue
            if len(paragraph) < 70:
                pending = f"{pending}{paragraph}"
                if len(pending) < 100:
                    continue
                paragraph, pending = pending, ""
            elif pending:
                paragraph, pending = f"{pending}{paragraph}", ""
            for passage in _split_long_paragraph(paragraph):
                passage = passage.strip()
                if not 90 <= len(passage) <= 700:
                    continue
                kind = _passage_type(passage)
                score = _score_passage(passage, kind)
                if score < 0.42:
                    continue
                passage_index += 1
                local_candidates.append(
                    {
                        "chapter_index": chapter_index,
                        "chunk_index": chunk_index,
                        "passage_index": passage_index,
                        "passage_type": kind,
                        "content": passage,
                        "content_hash": _content_hash(passage),
                        "technique_summary": _technique_summary(passage, kind),
                        "metadata_payload": {
                            "length": len(passage),
                            "knowledge_channel": "language",
                            "has_metaphor": any(marker in passage for marker in METAPHOR_MARKERS),
                            "sensory_marker_count": sum(
                                passage.count(marker) for marker in SENSORY_MARKERS
                            ),
                        },
                        "quality_score": score,
                    }
                )
        for exchange in _dialogue_exchange_candidates(chunk_paragraphs):
            passage_index += 1
            local_candidates.append(
                {
                    "chapter_index": chapter_index,
                    "chunk_index": chunk_index,
                    "passage_index": passage_index,
                    "passage_type": "dialogue_exchange",
                    "content": exchange,
                    "content_hash": _content_hash(exchange),
                    "technique_summary": (
                        "观察人物如何用称呼、停顿、回避、打断和动作反应完成真实话术回合"
                    ),
                    "metadata_payload": {
                        "length": len(exchange),
                        "dialogue_turns": len(re.findall(r"“[^”]+”", exchange)),
                        "knowledge_channel": "language",
                    },
                    "quality_score": min(
                        1.0,
                        _score_passage(exchange, "dialogue") + 0.12,
                    ),
                }
            )
        local_candidates.sort(key=lambda item: item["quality_score"], reverse=True)
        candidates.extend(local_candidates[:8])
        plot_candidates = []
        for window in _plot_windows(chunk):
            score = _plot_window_score(window)
            if score < 0.56:
                continue
            passage_index += 1
            plot_candidates.append(
                {
                    "chapter_index": chapter_index,
                    "chunk_index": chunk_index,
                    "passage_index": passage_index,
                    "passage_type": "plot_window",
                    "content": window,
                    "content_hash": _content_hash(window),
                    "technique_summary": (
                        "从真实情节窗口抽取触发、阻力、人物主动选择、转折与代价的组合机制"
                    ),
                    "metadata_payload": {
                        "length": len(window),
                        "knowledge_channel": "plot",
                        "mechanism_coverage": {
                            "trigger": any(marker in window for marker in PLOT_TRIGGER_MARKERS),
                            "obstacle": any(marker in window for marker in PLOT_OBSTACLE_MARKERS),
                            "choice": any(marker in window for marker in PLOT_CHOICE_MARKERS),
                            "reversal": any(marker in window for marker in PLOT_REVERSAL_MARKERS),
                            "consequence": any(marker in window for marker in PLOT_CONSEQUENCE_MARKERS),
                        },
                    },
                    "quality_score": score,
                }
            )
        plot_candidates.sort(
            key=lambda item: item["quality_score"],
            reverse=True,
        )
        candidates.extend(plot_candidates[:2])
        if len(candidates) >= max_passages:
            break
    unique = {
        item["content_hash"]: item
        for item in candidates
    }
    return list(unique.values())[:max_passages]


def index_sample_passages(
    db: Session,
    *,
    analysis: SampleAnalysis,
    preferences: dict | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    config = build_embedding_config(preferences)
    if config is None:
        return {
            "status": "unavailable",
            "reason": "用户未开启 Embedding，或 Endpoint、API Key、模型配置不完整",
            "passage_count": 0,
            "plot_window_count": 0,
            "language_passage_count": 0,
            "embedding_model": "",
        }

    existing_models = set(
        db.scalars(
            select(SamplePassage.embedding_model).where(
                SamplePassage.sample_analysis_id == analysis.id
            )
        ).all()
    )
    if existing_models and existing_models != {config.model}:
        db.execute(
            delete(SamplePassage).where(
                SamplePassage.sample_analysis_id == analysis.id
            )
        )
        db.commit()

    candidates = extract_sample_passage_candidates(analysis)
    existing_hashes = set(
        db.scalars(
            select(SamplePassage.content_hash).where(
                SamplePassage.sample_analysis_id == analysis.id,
                SamplePassage.embedding_model == config.model,
            )
        ).all()
    )
    pending = [item for item in candidates if item["content_hash"] not in existing_hashes]
    plot_window_count = sum(
        1 for item in candidates if item["passage_type"] == "plot_window"
    )
    language_passage_count = len(candidates) - plot_window_count
    batch_size = max(1, min(int(settings.embedding_batch_size), 20))
    client = EmbeddingClient(config)
    completed = len(candidates) - len(pending)
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset:offset + batch_size]
        vectors = client.embed(
            [item["content"] for item in batch],
            text_type="document",
        )
        for item, vector in zip(batch, vectors):
            db.add(
                SamplePassage(
                    owner_id=analysis.owner_id,
                    sample_analysis_id=analysis.id,
                    embedding_model=config.model,
                    embedding=vector,
                    **item,
                )
            )
        db.commit()
        completed += len(batch)
        if progress_callback:
            progress_callback(completed, len(candidates))

    return {
        "status": "completed",
        "reason": "",
        "passage_count": len(candidates),
        "plot_window_count": plot_window_count,
        "language_passage_count": language_passage_count,
        "embedded_count": len(pending),
        "embedding_model": config.model,
        "embedding_dimensions": config.dimensions,
    }


def _experience_index_text(channel: str, card: dict[str, Any]) -> str:
    """把结构化经验卡转成适合 Embedding 检索的高密度文本。"""
    if channel == "plot":
        return "\n".join(
            [
                f"剧情经验：{card.get('title') or ''}",
                f"适用题材：{'、'.join(card.get('applicable_genres') or [])}",
                f"适用场景：{'、'.join(card.get('applicable_scenes') or [])}",
                f"前置状态：{card.get('setup') or ''}",
                f"触发：{card.get('trigger') or ''}",
                f"人物欲望：{card.get('character_desire') or ''}",
                f"冲突升级：{card.get('conflict_and_escalation') or ''}",
                f"主动选择：{card.get('character_choice') or ''}",
                f"转折：{card.get('turn_or_reframe') or ''}",
                f"读者回报：{card.get('payoff') or ''}",
                f"后果：{card.get('consequence') or ''}",
                f"优秀原因：{card.get('why_effective') or ''}",
                f"可迁移机制：{card.get('transferable_pattern') or ''}",
            ]
        ).strip()
    return "\n".join(
        [
            f"表达经验：{card.get('title') or ''}",
            f"类型：{card.get('category') or ''}",
            f"精彩原句：{card.get('original_excerpt') or ''}",
            f"上下文：{card.get('context') or ''}",
            f"人物关系：{card.get('relationship') or ''}",
            f"情绪：{card.get('emotion') or ''}",
            f"语言动作：{card.get('speech_act') or ''}",
            f"回应结构：{card.get('response_pattern') or ''}",
            f"优秀原因：{card.get('why_effective') or ''}",
            f"可迁移技巧：{card.get('transferable_technique') or ''}",
            f"使用边界：{card.get('usage_boundary') or ''}",
            f"适用场景：{'、'.join(card.get('applicable_scenes') or [])}",
        ]
    ).strip()


def build_sample_experience_candidates(
    experience_document: dict[str, Any],
) -> list[dict[str, Any]]:
    """从大模型总结出的经验文档生成剧情、表达两类索引记录。"""
    candidates: list[dict[str, Any]] = []
    passage_index = 0
    def normalized_quality(value: Any) -> float:
        try:
            return min(1.0, max(0.0, float(value or 80) / 100))
        except (TypeError, ValueError):
            return 0.8

    for channel, key, passage_type in (
        ("plot", "plot_experiences", "plot_experience"),
        ("language", "expression_experiences", "expression_experience"),
    ):
        for raw in experience_document.get(key) or []:
            if not isinstance(raw, dict):
                continue
            content = _experience_index_text(channel, raw)
            if len(content) < 40:
                continue
            passage_index += 1
            quality_score = normalized_quality(raw.get("quality_score"))
            candidates.append(
                {
                    "chapter_index": 0,
                    "chunk_index": int(raw.get("part_index") or 0),
                    "passage_index": passage_index,
                    "passage_type": passage_type,
                    "content": content,
                    "content_hash": _content_hash(content),
                    "technique_summary": str(
                        raw.get(
                            "transferable_pattern"
                            if channel == "plot"
                            else "transferable_technique"
                        )
                        or ""
                    )[:1600],
                    "metadata_payload": {
                        "knowledge_channel": channel,
                        "experience_schema": experience_document.get(
                            "schema_version",
                            "sample_experience.v1",
                        ),
                        "experience": raw,
                    },
                    "quality_score": quality_score,
                }
            )
    return candidates


def index_sample_experiences(
    db: Session,
    *,
    analysis: SampleAnalysis,
    experience_document: dict[str, Any],
    preferences: dict | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """只索引大模型提炼的经验卡，彻底替代规则截取的原文窗口。"""
    config = build_embedding_config(preferences)
    if config is None:
        return {
            "status": "unavailable",
            "reason": "用户未开启 Embedding，或 Endpoint、API Key、模型配置不完整",
            "passage_count": 0,
            "plot_experience_count": 0,
            "expression_experience_count": 0,
            "embedding_model": "",
        }

    candidates = build_sample_experience_candidates(experience_document)
    db.execute(
        delete(SamplePassage).where(
            SamplePassage.sample_analysis_id == analysis.id
        )
    )
    db.commit()
    batch_size = max(1, min(int(settings.embedding_batch_size), 20))
    client = EmbeddingClient(config)
    completed = 0
    for offset in range(0, len(candidates), batch_size):
        batch = candidates[offset:offset + batch_size]
        vectors = client.embed(
            [item["content"] for item in batch],
            text_type="document",
        )
        for item, vector in zip(batch, vectors):
            db.add(
                SamplePassage(
                    owner_id=analysis.owner_id,
                    sample_analysis_id=analysis.id,
                    embedding_model=config.model,
                    embedding=vector,
                    **item,
                )
            )
        db.commit()
        completed += len(batch)
        if progress_callback:
            progress_callback(completed, len(candidates))

    plot_count = sum(
        1 for item in candidates if item["passage_type"] == "plot_experience"
    )
    expression_count = len(candidates) - plot_count
    return {
        "status": "completed",
        "reason": "",
        "passage_count": len(candidates),
        "plot_experience_count": plot_count,
        "expression_experience_count": expression_count,
        # 兼容旧前端字段，重建期间不出现空统计。
        "plot_window_count": plot_count,
        "language_passage_count": expression_count,
        "embedded_count": len(candidates),
        "embedding_model": config.model,
        "embedding_dimensions": config.dimensions,
        "index_strategy": "llm_experience_cards",
    }
