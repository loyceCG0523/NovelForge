"""章节审校问题的纯规则批处理策略。"""

from __future__ import annotations

from typing import Protocol, TypeVar


MAX_ISSUES_PER_REPAIR_BATCH = 6
# 标点碎片修复倾向合并短句，段落过长修复倾向拆段，不能交给同一次 LLM 改写。
CONFLICTING_ISSUE_TYPE_PAIRS = {
    frozenset({"punctuation_fragmentation", "paragraph_overlength"}),
}


class RepairIssue(Protocol):
    issue_type: str
    severity: str


RepairIssueT = TypeVar("RepairIssueT", bound=RepairIssue)


def select_repair_batch(issues: list[RepairIssueT]) -> list[RepairIssueT]:
    """选择一次修订可共同处理的问题；高风险问题始终独立成批。"""
    if not issues:
        return []
    first = issues[0]
    if first.severity == "high":
        return [first]

    batch = [first]
    for candidate in issues[1:]:
        if len(batch) >= MAX_ISSUES_PER_REPAIR_BATCH:
            break
        if candidate.severity == "high":
            continue
        if any(
            frozenset({candidate.issue_type, selected.issue_type}) in CONFLICTING_ISSUE_TYPE_PAIRS
            for selected in batch
        ):
            continue
        batch.append(candidate)
    return batch
