"""检查生成正文是否连续复刻了检索到的样本片段。"""

from __future__ import annotations

import re
from typing import Any


def _normalize(text: str) -> str:
    return re.sub(r"[\s，。！？；：、“”‘’…—,.!?;:'\"-]+", "", text or "")


def _longest_common_substring(left: str, right: str) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    longest = 0
    for left_char in left:
        current = [0]
        for index, right_char in enumerate(right, start=1):
            value = previous[index - 1] + 1 if left_char == right_char else 0
            current.append(value)
            longest = max(longest, value)
        previous = current
    return longest


def build_reference_overlap_report(
    content: str,
    reference_pack: dict[str, Any] | None,
) -> dict[str, Any]:
    normalized_content = _normalize(content)
    matches = []
    for reference in (reference_pack or {}).get("references", []):
        excerpt = _normalize(str(reference.get("excerpt") or ""))
        if not excerpt:
            continue
        longest = _longest_common_substring(normalized_content, excerpt)
        excerpt_ratio = round(longest / max(len(excerpt), 1), 4)
        if longest >= 24 or (longest >= 16 and excerpt_ratio >= 0.2):
            matches.append(
                {
                    "passage_id": reference.get("passage_id", ""),
                    "longest_exact_match_chars": longest,
                    "excerpt_match_ratio": excerpt_ratio,
                }
            )
    return {
        "status": "failed" if matches else "passed",
        "has_risky_overlap": bool(matches),
        "match_count": len(matches),
        "matches": matches,
        "rule": "连续相同字符不少于 24，或不少于 16 且覆盖参考片段 20%",
    }
