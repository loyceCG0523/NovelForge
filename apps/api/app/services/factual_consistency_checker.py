"""局部事实一致性审校服务。

第一版先做确定性规则检查，专门兜住“说是三个字，但引号里实际四个字”这类
低级且可计算的错误。后续可以继续扩展金额、人数、时间等规则。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue


AUTO_REVIEW_SOURCE = "factual_consistency_checker"
ALLOWED_SEVERITIES = {"low", "medium", "high"}
COUNT_WORD_PATTERN = re.compile(
    r"(?P<count>[零〇一二两三四五六七八九十百千万\d]+)\s*个字[^“”\"']{0,12}[“\"'](?P<quote>[^”\"']{1,40})[”\"']"
)


def _parse_chinese_number(text: str) -> int | None:
    """解析简单中文数字，覆盖“一二三十十二二十”等常见写法。"""
    value = str(text or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)

    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value in digits:
        return digits[value]
    if "十" in value:
        left, _, right = value.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def _count_meaningful_chars(text: str) -> int:
    """计算引号内有意义字符数量，排除空白和标点。"""
    count = 0
    for char in text:
        if char.isspace():
            continue
        if unicodedata.category(char).startswith("P"):
            continue
        count += 1
    return count


def check_local_facts(chapter: Chapter) -> list[dict[str, Any]]:
    """执行规则型局部事实检查。"""
    content = chapter.content or ""
    issues: list[dict[str, Any]] = []
    for match in COUNT_WORD_PATTERN.finditer(content):
        expected_count = _parse_chinese_number(match.group("count"))
        quote = match.group("quote")
        actual_count = _count_meaningful_chars(quote)
        if expected_count is None or expected_count == actual_count:
            continue
        evidence = match.group(0)
        issues.append(
            {
                "issue_type": "count_mismatch",
                "severity": "medium",
                "message": f"局部字数描述不一致：文中说“{match.group('count')}个字”，但“{quote}”实际是 {actual_count} 个字。",
                "payload": {
                    "source": AUTO_REVIEW_SOURCE,
                    "auto_generated": True,
                    "evidence": evidence,
                    "expected": f"引号内容应为 {expected_count} 个字，或把数量改为 {actual_count}。",
                    "suggestion": f"将“{match.group('count')}个字”改为“{actual_count}个字”，或替换引号内文字。",
                    "rule": "quoted_word_count",
                    "expected_count": expected_count,
                    "actual_count": actual_count,
                    "quoted_text": quote,
                },
            }
        )
    return issues[:12]


def sync_factual_consistency_issues(
    db: Session,
    novel: Novel,
    chapter: Chapter,
) -> list[ReviewIssue]:
    """同步当前章节的规则型事实一致性问题。"""
    existing_issues = db.scalars(
        select(ReviewIssue).where(
            ReviewIssue.novel_id == novel.id,
            ReviewIssue.chapter_id == chapter.id,
            ReviewIssue.issue_type.in_(["count_mismatch", "local_fact_consistency", "timeline_conflict"]),
        )
    ).all()
    for issue in existing_issues:
        if issue.status == "open" and (issue.payload or {}).get("source") == AUTO_REVIEW_SOURCE:
            db.delete(issue)

    records = check_local_facts(chapter)
    issues = [
        ReviewIssue(
            novel_id=novel.id,
            chapter_id=chapter.id,
            issue_type=record["issue_type"],
            severity=record["severity"] if record["severity"] in ALLOWED_SEVERITIES else "medium",
            status="open",
            message=record["message"],
            payload=record["payload"],
        )
        for record in records
    ]
    db.add_all(issues)
    db.commit()
    for issue in issues:
        db.refresh(issue)
    return issues
