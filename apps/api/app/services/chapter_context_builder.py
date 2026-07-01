"""章节生成上下文构建器。

这个模块负责把“写第 N 章时必须知道的材料”整理成稳定的结构化快照。
后续无论是模拟生成、LLM prompt，还是 LangGraph 节点之间传递状态，都应优先复用
这里产出的 ChapterContext，避免每个 Agent 各自拼上下文导致遗漏。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.foreshadowing import Foreshadowing
from app.models.memory_item import MemoryItem
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue


def _chapter_to_context(chapter: Chapter) -> dict:
    """将章节 ORM 对象转成可序列化的上下文片段。"""
    return {
        "id": str(chapter.id),
        "chapter_index": chapter.chapter_index,
        "title": chapter.title,
        "status": chapter.status,
        "summary": chapter.summary,
        "content": chapter.content,
        "word_count": chapter.word_count,
    }


def _memory_to_context(memory: MemoryItem) -> dict:
    """将结构化记忆转成上下文片段，供生成器查询人物、地点、道具等状态。"""
    return {
        "id": str(memory.id),
        "memory_type": memory.memory_type,
        "entity_name": memory.entity_name,
        "chapter_index_start": memory.chapter_index_start,
        "chapter_index_end": memory.chapter_index_end,
        "payload": memory.payload,
    }


def _foreshadowing_to_context(item: Foreshadowing) -> dict:
    """将未完成或待推进的伏笔整理为生成时可参考的约束。"""
    return {
        "id": str(item.id),
        "title": item.title,
        "status": item.status,
        "planted_chapter_index": item.planted_chapter_index,
        "target_chapter_index": item.target_chapter_index,
        "resolved_chapter_index": item.resolved_chapter_index,
        "description": item.description,
        "payload": item.payload,
    }


def _review_issue_to_context(issue: ReviewIssue) -> dict:
    """将未处理审校问题放入上下文，提醒生成器避开已知风险。"""
    return {
        "id": str(issue.id),
        "chapter_id": str(issue.chapter_id) if issue.chapter_id else None,
        "issue_type": issue.issue_type,
        "severity": issue.severity,
        "status": issue.status,
        "message": issue.message,
        "payload": issue.payload,
    }


def build_chapter_context(
    db: Session,
    novel: Novel,
    target_chapter_index: int,
    task_input: dict | None = None,
) -> dict:
    """构建目标章节的完整上下文快照。

    当前策略刻意保留“最近三章全文 + 全局结构化记忆 + 待推进伏笔 + 未关闭审校风险”。
    这样能解决长篇小说超过模型上下文的问题：不用把整本书塞给模型，而是由系统持续维护
    可查询、可追溯、可版本化的生成材料。
    """
    brief = novel.brief or {}

    # 最近三章按倒序取出再反转，既方便数据库查询，也能在上下文里保持阅读顺序。
    recent_chapters = db.scalars(
        select(Chapter)
        .where(
            Chapter.novel_id == novel.id,
            Chapter.chapter_index < target_chapter_index,
        )
        .order_by(Chapter.chapter_index.desc())
        .limit(3)
    ).all()

    # 结构化记忆是长篇连续性的核心：人物状态、地点关系、道具归属都不应只依赖摘要。
    memories = db.scalars(
        select(MemoryItem)
        .where(MemoryItem.novel_id == novel.id)
        .order_by(MemoryItem.updated_at.desc())
        .limit(50)
    ).all()

    # 只给生成器活跃伏笔，已回收伏笔通常不再作为推进目标。
    active_foreshadowing = db.scalars(
        select(Foreshadowing)
        .where(
            Foreshadowing.novel_id == novel.id,
            Foreshadowing.status.in_(["planted", "pending", "active"]),
        )
        .order_by(Foreshadowing.updated_at.desc())
        .limit(30)
    ).all()

    # 审校风险进入生成上下文，便于下一轮生成前主动规避重复问题。
    open_review_issues = db.scalars(
        select(ReviewIssue)
        .where(
            ReviewIssue.novel_id == novel.id,
            ReviewIssue.status == "open",
        )
        .order_by(ReviewIssue.created_at.desc())
        .limit(30)
    ).all()

    recent_chapters_ordered = list(reversed(recent_chapters))

    return {
        "schema_version": "chapter_context.v1",
        "source": "ChapterContextBuilder",
        "novel": {
            "id": str(novel.id),
            "title": novel.title,
            "genre": novel.genre,
            "status": novel.status,
            "premise": novel.premise,
            "target_words": novel.target_words,
            "current_chapter_index": novel.current_chapter_index,
            "brief": brief,
        },
        "target": {
            "chapter_index": target_chapter_index,
            "task_input": task_input or {},
        },
        "recent_chapters": [_chapter_to_context(chapter) for chapter in recent_chapters_ordered],
        "memories": [_memory_to_context(memory) for memory in memories],
        "foreshadowing": [_foreshadowing_to_context(item) for item in active_foreshadowing],
        "review_issues": [_review_issue_to_context(issue) for issue in open_review_issues],
        "constraints": {
            "style_reference": brief.get("style_reference", ""),
            "forbidden_content": brief.get("forbidden_content", ""),
            "automation_strategy": brief.get("automation_strategy", ""),
        },
        "generation_guidance": {
            "chapter_goal": _build_chapter_goal(novel, target_chapter_index, brief),
            "continuity_reminders": _build_continuity_reminders(recent_chapters_ordered),
            "anti_ai_reminders": [
                "避免模板化转折句。",
                "避免解释性独白。",
                "避免用抽象情绪词替代具体行动和细节。",
                "优先保持角色动机、物品状态、地点关系的连续性。",
            ],
        },
        "stats": {
            "recent_chapter_count": len(recent_chapters_ordered),
            "memory_count": len(memories),
            "foreshadowing_count": len(active_foreshadowing),
            "open_review_issue_count": len(open_review_issues),
        },
    }


def _build_chapter_goal(novel: Novel, target_chapter_index: int, brief: dict) -> str:
    """从起始需求中提炼本章生成目标，作为 prompt 的主任务描述。"""
    plot_direction = brief.get("plot_direction") or novel.premise or "推进主线剧情，并保持人物动机连续。"
    return f"生成第 {target_chapter_index} 章草稿，围绕“{plot_direction}”推进剧情。"


def _build_continuity_reminders(recent_chapters: list[Chapter]) -> list[str]:
    """根据最近章节生成连续性提醒，减少前后文断裂。"""
    if not recent_chapters:
        return ["这是作品早期章节，优先建立核心悬念、主角处境和世界规则。"]

    reminders = []
    for chapter in recent_chapters:
        title = chapter.title or f"第 {chapter.chapter_index} 章"
        summary = chapter.summary or "该章暂无摘要，需要从正文中保持连续性。"
        reminders.append(f"承接第 {chapter.chapter_index} 章《{title}》：{summary}")
    return reminders
