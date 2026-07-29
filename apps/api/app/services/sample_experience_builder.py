"""把优秀小说样本并行总结为可检索的剧情与表达经验文档。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any

from app.services.llm_client import LLMClient, LLMConfig
from app.services.object_storage import iter_text_object_chunks


MAX_ANALYSIS_PARTS = 10
TARGET_PART_CHARS = 100_000
PART_OUTPUT_TOKENS = 30_000
MERGE_OUTPUT_TOKENS = 30_000
EXPERIENCE_SCHEMA_VERSION = "sample_experience.v1"


def split_sample_for_parallel_analysis(
    chunks: list[str],
    *,
    max_parts: int = MAX_ANALYSIS_PARTS,
    target_part_chars: int = TARGET_PART_CHARS,
) -> list[str]:
    """按自然文本块聚合，百万字样本最多形成十个并行分析分区。"""
    cleaned = [str(chunk or "").strip() for chunk in chunks if str(chunk or "").strip()]
    if not cleaned:
        return []
    total_chars = sum(len(chunk) for chunk in cleaned)
    part_count = min(
        max(1, int(max_parts)),
        max(1, math.ceil(total_chars / max(1, int(target_part_chars)))),
    )
    if part_count == 1:
        return ["\n\n".join(cleaned)]

    target_chars = math.ceil(total_chars / part_count)
    parts: list[str] = []
    buffer: list[str] = []
    buffer_chars = 0
    remaining_chars = total_chars
    for chunk in cleaned:
        remaining_slots = part_count - len(parts)
        should_flush = (
            bool(buffer)
            and buffer_chars + len(chunk) > target_chars
            and remaining_slots > 1
            and remaining_chars > target_chars
        )
        if should_flush:
            parts.append("\n\n".join(buffer))
            buffer = []
            buffer_chars = 0
        buffer.append(chunk)
        buffer_chars += len(chunk)
        remaining_chars -= len(chunk)
    if buffer:
        parts.append("\n\n".join(buffer))
    return parts[:max_parts]


def _part_prompt(
    *,
    sample_title: str,
    source_genre: str,
    part_index: int,
    part_count: int,
    content: str,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是资深中文网络小说拆解编辑。你的任务不是统计句长或复述故事，"
                "而是从优秀样本原文中提炼可迁移的剧情设计经验和语言表达经验。"
                "只根据原文判断；过滤广告、乱码、目录、重复段落和明显残句。"
                "保留能证明经验的短原句，但不得大段复制。只输出严格 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"作品：{sample_title}\n题材：{source_genre or '未标注'}\n"
                f"当前分区：{part_index}/{part_count}\n\n"
                "请深度分析这一分区，宁可少而精，不要输出空泛写作常识。\n"
                "剧情经验最多 10 条。每条字段：title、source_anchor、setup、trigger、"
                "character_desire、conflict_and_escalation、character_choice、turn_or_reframe、"
                "payoff、consequence、why_effective、transferable_pattern、applicable_genres、"
                "applicable_scenes、quality_score。\n"
                "表达经验最多 18 条，优先选择精彩对话、比喻、动作反应、潜台词和具体描写。"
                "每条字段：title、category、original_excerpt、context、relationship、emotion、"
                "speech_act、response_pattern、why_effective、transferable_technique、"
                "usage_boundary、applicable_scenes、quality_score。original_excerpt 不超过 220 字。\n"
                "quality_score 为 0—100，只保留真正值得复用的内容。\n"
                "顶层 JSON 字段：part_summary、plot_experiences、expression_experiences、"
                "discarded_noise_note。\n\n"
                f"【样本原文分区】\n{content}"
            ),
        },
    ]


def _string(value: Any, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def _string_list(value: Any, limit: int = 12) -> list[str]:
    if isinstance(value, str):
        values = [item for item in value.replace("，", ",").split(",") if item.strip()]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return [_string(item, 200) for item in values if _string(item, 200)][:limit]


def _score(value: Any, default: float = 80.0) -> float:
    try:
        return round(min(100.0, max(0.0, float(value))), 2)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_plot_card(raw: Any, part_index: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    title = _string(raw.get("title"), 160)
    pattern = _string(raw.get("transferable_pattern"), 1200)
    if not title or not pattern:
        return None
    return {
        "part_index": part_index,
        "title": title,
        "source_anchor": _string(raw.get("source_anchor"), 500),
        "setup": _string(raw.get("setup"), 800),
        "trigger": _string(raw.get("trigger"), 800),
        "character_desire": _string(raw.get("character_desire"), 800),
        "conflict_and_escalation": _string(raw.get("conflict_and_escalation"), 1200),
        "character_choice": _string(raw.get("character_choice"), 800),
        "turn_or_reframe": _string(raw.get("turn_or_reframe"), 800),
        "payoff": _string(raw.get("payoff"), 800),
        "consequence": _string(raw.get("consequence"), 800),
        "why_effective": _string(raw.get("why_effective"), 1200),
        "transferable_pattern": pattern,
        "applicable_genres": _string_list(raw.get("applicable_genres")),
        "applicable_scenes": _string_list(raw.get("applicable_scenes")),
        "quality_score": _score(raw.get("quality_score")),
    }


def _normalize_expression_card(raw: Any, part_index: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    excerpt = _string(raw.get("original_excerpt"), 220)
    technique = _string(raw.get("transferable_technique"), 1200)
    if not excerpt or not technique:
        return None
    return {
        "part_index": part_index,
        "title": _string(raw.get("title"), 160) or excerpt[:40],
        "category": _string(raw.get("category"), 80) or "expression",
        "original_excerpt": excerpt,
        "context": _string(raw.get("context"), 700),
        "relationship": _string(raw.get("relationship"), 500),
        "emotion": _string(raw.get("emotion"), 300),
        "speech_act": _string(raw.get("speech_act"), 300),
        "response_pattern": _string(raw.get("response_pattern"), 700),
        "why_effective": _string(raw.get("why_effective"), 1200),
        "transferable_technique": technique,
        "usage_boundary": _string(raw.get("usage_boundary"), 800),
        "applicable_scenes": _string_list(raw.get("applicable_scenes")),
        "quality_score": _score(raw.get("quality_score")),
    }


def _normalize_part_result(parsed: dict[str, Any], part_index: int) -> dict[str, Any]:
    return {
        "part_index": part_index,
        "part_summary": _string(parsed.get("part_summary"), 1200),
        "plot_experiences": [
            card
            for raw in (parsed.get("plot_experiences") or [])[:10]
            if (card := _normalize_plot_card(raw, part_index)) is not None
        ],
        "expression_experiences": [
            card
            for raw in (parsed.get("expression_experiences") or [])[:18]
            if (card := _normalize_expression_card(raw, part_index)) is not None
        ],
        "discarded_noise_note": _string(parsed.get("discarded_noise_note"), 600),
    }


def _card_identity(card: dict[str, Any], fields: tuple[str, ...]) -> str:
    text = "|".join(_string(card.get(field), 500) for field in fields)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _deduplicate_cards(
    cards: list[dict[str, Any]],
    *,
    fields: tuple[str, ...],
    limit: int,
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for card in sorted(
        cards,
        key=lambda item: float(item.get("quality_score") or 0),
        reverse=True,
    ):
        identity = _card_identity(card, fields)
        if identity not in selected:
            selected[identity] = card
        if len(selected) >= limit:
            break
    return list(selected.values())


def _fallback_merge(
    *,
    sample_title: str,
    source_genre: str,
    model: str,
    part_results: list[dict[str, Any]],
) -> dict[str, Any]:
    plot_cards = [
        card for part in part_results for card in part.get("plot_experiences") or []
    ]
    expression_cards = [
        card
        for part in part_results
        for card in part.get("expression_experiences") or []
    ]
    return {
        "schema_version": EXPERIENCE_SCHEMA_VERSION,
        "sample_title": sample_title,
        "source_genre": source_genre,
        "model": model,
        "generated_at": datetime.now(UTC).isoformat(),
        "part_count": len(part_results),
        "overview": "已按原作分区提炼剧情设计与语言表达经验。",
        "plot_principles": [],
        "expression_principles": [],
        "anti_patterns": [],
        "plot_experiences": _deduplicate_cards(
            plot_cards,
            fields=("title", "transferable_pattern"),
            limit=50,
        ),
        "expression_experiences": _deduplicate_cards(
            expression_cards,
            fields=("original_excerpt", "transferable_technique"),
            limit=100,
        ),
        "part_summaries": [
            {
                "part_index": part["part_index"],
                "summary": part.get("part_summary", ""),
            }
            for part in part_results
        ],
    }


def _merge_prompt(
    *,
    sample_title: str,
    source_genre: str,
    part_results: list[dict[str, Any]],
) -> list[dict[str, str]]:
    payload = [
        {
            "part_index": part["part_index"],
            "part_summary": part.get("part_summary", ""),
            "plot_experiences": part.get("plot_experiences") or [],
            "expression_experiences": part.get("expression_experiences") or [],
        }
        for part in part_results
    ]
    return [
        {
            "role": "system",
            "content": (
                "你是中文网络小说总编。请把分区分析合并为一份高密度经验库。"
                "合并同类项但保留真正不同的剧情机制和语言表达，不能把具体经验压缩成空泛常识。"
                "只输出严格 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"作品：{sample_title}\n题材：{source_genre or '未标注'}\n\n"
                "请输出：overview、plot_principles、expression_principles、anti_patterns、"
                "plot_experiences、expression_experiences。"
                "plot_experiences 最多 50 条，expression_experiences 最多 100 条；"
                "保留原有字段和 part_index，优先保留高质量、差异化、可直接用于检索的条目。"
                "原则列表各不超过 12 条，每条必须能执行。\n\n"
                f"{json.dumps(payload, ensure_ascii=False)}"
            ),
        },
    ]


def _normalize_merged_document(
    parsed: dict[str, Any],
    *,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    plot_cards = [
        card
        for raw in (parsed.get("plot_experiences") or [])[:50]
        if (card := _normalize_plot_card(
            raw,
            _int((raw or {}).get("part_index")) if isinstance(raw, dict) else 0,
        ))
        is not None
    ]
    expression_cards = [
        card
        for raw in (parsed.get("expression_experiences") or [])[:100]
        if (card := _normalize_expression_card(
            raw,
            _int((raw or {}).get("part_index")) if isinstance(raw, dict) else 0,
        ))
        is not None
    ]
    return {
        **fallback,
        "overview": _string(parsed.get("overview"), 2400) or fallback["overview"],
        "plot_principles": _string_list(parsed.get("plot_principles"), 12),
        "expression_principles": _string_list(
            parsed.get("expression_principles"),
            12,
        ),
        "anti_patterns": _string_list(parsed.get("anti_patterns"), 12),
        "plot_experiences": plot_cards or fallback["plot_experiences"],
        "expression_experiences": (
            expression_cards or fallback["expression_experiences"]
        ),
    }


def build_experience_markdown(document: dict[str, Any]) -> str:
    """把结构化经验库渲染成用户可下载、可人工阅读的 Markdown。"""
    lines = [
        f"# 《{document.get('sample_title') or '未命名样本'}》创作经验文档",
        "",
        f"- 题材：{document.get('source_genre') or '未标注'}",
        f"- 分析模型：{document.get('model') or '-'}",
        f"- 并行分区：{document.get('part_count') or 0}",
        f"- 生成时间：{document.get('generated_at') or '-'}",
        "",
        "## 总体评价",
        "",
        _string(document.get("overview"), 2400),
        "",
        "## 剧情设计原则",
        "",
    ]
    lines.extend(
        f"{index}. {item}"
        for index, item in enumerate(document.get("plot_principles") or [], start=1)
    )
    lines.extend(["", "## 优秀剧情设计经验", ""])
    for index, card in enumerate(document.get("plot_experiences") or [], start=1):
        lines.extend(
            [
                f"### {index}. {card.get('title') or '未命名剧情经验'}",
                "",
                f"- 原文定位：{card.get('source_anchor') or '-'}",
                f"- 前置状态：{card.get('setup') or '-'}",
                f"- 触发：{card.get('trigger') or '-'}",
                f"- 人物欲望：{card.get('character_desire') or '-'}",
                f"- 冲突升级：{card.get('conflict_and_escalation') or '-'}",
                f"- 主动选择：{card.get('character_choice') or '-'}",
                f"- 转折：{card.get('turn_or_reframe') or '-'}",
                f"- 回报：{card.get('payoff') or '-'}",
                f"- 后果：{card.get('consequence') or '-'}",
                f"- 优秀原因：{card.get('why_effective') or '-'}",
                f"- 可迁移机制：{card.get('transferable_pattern') or '-'}",
                f"- 适用题材：{'、'.join(card.get('applicable_genres') or []) or '-'}",
                f"- 适用场景：{'、'.join(card.get('applicable_scenes') or []) or '-'}",
                "",
            ]
        )
    lines.extend(["## 语言表达原则", ""])
    lines.extend(
        f"{index}. {item}"
        for index, item in enumerate(
            document.get("expression_principles") or [],
            start=1,
        )
    )
    lines.extend(["", "## 精彩语句与表达经验", ""])
    for index, card in enumerate(
        document.get("expression_experiences") or [],
        start=1,
    ):
        lines.extend(
            [
                f"### {index}. {card.get('title') or '未命名表达经验'}",
                "",
                f"> {card.get('original_excerpt') or '-'}",
                "",
                f"- 类型：{card.get('category') or '-'}",
                f"- 上下文：{card.get('context') or '-'}",
                f"- 人物关系：{card.get('relationship') or '-'}",
                f"- 情绪：{card.get('emotion') or '-'}",
                f"- 语言动作：{card.get('speech_act') or '-'}",
                f"- 回应结构：{card.get('response_pattern') or '-'}",
                f"- 优秀原因：{card.get('why_effective') or '-'}",
                f"- 可迁移技巧：{card.get('transferable_technique') or '-'}",
                f"- 使用边界：{card.get('usage_boundary') or '-'}",
                f"- 适用场景：{'、'.join(card.get('applicable_scenes') or []) or '-'}",
                "",
            ]
        )
    lines.extend(["## 应避免的错误归纳", ""])
    lines.extend(
        f"- {item}" for item in document.get("anti_patterns") or []
    )
    return "\n".join(lines).strip() + "\n"


def build_sample_experience_document(
    *,
    sample_title: str,
    source_genre: str,
    source_object_key: str,
    llm_config: LLMConfig,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """最多十路并行分析原作，再合并成结构化经验文档。"""
    chunks = list(iter_text_object_chunks(source_object_key))
    parts = split_sample_for_parallel_analysis(chunks)
    if not parts:
        raise ValueError("样本文本为空或无法解码")

    results: list[dict[str, Any]] = []
    failures: list[str] = []

    def analyze_part(part_index: int, content: str) -> dict[str, Any]:
        _raw, parsed = LLMClient(llm_config).complete_json(
            _part_prompt(
                sample_title=sample_title,
                source_genre=source_genre,
                part_index=part_index,
                part_count=len(parts),
                content=content,
            ),
            max_tokens=PART_OUTPUT_TOKENS,
        )
        return _normalize_part_result(parsed, part_index)

    with ThreadPoolExecutor(
        max_workers=min(MAX_ANALYSIS_PARTS, len(parts)),
        thread_name_prefix="sample-experience",
    ) as executor:
        futures = {
            executor.submit(analyze_part, index, part): index
            for index, part in enumerate(parts, start=1)
        }
        for future in as_completed(futures):
            part_index = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - 允许单分区失败后继续合并。
                failures.append(f"第 {part_index} 份分析失败：{exc}")
            if progress_callback:
                progress_callback(len(results) + len(failures), len(parts))

    if not results:
        raise RuntimeError("全部样本分区分析失败：" + "；".join(failures[:3]))
    results.sort(key=lambda item: item["part_index"])
    fallback = _fallback_merge(
        sample_title=sample_title,
        source_genre=source_genre,
        model=llm_config.model,
        part_results=results,
    )
    try:
        _raw, parsed = LLMClient(llm_config).complete_json(
            _merge_prompt(
                sample_title=sample_title,
                source_genre=source_genre,
                part_results=results,
            ),
            max_tokens=MERGE_OUTPUT_TOKENS,
        )
        document = _normalize_merged_document(parsed, fallback=fallback)
    except Exception as exc:  # noqa: BLE001 - 分区成果仍可形成可用经验文档。
        document = {
            **fallback,
            "merge_warning": f"总编合并调用失败，已使用确定性去重结果：{exc}",
        }
    document["failed_parts"] = failures
    document["markdown"] = build_experience_markdown(document)
    document["source_char_count"] = sum(len(part) for part in parts)
    document["source_chapter_count"] = sum(
        len(
            re.findall(
                r"(?m)^\s*(?:第[零一二三四五六七八九十百千万\d]+章|Chapter\s+\d+|CHAPTER\s+\d+)",
                part,
            )
        )
        for part in parts
    )
    return document
