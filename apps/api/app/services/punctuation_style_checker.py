"""中文小说标点与句式节奏审校。

这类问题不依赖模型判断：当同一段内连续出现多个很短的陈述句，且全部被句号
硬切开时，往往会形成机械罗列的 AI 写作痕迹。规则只标记明显的连续短句，避免
干预有意保留的正常短句节奏。
"""

from __future__ import annotations

import re
from typing import Any

from app.models.chapter import Chapter


AUTO_REVIEW_SOURCE = "punctuation_style_checker"
ISSUE_TYPE = "punctuation_fragmentation"
SHORT_SENTENCE_MAX_CHARS = 18
MIN_FRAGMENT_RUN = 3
MAX_ISSUES_PER_CHAPTER = 6
TWO_SENTENCE_FRAGMENT_MAX_CHARS = 14
FRAGMENT_VERB_MARKERS = (
    "是", "有", "在", "把", "被", "会", "能", "要", "想", "让", "使", "给",
    "放", "拿", "找", "看", "听", "说", "问", "走", "跑", "坐", "站", "进",
    "出", "回", "来", "去", "开", "关", "等", "躺", "写", "读", "吃", "喝",
)
FRAGMENT_NOUN_MARKERS = (
    "柜", "门", "桌", "床", "抽屉", "架", "盒", "包", "袋", "箱", "角落",
    "里面", "外面", "左边", "右边", "楼上", "楼下", "前面", "后面", "旁边",
    "卫生间", "厨房", "客厅", "卧室", "走廊", "电梯", "车里", "口袋",
)
ORDINAL_NOUN_PATTERN = re.compile(r"^第[一二三四五六七八九十百千万两\d]+(?:个|间|层|扇|只|张|把|本|位|件|枚).+")


def _meaningful_length(sentence: str) -> int:
    """忽略空白和标点，得到用于判断节奏的有效字符数。"""
    return sum(1 for char in sentence if not char.isspace() and not re.match(r"[\W_]", char))


def _extract_sentences(paragraph: str) -> list[str]:
    return [item.strip() for item in re.findall(r"[^。！？!?]+[。！？!?]", paragraph) if item.strip()]


def _is_short_period_sentence(sentence: str) -> bool:
    return sentence.endswith("。") and _meaningful_length(sentence) <= SHORT_SENTENCE_MAX_CHARS


def _is_fragmentary_follow_up(sentence: str) -> bool:
    """识别“第二个柜子。”这类缺谓语的位置/物品补充残句。"""
    text = sentence.strip().rstrip("。")
    if not text or _meaningful_length(text) > TWO_SENTENCE_FRAGMENT_MAX_CHARS:
        return False
    if any(marker in text for marker in FRAGMENT_VERB_MARKERS):
        return False
    return bool(ORDINAL_NOUN_PATTERN.match(text) or any(marker in text for marker in FRAGMENT_NOUN_MARKERS))


def _extract_short_sentence_runs(paragraph: str) -> list[list[str]]:
    """提取连续使用句号结尾的短句组。"""
    sentences = _extract_sentences(paragraph)
    runs: list[list[str]] = []
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        if len(current) >= MIN_FRAGMENT_RUN:
            lengths = [_meaningful_length(item) for item in current]
            # 既要连续短句，也要整体非常紧凑；避免误伤正常的短句叙述节奏。
            if sum(lengths) <= 42 and sum(length <= 10 for length in lengths) >= 2:
                runs.append(current)
        current = []

    for sentence in sentences:
        if _is_short_period_sentence(sentence):
            current.append(sentence)
        else:
            flush()
    flush()
    return runs


def _extract_two_sentence_fragments(paragraph: str) -> list[list[str]]:
    """提取孤立的“双短句 + 第二句补充残句”组合。"""
    sentences = _extract_sentences(paragraph)
    pairs: list[list[str]] = []
    for index in range(len(sentences) - 1):
        first, second = sentences[index], sentences[index + 1]
        if not (_is_short_period_sentence(first) and _is_short_period_sentence(second)):
            continue
        # 三句以上的连续短句由既有规则处理，避免同一位置重复报警。
        if (index > 0 and _is_short_period_sentence(sentences[index - 1])) or (
            index + 2 < len(sentences) and _is_short_period_sentence(sentences[index + 2])
        ):
            continue
        if _is_fragmentary_follow_up(second):
            pairs.append([first, second])
    return pairs


def check_punctuation_style(chapter: Chapter) -> list[dict[str, Any]]:
    """检查章节中明显的机械句号切分。"""
    records: list[dict[str, Any]] = []
    paragraphs = [item.strip() for item in re.split(r"\r?\n+", chapter.content or "") if item.strip()]
    for paragraph_index, paragraph in enumerate(paragraphs, start=1):
        for sentence_run in _extract_short_sentence_runs(paragraph):
            evidence = "".join(sentence_run)
            sentence_lengths = [_meaningful_length(item) for item in sentence_run]
            count = len(sentence_run)
            records.append(
                {
                    "issue_type": ISSUE_TYPE,
                    "severity": "medium" if count >= 4 else "low",
                    "message": f"第 {paragraph_index} 段连续 {count} 个短句均以句号硬切，阅读节奏像机械罗列，容易显出 AI 写作痕迹。",
                    "payload": {
                        "source": AUTO_REVIEW_SOURCE,
                        "auto_generated": True,
                        "evidence": evidence,
                        "expected": "保留必要的短句力度，但同一逻辑链不应连续用多个句号切碎。",
                        "suggestion": "结合语义将相邻短句用逗号、分号、转折或因果衔接；只保留真正需要强调的句号停顿。",
                        "rule": "consecutive_short_full_stops",
                        "paragraph_index": paragraph_index,
                        "sentence_count": count,
                        "sentence_lengths": sentence_lengths,
                    },
                }
            )
            if len(records) >= MAX_ISSUES_PER_CHAPTER:
                return records
        for sentence_pair in _extract_two_sentence_fragments(paragraph):
            evidence = "".join(sentence_pair)
            sentence_lengths = [_meaningful_length(item) for item in sentence_pair]
            records.append(
                {
                    "issue_type": ISSUE_TYPE,
                    "severity": "low",
                    "message": f"第 {paragraph_index} 段出现双短句碎片化表达：第二句像缺谓语的位置或物品补充，句号硬切容易显出 AI 痕迹。",
                    "payload": {
                        "source": AUTO_REVIEW_SOURCE,
                        "auto_generated": True,
                        "evidence": evidence,
                        "expected": "位置、物品或序号补充应与前句保持完整语义关系，不应单独形成残句。",
                        "suggestion": "将两句按语义合并，例如用逗号、冒号、连接词或补全第二句谓语；仅在刻意制造停顿时保留分句。",
                        "rule": "two_short_sentence_fragment",
                        "paragraph_index": paragraph_index,
                        "sentence_count": 2,
                        "sentence_lengths": sentence_lengths,
                    },
                }
            )
            if len(records) >= MAX_ISSUES_PER_CHAPTER:
                return records
    return records
