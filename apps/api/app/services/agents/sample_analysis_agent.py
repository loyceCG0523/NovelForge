"""SampleAnalysisAgent：把优秀小说样本转成可迁移的风格工程特征。

样本可能达到百万字级别，因此这里采用 map-reduce 思路：
先对每个文本分片计算局部工程指标，再把局部指标聚合成全书级风格向量。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from statistics import mean
from typing import Any

from app.services.llm_client import LLMClient, LLMConfig

AGENT_NAME = "SampleAnalysisAgent"
SAMPLE_ANALYSIS_SCHEMA_VERSION = "sample_analysis.v4"
CHUNK_TARGET_CHARS = 8000

SENSORY_LEXICON = {
    "visual": ["看", "望", "瞧", "亮", "暗", "颜色", "影", "光", "黑", "白", "红"],
    "auditory": ["听", "响", "声", "吵", "静", "喊", "笑声", "脚步", "铃", "风声"],
    "olfactory": ["闻", "味", "香", "臭", "气息", "腥", "烟味", "油墨"],
    "tactile": ["冷", "热", "疼", "痛", "软", "硬", "湿", "干", "碰", "握", "触"],
    "taste": ["甜", "苦", "酸", "辣", "咸", "涩", "味道"],
}

EMOTION_LEXICON = {
    "positive": ["笑", "轻松", "高兴", "开心", "暖", "安心", "期待", "兴奋"],
    "negative": ["难过", "失落", "害怕", "慌", "痛", "冷", "沉默", "委屈", "疲惫"],
    "tension": ["紧张", "压", "盯", "冲", "争", "危险", "僵", "急", "逼"],
    "romance": ["脸红", "心跳", "靠近", "温柔", "目光", "指尖", "同桌", "喜欢"],
}

FORESHADOWING_MARKERS = ["似乎", "好像", "奇怪", "没注意", "后来", "秘密", "线索", "眼神", "预感", "记得"]
PAYOFF_MARKERS = ["原来", "终于", "真相", "揭开", "回想起", "兑现", "明白", "答案"]
MISDIRECTION_MARKERS = ["以为", "却", "没想到", "反而", "然而", "偏偏"]
CONFLICT_MARKERS = ["冲突", "争", "吵", "质问", "拒绝", "逼", "输", "赢", "危险", "威胁", "误会"]
INFO_RELEASE_MARKERS = ["发现", "知道", "明白", "原来", "消息", "通知", "名单", "秘密", "线索"]
EXPLANATORY_DIALOGUE_MARKERS = ["因为", "所以", "其实", "也就是说", "换句话", "你知道", "原因"]


def analyze_sample_chunks(
    sample_title: str,
    source_genre: str,
    chunks: Iterable[str],
    progress_callback: Callable[[int, dict[str, Any]], None] | None = None,
    llm_config: LLMConfig | None = None,
) -> dict[str, Any]:
    """分析文本分片并聚合为全书报告。"""
    chunk_reports = []
    for index, chunk in enumerate(chunks, start=1):
        normalized = _normalize_text(chunk)
        if len(normalized) < 40:
            continue
        chunk_report = _analyze_segment(
            sample_title=sample_title,
            source_genre=source_genre,
            text=normalized,
            chunk_index=index,
        )
        chunk_reports.append(chunk_report)
        if progress_callback:
            progress_callback(len(chunk_reports), chunk_report)

    if not chunk_reports:
        raise ValueError("样本文本为空或无法解码")

    report = _aggregate_chunk_reports(sample_title, source_genre, chunk_reports)
    strategy = _build_llm_style_strategy(report, llm_config)
    return _compact_sample_report(report, strategy)


def summarize_sample_report(report: dict[str, Any]) -> str:
    """生成一行产品界面可读摘要。"""
    profile = report.get("reference_profile") or {}
    if profile.get("available") and profile.get("summary"):
        return str(profile["summary"])[:180]
    return "样本已完成切片，生成时将通过双通道 RAG 按需检索情节窗口和语言片段。"


def _compact_sample_report(
    report: dict[str, Any],
    strategy: dict[str, Any],
) -> dict[str, Any]:
    """最终报告只保留生成链真正会消费的短策略；详细统计不再落库或进入上下文。"""
    sample = report.get("sample") or {}
    return {
        "schema_version": SAMPLE_ANALYSIS_SCHEMA_VERSION,
        "agent": AGENT_NAME,
        "analysis_mode": "rag_first_compact",
        "sample": {
            "title": sample.get("title", ""),
            "genre": sample.get("genre", ""),
            "word_count": sample.get("word_count", 0),
            "chapter_count": sample.get("chapter_count", 0),
            "chunk_count": sample.get("chunk_count", 0),
        },
        "reference_profile": strategy,
    }


def _analyze_segment(sample_title: str, source_genre: str, text: str, chunk_index: int) -> dict[str, Any]:
    """对一个分片执行局部指标抽取。"""
    chapters = _split_chapters(text)
    sentences = _split_sentences(text)
    paragraphs = _split_paragraphs(text)
    dialogues = re.findall(r"“([^”]{1,500})”", text)

    report = {
        "schema_version": SAMPLE_ANALYSIS_SCHEMA_VERSION,
        "agent": AGENT_NAME,
        "chunk_index": chunk_index,
        "sample": {
            "title": sample_title,
            "genre": source_genre,
            "word_count": len(text),
            "chapter_count": len(chapters),
            "sentence_count": len(sentences),
            "paragraph_count": len(paragraphs),
        },
        "style_fingerprint": _build_style_fingerprint(text, sentences, paragraphs, dialogues),
        "emotion_curve": _build_emotion_curve(chapters),
        "foreshadowing_pattern": _build_foreshadowing_pattern(text, chapters),
        "dialogue_style": _build_dialogue_style(text, dialogues),
        "pacing_model": _build_pacing_model(text, chapters),
    }
    report["transferable_style_vector"] = _build_transferable_style_vector(report)
    return report


def _aggregate_chunk_reports(sample_title: str, source_genre: str, reports: list[dict[str, Any]]) -> dict[str, Any]:
    """把分片报告聚合为全书级报告。"""
    total_chars = sum(item["sample"]["word_count"] for item in reports)
    total_sentences = sum(item["sample"]["sentence_count"] for item in reports)
    total_paragraphs = sum(item["sample"]["paragraph_count"] for item in reports)
    total_chapters = sum(item["sample"]["chapter_count"] for item in reports)

    report = {
        "schema_version": SAMPLE_ANALYSIS_SCHEMA_VERSION,
        "agent": AGENT_NAME,
        "analysis_mode": "chunked_map_reduce",
        "sample": {
            "title": sample_title,
            "genre": source_genre,
            "word_count": total_chars,
            "chapter_count": max(total_chapters, len(reports)),
            "sentence_count": total_sentences,
            "paragraph_count": total_paragraphs,
            "chunk_count": len(reports),
        },
        "style_fingerprint": _merge_style_fingerprint(reports),
        "emotion_curve": _merge_emotion_curve(reports),
        "foreshadowing_pattern": _merge_foreshadowing_pattern(reports),
        "dialogue_style": _merge_dialogue_style(reports),
        "pacing_model": _merge_pacing_model(reports),
        "chunk_summaries": _build_chunk_summaries(reports),
    }
    report["transferable_style_vector"] = _build_transferable_style_vector(report)
    return report


def _build_llm_style_strategy(report: dict[str, Any], llm_config: LLMConfig | None) -> dict[str, Any]:
    """把少量诊断指标压成短规则；真正的情节和语言参考由 RAG 提供。"""
    if llm_config is None:
        return {
            "available": False,
            "reason": "未配置 LLM API Key，仅保存量化分析结果。",
            "model": "",
            "summary": "",
            "language_rules": [],
            "anti_ai_rules": [],
        }

    payload = _build_llm_strategy_payload(report)
    messages = [
        {
            "role": "system",
            "content": (
                "你是长篇网文风格分析工程师。你只能根据输入的结构化量化指标生成可迁移策略，"
                "不要复述、续写、模仿或复制样本原文。输出必须是严格 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请把以下少量诊断指标压缩成可直接执行的语言参考规则。"
                "重点是人物话术的生活感、潜台词、伴随动作和具体叙述；不要输出剧情、节奏、伏笔、句长目标，"
                "不要包含任何样本文本内容。\n\n"
                f"{json.dumps(payload, ensure_ascii=False)}\n\n"
                "JSON 字段：summary、language_rules、anti_ai_rules。"
                "language_rules 最多 4 条，anti_ai_rules 最多 3 条；每条必须具体、短小、可执行。"
            ),
        },
    ]
    try:
        _raw, parsed = LLMClient(llm_config).complete_json(messages)
    except Exception as exc:  # noqa: BLE001 - LLM 不可用不应导致样本分析整体失败。
        return {
            "available": False,
            "reason": f"LLM 风格策略生成失败：{exc}",
            "model": llm_config.model,
            "summary": "",
            "language_rules": [],
            "anti_ai_rules": [],
        }

    return {
        "available": True,
        "reason": "",
        "model": llm_config.model,
        "summary": str(parsed.get("summary") or "").strip()[:180],
        "language_rules": _normalize_string_list(parsed.get("language_rules"), limit=4),
        "anti_ai_rules": _normalize_string_list(parsed.get("anti_ai_rules"), limit=3),
    }


def _build_llm_strategy_payload(report: dict[str, Any]) -> dict[str, Any]:
    """只发送解释语言表现所需的少量诊断值，避免整份统计报告占上下文。"""
    style = report.get("style_fingerprint") or {}
    dialogue = report.get("dialogue_style") or {}
    return {
        "sample": {
            "genre": (report.get("sample") or {}).get("genre", ""),
        },
        "language_diagnostics": {
            "dialogue_ratio": dialogue.get("dialogue_ratio", 0),
            "utterance_length_avg": (dialogue.get("utterance_length") or {}).get("avg", 0),
            "subtext_ratio": dialogue.get("subtext_ratio", 0),
            "interruption_frequency_per_100_dialogues": dialogue.get(
                "interruption_frequency_per_100_dialogues", 0
            ),
            "explanatory_dialogue_ratio": dialogue.get("explanatory_dialogue_ratio", 0),
            "metaphor_density_per_1k_chars": style.get("metaphor_density_per_1k_chars", 0),
            "dominant_sensory_dimension": (
                style.get("sensory_coverage") or {}
            ).get("dominant_dimension", ""),
        },
    }


def _normalize_string_list(value: Any, limit: int = 8) -> list[str]:
    """把模型输出整理成短字符串列表，避免前端出现异常结构。"""
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        items = []
    cleaned = []
    for item in items:
        text = str(item).strip()
        if text:
            cleaned.append(text[:200])
    return cleaned[:limit]


def _normalize_text(content: str) -> str:
    """统一换行和空白，保留段落边界。"""
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _split_text_for_analysis(text: str, chunk_chars: int = CHUNK_TARGET_CHARS) -> list[str]:
    """小文本入口使用的切片器；文件上传场景由对象存储流式切片。"""
    chunks = []
    buffer = text
    while len(buffer) > chunk_chars:
        cut = _find_chunk_boundary(buffer, chunk_chars)
        chunks.append(buffer[:cut])
        buffer = buffer[cut:]
    if buffer.strip():
        chunks.append(buffer)
    return chunks


def _find_chunk_boundary(buffer: str, chunk_chars: int) -> int:
    """优先在自然边界切片。"""
    window = buffer[:chunk_chars]
    boundary = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind("。"), window.rfind("！"), window.rfind("？"))
    return boundary + 1 if boundary >= int(chunk_chars * 0.55) else chunk_chars


def _split_chapters(text: str) -> list[str]:
    """按章节标题切分；没有章节标题时按长度自动切块。"""
    parts = re.split(r"\n\s*(?:第[零一二三四五六七八九十百千万\d]+章|Chapter\s+\d+|CHAPTER\s+\d+)[^\n]*\n", text)
    chapters = [part.strip() for part in parts if len(part.strip()) > 80]
    if chapters:
        return chapters[:80]
    return [text[index:index + 3500] for index in range(0, len(text), 3500) if text[index:index + 3500].strip()]


def _split_sentences(text: str) -> list[str]:
    """按中文和英文句末符号切句。"""
    return [item.strip() for item in re.split(r"[。！？!?；;]+", text) if item.strip()]


def _split_paragraphs(text: str) -> list[str]:
    """按空行或换行切段。"""
    return [item.strip() for item in re.split(r"\n+", text) if item.strip()]


def _count_markers(text: str, markers: list[str]) -> int:
    """统计一组标记词出现次数。"""
    return sum(text.count(marker) for marker in markers)


def _ratio(part: float, total: float) -> float:
    """安全计算比例，保留 4 位小数。"""
    return round(part / total, 4) if total else 0


def _density(count: int | float, char_count: int) -> float:
    """计算每千字密度。"""
    return round(count / max(char_count, 1) * 1000, 2)


def _count_from_density(density: float, char_count: int) -> float:
    """由每千字密度反推近似次数。"""
    return float(density or 0) * max(char_count, 1) / 1000


def _weighted_avg(items: list[tuple[float, float]]) -> float:
    """加权平均。"""
    total_weight = sum(weight for _, weight in items)
    if not total_weight:
        return 0
    return round(sum(value * weight for value, weight in items) / total_weight, 2)


def _bucket_sentence_lengths(lengths: list[int]) -> dict[str, float]:
    """句长分布。"""
    total = len(lengths)
    return {
        "short_1_12": _ratio(len([item for item in lengths if item <= 12]), total),
        "medium_13_28": _ratio(len([item for item in lengths if 13 <= item <= 28]), total),
        "long_29_50": _ratio(len([item for item in lengths if 29 <= item <= 50]), total),
        "extra_long_51_plus": _ratio(len([item for item in lengths if item >= 51]), total),
    }


def _percentile(values: list[int], percent: float) -> int:
    """计算简单百分位数。"""
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percent)))
    return ordered[index]


def _build_style_fingerprint(text: str, sentences: list[str], paragraphs: list[str], dialogues: list[str]) -> dict[str, Any]:
    """文风指纹：句长、比喻、感官维度、叙事距离、对白占比和段落节奏。"""
    lengths = [len(sentence) for sentence in sentences]
    paragraph_lengths = [len(item) for item in paragraphs]
    dialogue_chars = sum(len(item) for item in dialogues)
    sensory_counts = {name: _count_markers(text, markers) for name, markers in SENSORY_LEXICON.items()}
    first_person = _count_markers(text, ["我", "我们"])
    third_person = _count_markers(text, ["他", "她", "他们", "她们"])
    inner_state = _count_markers(text, ["想", "觉得", "意识到", "明白", "心里"])
    metaphor_count = _count_markers(text, ["像", "仿佛", "如同", "好似", "似的"])
    return {
        "sentence_length": {
            "avg": round(mean(lengths), 2) if lengths else 0,
            "p50": _percentile(lengths, 0.5),
            "p90": _percentile(lengths, 0.9),
            "distribution": _bucket_sentence_lengths(lengths),
        },
        "metaphor_density_per_1k_chars": _density(metaphor_count, len(text)),
        "sensory_coverage": {
            "counts": sensory_counts,
            "covered_dimension_count": len([value for value in sensory_counts.values() if value > 0]),
            "dominant_dimension": max(sensory_counts, key=sensory_counts.get) if sensory_counts else "",
        },
        "narrative_distance": {
            "first_person_ratio": _ratio(first_person, first_person + third_person),
            "third_person_ratio": _ratio(third_person, first_person + third_person),
            "inner_state_density_per_1k_chars": _density(inner_state, len(text)),
            "estimated_distance": "close" if _density(inner_state, len(text)) >= 4 else "medium",
        },
        "dialogue_ratio": _ratio(dialogue_chars, len(text)),
        "paragraph_rhythm": {
            "avg_paragraph_chars": round(mean(paragraph_lengths), 2) if paragraph_lengths else 0,
            "p50_paragraph_chars": _percentile(paragraph_lengths, 0.5),
            "p90_paragraph_chars": _percentile(paragraph_lengths, 0.9),
            "preferred_paragraph_ratio": _ratio(len([item for item in paragraph_lengths if item <= 50]), len(paragraph_lengths)),
            "over_preferred_paragraph_ratio": _ratio(len([item for item in paragraph_lengths if item > 50]), len(paragraph_lengths)),
            "over_hard_limit_paragraph_ratio": _ratio(len([item for item in paragraph_lengths if item > 70]), len(paragraph_lengths)),
        },
    }


def _chapter_emotion(chapter: str) -> dict[str, Any]:
    """计算单章情绪标签和强度。"""
    scores = {name: _count_markers(chapter, markers) for name, markers in EMOTION_LEXICON.items()}
    dominant = max(scores, key=scores.get) if scores else "neutral"
    intensity = _density(sum(scores.values()), len(chapter))
    valence = _density(scores.get("positive", 0) + scores.get("romance", 0), len(chapter)) - _density(scores.get("negative", 0), len(chapter))
    return {
        "label": dominant if scores.get(dominant, 0) else "neutral",
        "intensity": round(intensity, 2),
        "valence": round(valence, 2),
        "scores": scores,
    }


def _build_emotion_curve(chapters: list[str]) -> dict[str, Any]:
    """情绪曲线：每章标签、起伏、高潮和低谷位置。"""
    points = []
    for index, chapter in enumerate(chapters, start=1):
        emotion = _chapter_emotion(chapter)
        points.append({"chapter": index, **emotion})
    intensities = [point["intensity"] for point in points]
    valences = [point["valence"] for point in points]
    peak = max(points, key=lambda item: item["intensity"], default={})
    valley = min(points, key=lambda item: item["valence"], default={})
    volatility = mean([abs(intensities[index] - intensities[index - 1]) for index in range(1, len(intensities))]) if len(intensities) > 1 else 0
    return {
        "chapter_points": points[:80],
        "volatility_avg_delta": round(volatility, 2),
        "peak_chapter": peak.get("chapter"),
        "peak_intensity": peak.get("intensity", 0),
        "valley_chapter": valley.get("chapter"),
        "valley_valence": valley.get("valence", 0),
    }


def _build_foreshadowing_pattern(text: str, chapters: list[str]) -> dict[str, Any]:
    """伏笔模式：埋设密度、兑现周期、埋设方式和误导策略。"""
    planting_counts = [_count_markers(chapter, FORESHADOWING_MARKERS) for chapter in chapters]
    payoff_counts = [_count_markers(chapter, PAYOFF_MARKERS) for chapter in chapters]
    planting_positions = [index for index, count in enumerate(planting_counts, start=1) for _ in range(count)]
    payoff_positions = [index for index, count in enumerate(payoff_counts, start=1) for _ in range(count)]
    cycles = []
    for planting in planting_positions:
        payoff = next((item for item in payoff_positions if item >= planting), None)
        if payoff is not None:
            cycles.append(payoff - planting)
    return {
        "planting_density_per_1k_chars": _density(sum(planting_counts), len(text)),
        "payoff_density_per_1k_chars": _density(sum(payoff_counts), len(text)),
        "estimated_payoff_cycle_chapters_avg": round(mean(cycles), 2) if cycles else 0,
        "planting_methods": {
            "anomaly_marker_count": _count_markers(text, ["奇怪", "不对劲", "异样"]),
            "object_detail_count": _count_markers(text, ["纸条", "钥匙", "手机", "笔记", "照片", "名单"]),
            "gaze_or_silence_count": _count_markers(text, ["眼神", "沉默", "停顿", "没说话"]),
        },
        "misdirection_strategy": {
            "marker_count": _count_markers(text, MISDIRECTION_MARKERS),
            "density_per_1k_chars": _density(_count_markers(text, MISDIRECTION_MARKERS), len(text)),
        },
    }


def _build_dialogue_style(text: str, dialogues: list[str]) -> dict[str, Any]:
    """对白风格：话语长度、潜台词、打断和解释性对白。"""
    lengths = [len(item) for item in dialogues]
    dialogue_text = "\n".join(dialogues)
    subtext_count = _count_markers(dialogue_text, ["……", "...", "沉默", "算了", "没事", "笑"])
    interruption_count = _count_markers(dialogue_text, ["——", "…", "等等", "别说"])
    explanatory_count = _count_markers(dialogue_text, EXPLANATORY_DIALOGUE_MARKERS)
    return {
        "dialogue_count": len(dialogues),
        "dialogue_ratio": _ratio(sum(lengths), len(text)),
        "utterance_length": {
            "avg": round(mean(lengths), 2) if lengths else 0,
            "p50": _percentile(lengths, 0.5),
            "p90": _percentile(lengths, 0.9),
            "short_utterance_ratio": _ratio(len([item for item in lengths if item <= 12]), len(lengths)),
        },
        "subtext_ratio": _ratio(subtext_count, len(dialogues)),
        "interruption_frequency_per_100_dialogues": round(interruption_count / max(len(dialogues), 1) * 100, 2),
        "explanatory_dialogue_ratio": _ratio(explanatory_count, len(dialogues)),
    }


def _classify_hook(chapter: str) -> str:
    """估计章尾钩子类型。"""
    ending = chapter[-160:]
    if "？" in ending or "?" in ending:
        return "question"
    if _count_markers(ending, ["冲", "推开", "跑", "抓", "站起"]):
        return "action"
    if _count_markers(ending, ["原来", "发现", "名单", "秘密", "真相"]):
        return "reveal"
    if _count_markers(ending, ["心跳", "沉默", "眼神", "脸红", "笑"]):
        return "emotion"
    return "soft_pause"


def _build_pacing_model(text: str, chapters: list[str]) -> dict[str, Any]:
    """节奏模型：冲突间隔、信息释放速度和章尾钩子类型。"""
    conflict_total = _count_markers(text, CONFLICT_MARKERS)
    info_total = _count_markers(text, INFO_RELEASE_MARKERS)
    hook_types = [_classify_hook(chapter) for chapter in chapters]
    hook_strengths = [1 if hook != "soft_pause" else 0.35 for hook in hook_types]
    hook_distribution = {hook: hook_types.count(hook) for hook in sorted(set(hook_types))}
    return {
        "conflict_interval_chars_est": round(len(text) / max(conflict_total, 1), 2),
        "conflict_density_per_1k_chars": _density(conflict_total, len(text)),
        "information_release_density_per_1k_chars": _density(info_total, len(text)),
        "ending_hook_type_distribution": hook_distribution,
        "ending_hook_strength_avg": round(mean(hook_strengths), 2) if hook_strengths else 0,
    }


def _merge_style_fingerprint(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合文风指纹。"""
    total_chars = sum(item["sample"]["word_count"] for item in reports)
    total_sentences = sum(item["sample"]["sentence_count"] for item in reports)
    total_paragraphs = sum(item["sample"]["paragraph_count"] for item in reports)
    sensory_counts = {name: 0 for name in SENSORY_LEXICON}
    for item in reports:
        counts = item["style_fingerprint"]["sensory_coverage"]["counts"]
        for name in sensory_counts:
            sensory_counts[name] += counts.get(name, 0)
    sentence_distribution = {}
    for bucket in ["short_1_12", "medium_13_28", "long_29_50", "extra_long_51_plus"]:
        sentence_distribution[bucket] = round(
            sum(
                item["style_fingerprint"]["sentence_length"]["distribution"].get(bucket, 0) * item["sample"]["sentence_count"]
                for item in reports
            ) / max(total_sentences, 1),
            4,
        )
    return {
        "sentence_length": {
            "avg": _weighted_avg([(item["style_fingerprint"]["sentence_length"]["avg"], item["sample"]["sentence_count"]) for item in reports]),
            "p50": round(_weighted_avg([(item["style_fingerprint"]["sentence_length"]["p50"], item["sample"]["sentence_count"]) for item in reports])),
            "p90": round(_weighted_avg([(item["style_fingerprint"]["sentence_length"]["p90"], item["sample"]["sentence_count"]) for item in reports])),
            "distribution": sentence_distribution,
        },
        "metaphor_density_per_1k_chars": _density(
            sum(_count_from_density(item["style_fingerprint"]["metaphor_density_per_1k_chars"], item["sample"]["word_count"]) for item in reports),
            total_chars,
        ),
        "sensory_coverage": {
            "counts": sensory_counts,
            "covered_dimension_count": len([value for value in sensory_counts.values() if value > 0]),
            "dominant_dimension": max(sensory_counts, key=sensory_counts.get),
        },
        "narrative_distance": {
            "first_person_ratio": round(_weighted_avg([(item["style_fingerprint"]["narrative_distance"]["first_person_ratio"], item["sample"]["word_count"]) for item in reports]), 4),
            "third_person_ratio": round(_weighted_avg([(item["style_fingerprint"]["narrative_distance"]["third_person_ratio"], item["sample"]["word_count"]) for item in reports]), 4),
            "inner_state_density_per_1k_chars": _density(
                sum(_count_from_density(item["style_fingerprint"]["narrative_distance"]["inner_state_density_per_1k_chars"], item["sample"]["word_count"]) for item in reports),
                total_chars,
            ),
            "estimated_distance": "close",
        },
        "dialogue_ratio": round(_weighted_avg([(item["style_fingerprint"]["dialogue_ratio"], item["sample"]["word_count"]) for item in reports]), 4),
        "paragraph_rhythm": {
            "avg_paragraph_chars": _weighted_avg([(item["style_fingerprint"]["paragraph_rhythm"]["avg_paragraph_chars"], item["sample"]["paragraph_count"]) for item in reports]),
            "p50_paragraph_chars": round(_weighted_avg([(item["style_fingerprint"]["paragraph_rhythm"].get("p50_paragraph_chars", 0), item["sample"]["paragraph_count"]) for item in reports])),
            "p90_paragraph_chars": round(_weighted_avg([(item["style_fingerprint"]["paragraph_rhythm"]["p90_paragraph_chars"], item["sample"]["paragraph_count"]) for item in reports])),
            "preferred_paragraph_ratio": round(_weighted_avg([(item["style_fingerprint"]["paragraph_rhythm"].get("preferred_paragraph_ratio", 0), item["sample"]["paragraph_count"]) for item in reports]), 4),
            "over_preferred_paragraph_ratio": round(_weighted_avg([(item["style_fingerprint"]["paragraph_rhythm"].get("over_preferred_paragraph_ratio", 0), item["sample"]["paragraph_count"]) for item in reports]), 4),
            "over_hard_limit_paragraph_ratio": round(_weighted_avg([(item["style_fingerprint"]["paragraph_rhythm"].get("over_hard_limit_paragraph_ratio", 0), item["sample"]["paragraph_count"]) for item in reports]), 4),
        },
    }


def _merge_emotion_curve(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合情绪曲线。"""
    points = []
    for item in reports:
        for point in item["emotion_curve"]["chapter_points"]:
            points.append({"chapter": len(points) + 1, **{key: value for key, value in point.items() if key != "chapter"}})
    intensities = [point["intensity"] for point in points]
    valences = [point["valence"] for point in points]
    peak = max(points, key=lambda item: item["intensity"], default={})
    valley = min(points, key=lambda item: item["valence"], default={})
    volatility = mean([abs(intensities[index] - intensities[index - 1]) for index in range(1, len(intensities))]) if len(intensities) > 1 else 0
    return {
        "chapter_points": points[:160],
        "volatility_avg_delta": round(volatility, 2),
        "peak_chapter": peak.get("chapter"),
        "peak_intensity": peak.get("intensity", 0),
        "valley_chapter": valley.get("chapter"),
        "valley_valence": valley.get("valence", 0),
    }


def _merge_foreshadowing_pattern(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合伏笔模式。"""
    total_chars = sum(item["sample"]["word_count"] for item in reports)
    planting_count = sum(_count_from_density(item["foreshadowing_pattern"]["planting_density_per_1k_chars"], item["sample"]["word_count"]) for item in reports)
    payoff_count = sum(_count_from_density(item["foreshadowing_pattern"]["payoff_density_per_1k_chars"], item["sample"]["word_count"]) for item in reports)
    misdirection_count = sum(item["foreshadowing_pattern"]["misdirection_strategy"]["marker_count"] for item in reports)
    methods = {"anomaly_marker_count": 0, "object_detail_count": 0, "gaze_or_silence_count": 0}
    for item in reports:
        for key in methods:
            methods[key] += item["foreshadowing_pattern"]["planting_methods"].get(key, 0)
    cycles = [
        item["foreshadowing_pattern"]["estimated_payoff_cycle_chapters_avg"]
        for item in reports
        if item["foreshadowing_pattern"]["estimated_payoff_cycle_chapters_avg"]
    ]
    return {
        "planting_density_per_1k_chars": _density(planting_count, total_chars),
        "payoff_density_per_1k_chars": _density(payoff_count, total_chars),
        "estimated_payoff_cycle_chapters_avg": round(mean(cycles), 2) if cycles else 0,
        "planting_methods": methods,
        "misdirection_strategy": {
            "marker_count": round(misdirection_count),
            "density_per_1k_chars": _density(misdirection_count, total_chars),
        },
    }


def _merge_dialogue_style(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合对白风格。"""
    total_chars = sum(item["sample"]["word_count"] for item in reports)
    dialogue_count = sum(item["dialogue_style"]["dialogue_count"] for item in reports)
    return {
        "dialogue_count": dialogue_count,
        "dialogue_ratio": round(_weighted_avg([(item["dialogue_style"]["dialogue_ratio"], item["sample"]["word_count"]) for item in reports]), 4),
        "utterance_length": {
            "avg": _weighted_avg([(item["dialogue_style"]["utterance_length"]["avg"], item["dialogue_style"]["dialogue_count"]) for item in reports]),
            "p50": round(_weighted_avg([(item["dialogue_style"]["utterance_length"]["p50"], item["dialogue_style"]["dialogue_count"]) for item in reports])),
            "p90": round(_weighted_avg([(item["dialogue_style"]["utterance_length"]["p90"], item["dialogue_style"]["dialogue_count"]) for item in reports])),
            "short_utterance_ratio": round(_weighted_avg([(item["dialogue_style"]["utterance_length"]["short_utterance_ratio"], item["dialogue_style"]["dialogue_count"]) for item in reports]), 4),
        },
        "subtext_ratio": round(_weighted_avg([(item["dialogue_style"]["subtext_ratio"], item["dialogue_style"]["dialogue_count"]) for item in reports]), 4),
        "interruption_frequency_per_100_dialogues": _weighted_avg([(item["dialogue_style"]["interruption_frequency_per_100_dialogues"], item["dialogue_style"]["dialogue_count"]) for item in reports]),
        "explanatory_dialogue_ratio": round(_weighted_avg([(item["dialogue_style"]["explanatory_dialogue_ratio"], item["dialogue_style"]["dialogue_count"]) for item in reports]), 4),
        "dialogue_density_per_1k_chars": _density(dialogue_count, total_chars),
    }


def _merge_pacing_model(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合节奏模型。"""
    total_chars = sum(item["sample"]["word_count"] for item in reports)
    conflict_count = sum(_count_from_density(item["pacing_model"]["conflict_density_per_1k_chars"], item["sample"]["word_count"]) for item in reports)
    info_count = sum(_count_from_density(item["pacing_model"]["information_release_density_per_1k_chars"], item["sample"]["word_count"]) for item in reports)
    hook_distribution: dict[str, int] = {}
    for item in reports:
        for hook, count in item["pacing_model"]["ending_hook_type_distribution"].items():
            hook_distribution[hook] = hook_distribution.get(hook, 0) + count
    return {
        "conflict_interval_chars_est": round(total_chars / max(conflict_count, 1), 2),
        "conflict_density_per_1k_chars": _density(conflict_count, total_chars),
        "information_release_density_per_1k_chars": _density(info_count, total_chars),
        "ending_hook_type_distribution": hook_distribution,
        "ending_hook_strength_avg": _weighted_avg([(item["pacing_model"]["ending_hook_strength_avg"], item["sample"]["chapter_count"]) for item in reports]),
    }


def _build_chunk_summaries(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保留每个分片的非原文摘要，方便排查样本分析质量。"""
    summaries = []
    for item in reports:
        emotion = item["emotion_curve"]
        pacing = item["pacing_model"]
        hook_distribution = pacing.get("ending_hook_type_distribution") or {}
        dominant_hook = max(hook_distribution, key=hook_distribution.get) if hook_distribution else "soft_pause"
        summaries.append(
            {
                "chunk_index": item["chunk_index"],
                "word_count": item["sample"]["word_count"],
                "dominant_emotion": (emotion.get("chapter_points") or [{}])[0].get("label", "neutral"),
                "peak_intensity": emotion.get("peak_intensity", 0),
                "dominant_hook": dominant_hook,
            }
        )
    return summaries[:240]


def _build_transferable_style_vector(report: dict[str, Any]) -> dict[str, Any]:
    """提取后续生成可直接读取的工程化风格向量。"""
    style = report["style_fingerprint"]
    emotion = report["emotion_curve"]
    foreshadowing = report["foreshadowing_pattern"]
    dialogue = report["dialogue_style"]
    pacing = report["pacing_model"]
    return {
        "target_sentence_avg": style["sentence_length"]["avg"],
        "target_paragraph_avg": style["paragraph_rhythm"]["avg_paragraph_chars"],
        "paragraph_p90": style["paragraph_rhythm"]["p90_paragraph_chars"],
        "over_preferred_paragraph_ratio": style["paragraph_rhythm"].get("over_preferred_paragraph_ratio", 0),
        "over_hard_limit_paragraph_ratio": style["paragraph_rhythm"].get("over_hard_limit_paragraph_ratio", 0),
        "short_sentence_ratio": style["sentence_length"]["distribution"]["short_1_12"],
        "dialogue_ratio": dialogue["dialogue_ratio"],
        "subtext_ratio": dialogue["subtext_ratio"],
        "metaphor_density_per_1k_chars": style["metaphor_density_per_1k_chars"],
        "sensory_covered_dimension_count": style["sensory_coverage"]["covered_dimension_count"],
        "emotion_volatility": emotion["volatility_avg_delta"],
        "foreshadowing_density_per_1k_chars": foreshadowing["planting_density_per_1k_chars"],
        "payoff_cycle_chapters": foreshadowing["estimated_payoff_cycle_chapters_avg"],
        "conflict_interval_chars": pacing["conflict_interval_chars_est"],
        "info_release_density_per_1k_chars": pacing["information_release_density_per_1k_chars"],
        "ending_hook_strength": pacing["ending_hook_strength_avg"],
    }
