"""把样本原文拆成适合阅读与标注的章节单元。"""

from __future__ import annotations

import re
from dataclasses import dataclass


_CHINESE_NUMBER = r"[0-9０-９〇零一二三四五六七八九十百千万两俩壹贰叁肆伍陆柒捌玖拾佰仟]+"
_CHAPTER_HEADING = re.compile(
    rf"^(?:(?:正文|卷[一二三四五六七八九十百千万0-9]+)\s*)?"
    rf"(?:第\s*{_CHINESE_NUMBER}\s*[章回节卷]|(?:chapter|chap)\s*[0-9ivxlcdm]+)"
    rf"(?:\s*$|(?:\s+|[:：、.．\-—_])\s*.*)$",
    re.IGNORECASE,
)
_SPECIAL_HEADING = re.compile(
    r"^(?:楔子|序章|前言|引子|尾声|后记|终章|大结局|番外(?:篇|章)?)"
    r"(?:\s*$|(?:\s+|[:：、.．\-—_])\s*.*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SampleChapterUnit:
    sequence_no: int
    title: str
    content: str
    start_offset: int
    end_offset: int
    unit_type: str = "chapter"


def split_sample_chapters(text: str, fallback_chars: int = 24000) -> list[SampleChapterUnit]:
    """优先按独立章节标题拆分；无法识别时使用明确标记的阅读单元兜底。"""
    source = str(text or "")
    if not source.strip():
        return []
    headings = _find_headings(source)
    if headings:
        units: list[SampleChapterUnit] = []
        first_start = headings[0][0]
        if source[:first_start].strip():
            start, end, content = _trimmed_slice(source, 0, first_start)
            units.append(
                SampleChapterUnit(
                    sequence_no=1,
                    title="前置内容",
                    content=content,
                    start_offset=start,
                    end_offset=end,
                    unit_type="preface",
                )
            )
        for index, (start_offset, title) in enumerate(headings):
            end_offset = headings[index + 1][0] if index + 1 < len(headings) else len(source)
            start, end, content = _trimmed_slice(source, start_offset, end_offset)
            if not content:
                continue
            units.append(
                SampleChapterUnit(
                    sequence_no=len(units) + 1,
                    title=title,
                    content=content,
                    start_offset=start,
                    end_offset=end,
                    unit_type="chapter",
                )
            )
        if units:
            return units
    return _fallback_units(source, max(4000, int(fallback_chars)))


def locate_quote_in_units(
    units: list[SampleChapterUnit], quote_text: str, approximate_offset: int = 0
) -> tuple[int, int, int] | None:
    """在新章节中定位旧标注；重复原句选择最接近旧全局偏移的位置。"""
    quote = str(quote_text or "")
    if not quote:
        return None
    candidates: list[tuple[int, int, int, int]] = []
    for unit_index, unit in enumerate(units):
        cursor = 0
        while True:
            local_start = unit.content.find(quote, cursor)
            if local_start < 0:
                break
            absolute_start = unit.start_offset + local_start
            candidates.append(
                (
                    abs(absolute_start - max(0, approximate_offset)),
                    unit_index,
                    local_start,
                    local_start + len(quote),
                )
            )
            cursor = local_start + max(1, len(quote))
    if not candidates:
        return None
    _distance, unit_index, local_start, local_end = min(candidates)
    return unit_index, local_start, local_end


def _find_headings(text: str) -> list[tuple[int, str]]:
    headings: list[tuple[int, str]] = []
    cursor = 0
    for line in text.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        normalized = raw.strip()
        if normalized.startswith("#"):
            normalized = normalized.lstrip("#").strip()
        if 1 <= len(normalized) <= 160 and (
            _CHAPTER_HEADING.fullmatch(normalized)
            or _SPECIAL_HEADING.fullmatch(normalized)
        ):
            headings.append((cursor, normalized))
        cursor += len(line)
    return headings


def _fallback_units(text: str, target_chars: int) -> list[SampleChapterUnit]:
    units: list[SampleChapterUnit] = []
    cursor = 0
    while cursor < len(text):
        desired_end = min(len(text), cursor + target_chars)
        end = desired_end if desired_end == len(text) else _find_boundary(text, cursor, desired_end)
        start, trimmed_end, content = _trimmed_slice(text, cursor, end)
        cursor = max(end, cursor + 1)
        if not content:
            continue
        units.append(
            SampleChapterUnit(
                sequence_no=len(units) + 1,
                title=f"未识别章节 · 阅读单元 {len(units) + 1}",
                content=content,
                start_offset=start,
                end_offset=trimmed_end,
                unit_type="fallback",
            )
        )
    return units


def _find_boundary(text: str, start: int, desired_end: int) -> int:
    window = text[start:desired_end]
    candidates = [
        window.rfind("\n\n"),
        window.rfind("\n"),
        window.rfind("。"),
        window.rfind("！"),
        window.rfind("？"),
    ]
    boundary = max(candidates)
    if boundary >= int(len(window) * 0.6):
        return start + boundary + 1
    return desired_end


def _trimmed_slice(text: str, start: int, end: int) -> tuple[int, int, str]:
    raw = text[start:end]
    left_trimmed = len(raw) - len(raw.lstrip())
    content = raw.strip()
    actual_start = start + left_trimmed
    return actual_start, actual_start + len(content), content
