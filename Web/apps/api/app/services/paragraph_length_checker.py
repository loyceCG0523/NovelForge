"""网文段落长度的纯规则检查。"""

from __future__ import annotations

import re
from typing import Any

from app.models.chapter import Chapter
from app.services.paragraph_formatter import (
    HARD_MAX_PARAGRAPH_CHARS,
    MAX_LONG_PARAGRAPHS_PER_CHAPTER,
    MAX_VERY_LONG_PARAGRAPHS_PER_CHAPTER,
    PREFERRED_PARAGRAPH_CHARS,
    VERY_LONG_PARAGRAPH_CHARS,
    meaningful_length,
)


ISSUE_TYPE = "paragraph_overlength"
MAX_ISSUES_PER_CHAPTER = 12


def _dialogue_ratio(text: str) -> float:
    quoted = sum(len(item) for item in re.findall(r"[“\"]([^”\"]+)[”\"]", text))
    return quoted / max(meaningful_length(text), 1)


def check_paragraph_lengths(chapter: Chapter) -> list[dict[str, Any]]:
    """检查 60/80/120 字分级规则及每章长段配额，不直接写数据库。"""
    paragraphs = [
        item.strip()
        for item in re.split(r"\r?\n+", chapter.content or "")
        if item.strip()
    ]
    lengths = [meaningful_length(paragraph) for paragraph in paragraphs]
    over_preferred = [
        index
        for index, length in enumerate(lengths)
        if length > PREFERRED_PARAGRAPH_CHARS
    ]
    over_very_long = [
        index
        for index, length in enumerate(lengths)
        if length > VERY_LONG_PARAGRAPH_CHARS
    ]
    violations: dict[int, str] = {
        index: "hard_max"
        for index, length in enumerate(lengths)
        if length > HARD_MAX_PARAGRAPH_CHARS
    }
    for index in over_very_long[MAX_VERY_LONG_PARAGRAPHS_PER_CHAPTER:]:
        violations.setdefault(index, "very_long_quota")
    for index in over_preferred[MAX_LONG_PARAGRAPHS_PER_CHAPTER:]:
        violations.setdefault(index, "long_quota")

    records: list[dict[str, Any]] = []
    for zero_index, reason in sorted(violations.items()):
        paragraph = paragraphs[zero_index]
        paragraph_index = zero_index + 1
        length = lengths[zero_index]
        if reason == "hard_max":
            limit_text = f"超过硬性上限 {HARD_MAX_PARAGRAPH_CHARS} 字"
        elif reason == "very_long_quota":
            limit_text = (
                f"导致本章超过 {VERY_LONG_PARAGRAPH_CHARS} 字的段落多于 "
                f"{MAX_VERY_LONG_PARAGRAPHS_PER_CHAPTER} 段"
            )
        else:
            limit_text = (
                f"导致本章超过 {PREFERRED_PARAGRAPH_CHARS} 字的段落多于 "
                f"{MAX_LONG_PARAGRAPHS_PER_CHAPTER} 段"
            )
        records.append(
            {
                "issue_type": ISSUE_TYPE,
                "severity": "high" if reason == "hard_max" else "medium",
                "message": (
                    f"第 {paragraph_index} 段约 {length} 字，{limit_text}。"
                    "请优先在句号、问号或感叹号后自然换段。"
                ),
                "payload": {
                    "evidence": paragraph[:1200],
                    "rule": "mobile_readability_paragraph_length",
                    "violation_reason": reason,
                    "paragraph_index": paragraph_index,
                    "paragraph_length": length,
                    "preferred_limit": PREFERRED_PARAGRAPH_CHARS,
                    "very_long_limit": VERY_LONG_PARAGRAPH_CHARS,
                    "hard_limit": HARD_MAX_PARAGRAPH_CHARS,
                    "chapter_over_preferred_count": len(over_preferred),
                    "chapter_over_very_long_count": len(over_very_long),
                    "max_over_preferred_count": MAX_LONG_PARAGRAPHS_PER_CHAPTER,
                    "max_over_very_long_count": MAX_VERY_LONG_PARAGRAPHS_PER_CHAPTER,
                    "exceeds_hard_limit": reason == "hard_max",
                    "dialogue_heavy": _dialogue_ratio(paragraph) >= 0.45,
                },
            }
        )
        if len(records) >= MAX_ISSUES_PER_CHAPTER:
            break
    return records
