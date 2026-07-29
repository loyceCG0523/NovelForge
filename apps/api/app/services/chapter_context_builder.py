"""章节生成上下文构建器。

这个模块负责把“写第 N 章时必须知道的材料”整理成稳定的结构化快照。
后续无论是模拟生成、LLM prompt，还是 LangGraph 节点之间传递状态，都应优先复用
这里产出的 ChapterContext，避免每个 Agent 各自拼上下文导致遗漏。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.foreshadowing import Foreshadowing
from app.models.memory_item import MemoryItem
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue
from app.models.research_source import ResearchSource
from app.services.agents.story_planning_agent import get_story_bible_context
from app.services.memory_extractor import build_consolidated_memory_context
from app.services.story_bible_builder import get_sample_style_reference_context
from app.services.tavily_search import is_allowed_research_source
from app.services.timeline_service import get_timeline_context

def _chapter_to_context(chapter: Chapter) -> dict:
    """将章节 ORM 对象转成可序列化的上下文片段。"""
    context_snapshot = chapter.context_snapshot or {}
    chapter_progress = context_snapshot.get("chapter_progress") or {}
    return {
        "id": str(chapter.id),
        "chapter_index": chapter.chapter_index,
        "title": chapter.title,
        "status": chapter.status,
        "summary": chapter.summary,
        "content": chapter.content,
        "word_count": chapter.word_count,
        "chapter_progress": chapter_progress,
        "word_guard": context_snapshot.get("word_guard") or {},
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
    chapter_word_range = _build_chapter_word_range(brief)
    story_bible = get_story_bible_context(db, novel)
    timeline_entries = get_timeline_context(db, novel, limit=30)

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
    memory_records = db.scalars(
        select(MemoryItem)
        .where(MemoryItem.novel_id == novel.id)
        .order_by(MemoryItem.updated_at.desc())
        .limit(50)
    ).all()
    merged_memories = build_consolidated_memory_context(memory_records, limit=30)

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

    # 审校记录进入生成上下文，便于下一轮生成前主动规避重复问题。
    # system_deferred 是系统后续自动处理项，不暴露为用户待办，但仍应约束后续生成。
    active_review_issues = db.scalars(
        select(ReviewIssue)
        .where(
            ReviewIssue.novel_id == novel.id,
            ReviewIssue.status.in_(["open", "system_deferred"]),
        )
        .order_by(ReviewIssue.created_at.desc())
        .limit(30)
    ).all()

    # 样本分析只提供可迁移工程特征，不提供原文内容；优先读取作品管理中显式选择的参考样本。
    sample_style_references = get_sample_style_reference_context(db, novel)
    has_event_research_scope = bool(task_input and "research_source_ids" in task_input)
    requested_research_ids = (task_input or {}).get("research_source_ids") or []
    valid_research_ids = []
    for value in requested_research_ids:
        try:
            valid_research_ids.append(UUID(str(value)))
        except (TypeError, ValueError):
            continue
    if has_event_research_scope and not valid_research_ids:
        research_sources = []
    else:
        research_statement = select(ResearchSource).where(ResearchSource.novel_id == novel.id)
        if valid_research_ids:
            research_statement = research_statement.where(ResearchSource.id.in_(valid_research_ids))
        research_candidates = db.scalars(
            research_statement.order_by(ResearchSource.created_at.desc()).limit(24)
        ).all()
        research_sources = [
            item
            for item in research_candidates
            if is_allowed_research_source(
                item.title,
                item.snippet,
                item.published_at,
                item.score,
                item.domain,
            )
        ][:8]

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
        "story_bible": story_bible,
        "target": {
            "chapter_index": target_chapter_index,
            "task_input": task_input or {},
        },
        "recent_chapters": [_chapter_to_context(chapter) for chapter in recent_chapters_ordered],
        "memories": merged_memories,
        "timeline_entries": timeline_entries,
        "foreshadowing": [_foreshadowing_to_context(item) for item in active_foreshadowing],
        "review_issues": [_review_issue_to_context(issue) for issue in active_review_issues],
        "sample_style_references": sample_style_references,
        "research_sources": [
            {
                "query": item.query,
                "title": item.title,
                "url": item.url,
                "domain": item.domain,
                "snippet": item.snippet,
                "published_at": item.published_at,
            }
            for item in research_sources
        ],
        "constraints": {
            "style_reference": brief.get("style_reference", ""),
            "forbidden_content": brief.get("forbidden_content", ""),
            "automation_strategy": brief.get("automation_strategy", ""),
            "story_era": brief.get("story_era", ""),
            "story_location": brief.get("story_location", ""),
            "characters": brief.get("characters", []),
            "planned_events": brief.get("planned_events", []),
            "chapter_word_range": chapter_word_range,
        },
        "generation_guidance": {
            "chapter_goal": _build_chapter_goal(novel, target_chapter_index, brief, task_input or {}),
            "chapter_word_range": chapter_word_range,
            "continuity_reminders": _build_continuity_reminders(recent_chapters_ordered),
        },
        "stats": {
            "recent_chapter_count": len(recent_chapters_ordered),
            "memory_count": len(memory_records),
            "merged_memory_count": len(merged_memories),
            "foreshadowing_count": len(active_foreshadowing),
            "open_review_issue_count": len(active_review_issues),
            "sample_analysis_count": len(sample_style_references),
            "research_source_count": len(research_sources),
            "timeline_entry_count": len(timeline_entries),
        },
    }


def _build_chapter_word_range(brief: dict) -> dict:
    """从起始需求中读取单章字数范围，并做基础纠偏。"""
    min_words = _safe_int(brief.get("chapter_word_min"), 2500)
    max_words = _safe_int(brief.get("chapter_word_max"), 2800)
    min_words = max(500, min_words)
    max_words = max(500, max_words)
    if min_words > max_words:
        min_words, max_words = max_words, min_words
    return {
        "min": min_words,
        "max": max_words,
        "unit": "字",
    }


def _safe_int(value: object, default: int) -> int:
    """将 brief 中的数字型配置转为 int，避免前端字符串影响后续 prompt。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_chapter_goal(novel: Novel, target_chapter_index: int, brief: dict, task_input: dict | None = None) -> str:
    """从起始需求中提炼本章生成目标，作为 prompt 的主任务描述。"""
    chapter_plan = (task_input or {}).get("chapter_plan") or {}
    core_event = str(chapter_plan.get("core_event") or "").strip()
    if core_event:
        return f"生成第 {target_chapter_index} 章草稿，从上一章真实结束状态继续，核心推进：{core_event}"
    plot_direction = brief.get("plot_direction") or novel.premise or "推进主线剧情，并保持人物动机连续。"
    return f"生成第 {target_chapter_index} 章草稿，围绕“{plot_direction}”推进剧情。"


def _build_continuity_reminders(recent_chapters: list[Chapter]) -> list[str]:
    """根据最近章节生成连续性提醒，减少前后文断裂。"""
    if not recent_chapters:
        return ["这是作品早期章节，优先建立核心悬念、主角处境和世界规则。"]

    reminders = []
    for chapter in recent_chapters:
        title = chapter.title or f"第 {chapter.chapter_index} 章"
        progress = (chapter.context_snapshot or {}).get("chapter_progress") or {}
        summary = progress.get("actual_summary") or chapter.summary or "该章暂无摘要，需要从正文中保持连续性。"
        ending_state = progress.get("ending_state") or {}
        ending_note = f"；真实结尾状态：{ending_state}" if ending_state else ""
        reminders.append(f"承接第 {chapter.chapter_index} 章《{title}》：{summary}{ending_note}")
    return reminders
