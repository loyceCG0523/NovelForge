"""章节修订服务。

RevisionAgent 根据 ReviewIssue 对已有章节做定向修订：只修复指定问题，尽量保留原文结构、
人物关系和叙事节奏。这里不直接处理任务状态，任务编排由 Worker 负责。
"""

import json
from typing import Any

from app.models.chapter import Chapter
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue
from app.services.llm_client import LLMClient, LLMConfig


def _issue_to_payload(issue: ReviewIssue) -> dict[str, Any]:
    """把审校问题整理成模型容易理解的修订目标。"""
    payload = issue.payload or {}
    return {
        "id": str(issue.id),
        "issue_type": issue.issue_type,
        "severity": issue.severity,
        "message": issue.message,
        "evidence": payload.get("evidence", ""),
        "expected": payload.get("expected", ""),
        "suggestion": payload.get("suggestion", ""),
    }


def build_revision_prompt(
    novel: Novel,
    chapter: Chapter,
    issue: ReviewIssue,
    context: dict,
) -> list[dict[str, str]]:
    """构建章节修订 Prompt，要求模型只返回 JSON。"""
    compact_context = {
        "novel": context.get("novel", {}),
        "target": context.get("target", {}),
        "recent_chapters": context.get("recent_chapters", []),
        "structured_memory": context.get("memories", []),
        "active_foreshadowing": context.get("foreshadowing", []),
        "open_review_issues": context.get("review_issues", []),
        "constraints": context.get("constraints", {}),
    }
    revision_payload = {
        "task": {
            "type": "revise_chapter",
            "goal": "只修复指定审校问题，不要重写无关剧情，不要改变已成立的人物设定和章节核心事件。",
            "expected_output": {
                "title": "修订后的章节标题，可保持原标题",
                "summary": "修订后的章节摘要，200 字以内",
                "content": "修订后的完整章节正文",
                "revision_note": "简述修复了什么，不超过 80 字",
            },
        },
        "target_issue": _issue_to_payload(issue),
        "chapter": {
            "chapter_index": chapter.chapter_index,
            "title": chapter.title,
            "summary": chapter.summary,
            "content": chapter.content,
        },
        "context": compact_context,
    }
    return [
        {
            "role": "system",
            "content": "\n".join(
                [
                    "你是 NovelForge 的章节修订 Agent。",
                    "你的任务是根据指定 ReviewIssue 对已有中文小说章节做定向修订。",
                    "必须优先保持原章节的叙事结构、人物口吻、场景顺序和主线推进。",
                    "只修复 target_issue 指出的连续性、行为合理性或局部事实一致性问题，不要借机大改、扩写或删掉无关内容。",
                    "如果问题是行为动机不足，优先补一两处能支撑行为的现实压力、安全缓冲、人物心理或伏笔解释。",
                    "如果问题是数字、字数、时间、人数等局部事实错误，优先做最小文本修正。",
                    "必须降低 AI 味：避免模板句、解释性总结、空泛情绪词和说教。",
                    "输出必须是 JSON 对象，字段固定为 title、summary、content、revision_note。",
                ]
            ),
        },
        {
            "role": "user",
            "content": "\n".join(
                [
                    f"请修订《{novel.title}》第 {chapter.chapter_index} 章。",
                    "不要输出 Markdown，不要输出 JSON 以外的解释文字。",
                    json.dumps(revision_payload, ensure_ascii=False, indent=2),
                ]
            ),
        },
    ]


def revise_chapter_with_llm(
    novel: Novel,
    chapter: Chapter,
    issue: ReviewIssue,
    context: dict,
    llm_config: LLMConfig,
) -> dict[str, str]:
    """调用 LLM 完成章节修订，并归一化返回字段。"""
    raw_response, parsed = LLMClient(llm_config).complete_json(
        build_revision_prompt(novel=novel, chapter=chapter, issue=issue, context=context)
    )
    content = str(parsed.get("content") or "").strip()
    if not content:
        raise ValueError("RevisionAgent 没有返回修订后的章节正文")

    return {
        "title": str(parsed.get("title") or chapter.title or "未命名章节").strip(),
        "summary": str(parsed.get("summary") or chapter.summary or "").strip(),
        "content": content,
        "revision_note": str(parsed.get("revision_note") or "已根据审校问题完成定向修订。").strip(),
        "raw_response": raw_response,
    }
