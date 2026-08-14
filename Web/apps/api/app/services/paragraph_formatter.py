"""章节正文的确定性自然分段。

默认只在完整句末分段，不为了追求固定字数在普通逗号处截断。目标段长为 60 字；
单个完整句可以自然超过目标值，但绝不能超过 120 字。只有句子本身超过 120 字时，
才依次尝试分号、冒号、逗号和字符边界作为硬性兜底。
"""

from __future__ import annotations

import re


PREFERRED_PARAGRAPH_CHARS = 60
VERY_LONG_PARAGRAPH_CHARS = 80
HARD_MAX_PARAGRAPH_CHARS = 120
MAX_LONG_PARAGRAPHS_PER_CHAPTER = 3
MAX_VERY_LONG_PARAGRAPHS_PER_CHAPTER = 1

SENTENCE_END_PUNCTUATION = frozenset("。！？!?")
CLOSING_PUNCTUATION = frozenset("”’」』）》】〕〉\"'")
STRONG_FALLBACK_PUNCTUATION = frozenset("；;：:")
WEAK_FALLBACK_PUNCTUATION = frozenset("，,")
CONTINUATION_PUNCTUATION = frozenset("，,；;：:、—")
QUOTE_PAIRS = (("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"))


def meaningful_length(text: str) -> int:
    """统计可见字符；排版空白不计入正文长度。"""
    return sum(1 for char in text if not char.isspace())


def format_chapter_paragraphs(
    content: str | None,
    preferred_chars: int = PREFERRED_PARAGRAPH_CHARS,
    hard_max_chars: int = HARD_MAX_PARAGRAPH_CHARS,
) -> str:
    """按完整句重排段落，只增加、删除或归一化空白。"""
    if not content:
        return ""
    if preferred_chars < 1 or hard_max_chars < preferred_chars:
        raise ValueError("paragraph length limits are invalid")

    raw_paragraphs = [item.strip() for item in re.split(r"\r?\n+", content) if item.strip()]
    paragraphs = _coalesce_old_artificial_breaks(raw_paragraphs)
    formatted: list[str] = []
    for paragraph in paragraphs:
        sentences = _split_sentences(paragraph)
        chunks: list[str] = []
        for sentence in sentences:
            chunks.extend(_split_oversized_sentence(sentence, hard_max_chars))
        formatted.extend(_pack_complete_sentences(chunks, preferred_chars, hard_max_chars))
    return "\n\n".join(item for item in formatted if item)


def _coalesce_old_artificial_breaks(paragraphs: list[str]) -> list[str]:
    """修复旧版在逗号、分号或冒号后硬切出的假段落。"""
    merged: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        buffer = f"{buffer}{paragraph}" if buffer else paragraph
        if _ends_with_continuation_punctuation(buffer) or _has_unclosed_quote(buffer):
            continue
        merged.append(buffer)
        buffer = ""
    if buffer:
        merged.append(buffer)
    return merged


def _ends_with_continuation_punctuation(text: str) -> bool:
    stripped = text.rstrip()
    while stripped and stripped[-1] in CLOSING_PUNCTUATION:
        stripped = stripped[:-1].rstrip()
    return bool(stripped and stripped[-1] in CONTINUATION_PUNCTUATION)


def _has_unclosed_quote(text: str) -> bool:
    """识别仍在延续的直接引语，防止把同一人的连续对白拆成两个段落。"""
    if any(text.count(opening) > text.count(closing) for opening, closing in QUOTE_PAIRS):
        return True
    # 兼容模型偶尔返回的半角双引号；转义引号不参与计数。
    ascii_quotes = len(re.findall(r'(?<!\\)"', text))
    return ascii_quotes % 2 == 1


def _split_sentences(text: str) -> list[str]:
    """识别包含右引号的完整句末边界。"""
    sentences: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        if text[index] not in SENTENCE_END_PUNCTUATION:
            index += 1
            continue
        end = index + 1
        while end < len(text) and text[end] in CLOSING_PUNCTUATION:
            end += 1
        sentence = text[start:end].strip()
        if sentence:
            sentences.append(sentence)
        start = end
        index = end
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences or ([text.strip()] if text.strip() else [])


def _pack_complete_sentences(
    sentences: list[str],
    preferred_chars: int,
    hard_max_chars: int,
) -> list[str]:
    """按完整句组合段落；同一组尚未闭合的直接引语优先保持在同段。"""
    paragraphs: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current}{sentence}" if current else sentence
        quote_is_continuing = _has_unclosed_quote(current)
        exceeds_preferred = meaningful_length(candidate) > preferred_chars
        exceeds_hard_max = meaningful_length(candidate) > hard_max_chars
        if current and exceeds_preferred and (not quote_is_continuing or exceeds_hard_max):
            paragraphs.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        paragraphs.append(current)
    return paragraphs


def _split_oversized_sentence(sentence: str, hard_max_chars: int) -> list[str]:
    """仅对超过硬上限的单句做兜底拆分，优先使用较强语义标点。"""
    remaining = sentence
    chunks: list[str] = []
    while meaningful_length(remaining) > hard_max_chars:
        split_at = _last_boundary_within(remaining, hard_max_chars, STRONG_FALLBACK_PUNCTUATION)
        if split_at is None:
            split_at = _last_boundary_within(remaining, hard_max_chars, WEAK_FALLBACK_PUNCTUATION)
        if split_at is None:
            split_at = _index_after_meaningful_chars(remaining, hard_max_chars)
        left = remaining[:split_at].rstrip()
        right = remaining[split_at:].lstrip()
        if not left or not right:
            split_at = _index_after_meaningful_chars(remaining, hard_max_chars)
            left = remaining[:split_at].rstrip()
            right = remaining[split_at:].lstrip()
        chunks.append(left)
        remaining = right
    if remaining:
        chunks.append(remaining)
    return chunks


def _last_boundary_within(text: str, max_chars: int, punctuation: frozenset[str]) -> int | None:
    visible_count = 0
    candidates: list[int] = []
    for index, char in enumerate(text):
        if not char.isspace():
            visible_count += 1
        if visible_count > max_chars:
            break
        if char not in punctuation:
            continue
        split_at = index + 1
        candidate_count = visible_count
        while split_at < len(text) and text[split_at] in CLOSING_PUNCTUATION:
            candidate_count += 1
            split_at += 1
        if candidate_count <= max_chars and split_at < len(text):
            candidates.append(split_at)
    return candidates[-1] if candidates else None


def _index_after_meaningful_chars(text: str, max_chars: int) -> int:
    visible_count = 0
    for index, char in enumerate(text):
        if not char.isspace():
            visible_count += 1
        if visible_count >= max_chars:
            return index + 1
    return len(text)
