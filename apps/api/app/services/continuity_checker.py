"""连续性审校服务。

连续性审校负责把“已生成章节”反向拿去对照 ChapterContext，检查人物、道具、
地点、时间线、伏笔和世界规则是否与既有信息冲突，并把问题写入 ReviewIssue。
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue
from app.services.llm_client import LLMClient, LLMConfig


AUTO_REVIEW_SOURCE = "continuity_checker"
ALLOWED_SEVERITIES = {"low", "medium", "high"}


def build_continuity_check_prompt(novel: Novel, chapter: Chapter, context: dict) -> list[dict[str, str]]:
    """构造连续性审校 prompt，要求模型只返回结构化 JSON。"""
    compact_context = {
        "novel": context.get("novel", {}),
        "target": context.get("target", {}),
        "recent_chapters": context.get("recent_chapters", []),
        "structured_memory": context.get("memories", []),
        "active_foreshadowing": context.get("foreshadowing", []),
        "open_review_issues": context.get("review_issues", []),
        "constraints": context.get("constraints", {}),
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 NovelForge 的连续性审校 Agent。你的任务不是改写章节，"
                "而是判断当前章节是否违背已有上下文、结构化记忆、伏笔、时间线或世界规则。"
                "只输出 JSON，不要输出 Markdown 或解释。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请审校当前章节是否存在连续性问题。\n"
                "重点检查：人物身份/性格/关系冲突，道具归属或状态冲突，地点和时间线冲突，"
                "伏笔遗漏或提前回收，世界规则被改写，前文事件被错误复述。\n"
                "不要把普通文风建议当作连续性问题；没有明确证据时不要报错。\n"
                "输出格式必须为：\n"
                "{\n"
                '  "issues": [\n'
                "    {\n"
                '      "issue_type": "continuity_character|continuity_item|continuity_timeline|continuity_foreshadowing|continuity_world_rule|continuity_event",\n'
                '      "severity": "low|medium|high",\n'
                '      "message": "给用户看的简短问题说明",\n'
                '      "evidence": "当前章节中的依据",\n'
                '      "expected": "根据上下文应该保持的设定",\n'
                '      "suggestion": "建议如何修正"\n'
                "    }\n"
                "  ]\n"
                "}\n\n"
                f"作品标题：{novel.title}\n"
                f"待审章节：第 {chapter.chapter_index} 章《{chapter.title or '未命名章节'}》\n"
                f"章节摘要：{chapter.summary or '暂无摘要'}\n"
                f"章节正文：\n{chapter.content or ''}\n\n"
                f"生成时上下文快照：\n{compact_context}"
            ),
        },
    ]


def normalize_continuity_issues(raw_issues: Any) -> list[dict[str, Any]]:
    """清洗 LLM 输出，确保能安全写入 ReviewIssue。"""
    if not isinstance(raw_issues, list):
        return []

    normalized: list[dict[str, Any]] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            continue
        message = str(raw.get("message") or "").strip()
        if not message:
            continue

        issue_type = str(raw.get("issue_type") or "continuity_event").strip()
        if not issue_type.startswith("continuity_"):
            issue_type = "continuity_event"

        severity = str(raw.get("severity") or "medium").strip().lower()
        if severity not in ALLOWED_SEVERITIES:
            severity = "medium"

        normalized.append(
            {
                "issue_type": issue_type[:60],
                "severity": severity,
                "message": message,
                "payload": {
                    "source": AUTO_REVIEW_SOURCE,
                    "auto_generated": True,
                    "evidence": str(raw.get("evidence") or ""),
                    "expected": str(raw.get("expected") or ""),
                    "suggestion": str(raw.get("suggestion") or ""),
                },
            }
        )
    return normalized[:12]


def check_chapter_continuity(
    novel: Novel,
    chapter: Chapter,
    context: dict,
    llm_config: LLMConfig | None,
) -> list[dict[str, Any]]:
    """执行连续性审校；没有 LLM 配置时不产生自动风险。"""
    if llm_config is None:
        return []
    _, parsed = LLMClient(llm_config).complete_json(build_continuity_check_prompt(novel, chapter, context))
    return normalize_continuity_issues(parsed.get("issues"))


def sync_continuity_issues(
    db: Session,
    novel: Novel,
    chapter: Chapter,
    context: dict,
    llm_config: LLMConfig | None,
) -> list[ReviewIssue]:
    """同步当前章节的自动连续性审校结果，避免同章重复堆积旧风险。"""
    existing_issues = db.scalars(
        select(ReviewIssue).where(
            ReviewIssue.novel_id == novel.id,
            ReviewIssue.chapter_id == chapter.id,
            ReviewIssue.issue_type.like("continuity_%"),
        )
    ).all()
    for issue in existing_issues:
        if issue.status == "open" and (issue.payload or {}).get("source") == AUTO_REVIEW_SOURCE:
            db.delete(issue)

    issue_records = check_chapter_continuity(
        novel=novel,
        chapter=chapter,
        context=context,
        llm_config=llm_config,
    )
    issues = [
        ReviewIssue(
            novel_id=novel.id,
            chapter_id=chapter.id,
            issue_type=record["issue_type"],
            severity=record["severity"],
            status="open",
            message=record["message"],
            payload=record["payload"],
        )
        for record in issue_records
    ]
    db.add_all(issues)
    db.commit()
    for issue in issues:
        db.refresh(issue)
    return issues
