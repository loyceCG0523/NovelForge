"""行为合理性审校服务。

ReasonabilityChecker 关注角色行为动机是否成立，而不是文风好坏。它主要检查：
陌生人留宿、突然信任、危险行为、重大选择、情绪转折等是否有足够动机和安全缓冲。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue
from app.services.llm_client import LLMClient, LLMConfig


AUTO_REVIEW_SOURCE = "reasonability_checker"
ALLOWED_SEVERITIES = {"low", "medium", "high"}


def build_reasonability_check_prompt(novel: Novel, chapter: Chapter, context: dict) -> list[dict[str, str]]:
    """构造行为合理性审校 prompt。"""
    compact_context = {
        "novel": context.get("novel", {}),
        "story_bible": context.get("story_bible", {}),
        "target": context.get("target", {}),
        "recent_chapters": context.get("recent_chapters", []),
        "structured_memory": context.get("memories", []),
        "constraints": context.get("constraints", {}),
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 NovelForge 的人物行为合理性审校 Agent。你的任务是检查角色行为是否符合现实逻辑、"
                "人物设定、当前处境和安全常识。不要评价文笔，不要提出泛泛风格建议。只输出 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请审校当前章节是否存在行为动机缺口。\n"
                "重点检查：陌生人快速信任、进入私人空间、留宿、危险行为、重大关系推进、情绪突然转折、"
                "角色能力与行为不匹配、缺少安全缓冲或必要解释。\n"
                "没有明确问题时返回空 issues。输出格式：\n"
                "{\n"
                '  "issues": [\n'
                "    {\n"
                '      "issue_type": "behavior_reasonability",\n'
                '      "severity": "low|medium|high",\n'
                '      "character": "相关角色",\n'
                '      "scene": "问题场景",\n'
                '      "message": "给用户看的简短问题说明",\n'
                '      "evidence": "当前章节中的依据",\n'
                '      "expected": "合理情况下需要补足的动机或约束",\n'
                '      "suggestion": "具体修正建议"\n'
                "    }\n"
                "  ]\n"
                "}\n\n"
                f"作品标题：{novel.title}\n"
                f"待审章节：第 {chapter.chapter_index} 章《{chapter.title or '未命名章节'}》\n"
                f"章节摘要：{chapter.summary or '暂无摘要'}\n"
                f"章节正文：\n{chapter.content or ''}\n\n"
                f"上下文快照：\n{compact_context}"
            ),
        },
    ]


def normalize_reasonability_issues(raw_issues: Any) -> list[dict[str, Any]]:
    """清洗模型输出，确保能安全写入 ReviewIssue。"""
    if not isinstance(raw_issues, list):
        return []
    normalized: list[dict[str, Any]] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            continue
        message = str(raw.get("message") or "").strip()
        if not message:
            continue
        severity = str(raw.get("severity") or "medium").strip().lower()
        if severity not in ALLOWED_SEVERITIES:
            severity = "medium"
        normalized.append(
            {
                "issue_type": "behavior_reasonability",
                "severity": severity,
                "message": message,
                "payload": {
                    "source": AUTO_REVIEW_SOURCE,
                    "auto_generated": True,
                    "character": str(raw.get("character") or ""),
                    "scene": str(raw.get("scene") or ""),
                    "evidence": str(raw.get("evidence") or ""),
                    "expected": str(raw.get("expected") or ""),
                    "suggestion": str(raw.get("suggestion") or ""),
                },
            }
        )
    return normalized[:8]


def check_chapter_reasonability(
    novel: Novel,
    chapter: Chapter,
    context: dict,
    llm_config: LLMConfig | None,
) -> list[dict[str, Any]]:
    """执行行为合理性审校；没有 LLM 配置时跳过。"""
    if llm_config is None:
        return []
    _, parsed = LLMClient(llm_config).complete_json(build_reasonability_check_prompt(novel, chapter, context))
    return normalize_reasonability_issues(parsed.get("issues"))


def sync_reasonability_issues(
    db: Session,
    novel: Novel,
    chapter: Chapter,
    context: dict,
    llm_config: LLMConfig | None,
) -> list[ReviewIssue]:
    """同步当前章节的行为合理性审校结果。"""
    existing_issues = db.scalars(
        select(ReviewIssue).where(
            ReviewIssue.novel_id == novel.id,
            ReviewIssue.chapter_id == chapter.id,
            ReviewIssue.issue_type == "behavior_reasonability",
        )
    ).all()
    for issue in existing_issues:
        if issue.status == "open" and (issue.payload or {}).get("source") == AUTO_REVIEW_SOURCE:
            db.delete(issue)

    records = check_chapter_reasonability(novel=novel, chapter=chapter, context=context, llm_config=llm_config)
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
        for record in records
    ]
    db.add_all(issues)
    db.commit()
    for issue in issues:
        db.refresh(issue)
    return issues
