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
from app.services.agents.story_planning_agent import get_story_bible_context
from app.services.memory_extractor import build_consolidated_memory_context
from app.services.story_bible_builder import get_sample_style_reference_context

ROLE_PREFIXES = ("男主", "女主", "主角", "男生", "女生", "同桌", "班长", "学生")


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


def _canonical_entity_name(entity_name: str) -> str:
    """把“女主许清禾”这类带角色标签的实体名归一成稳定名称。"""
    name = (entity_name or "").strip()
    for prefix in ROLE_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix):].strip(" ：:，,")
    return name


def _memory_importance_score(memory: MemoryItem) -> int:
    """将重要性转成排序分数，便于在合并时保留更关键的事实。"""
    importance = str((memory.payload or {}).get("importance") or "").lower()
    return {"high": 3, "medium": 2, "low": 1}.get(importance, 0)


def _build_merged_memory_context(memories: list[MemoryItem]) -> list[dict]:
    """按类型和实体名合并记忆，避免 prompt 中出现大量重复事实。

    数据库仍保留逐章抽取记录，方便回溯；进入模型输入时只给实体级状态摘要和少量来源。
    """
    grouped: dict[tuple[str, str], list[MemoryItem]] = {}
    for memory in memories:
        canonical_name = _canonical_entity_name(memory.entity_name)
        if not canonical_name:
            continue
        grouped.setdefault((memory.memory_type, canonical_name), []).append(memory)

    merged_memories = []
    for (memory_type, canonical_name), items in grouped.items():
        ordered_items = sorted(
            items,
            key=lambda item: (item.chapter_index_end or item.chapter_index_start or 0, item.updated_at),
            reverse=True,
        )
        latest = ordered_items[0]
        summaries = []
        statuses = []
        source_ids = []
        source_chapters = []
        for item in ordered_items[:5]:
            payload = item.payload or {}
            summary = payload.get("summary") or payload.get("status") or payload.get("evidence")
            if summary and summary not in summaries:
                summaries.append(summary)
            status = payload.get("status")
            if status and status not in statuses:
                statuses.append(status)
            source_ids.append(str(item.id))
            if item.chapter_index_start:
                source_chapters.append(item.chapter_index_start)

        chapter_indices = [
            index
            for item in ordered_items
            for index in (item.chapter_index_start, item.chapter_index_end)
            if index is not None
        ]
        importance = max((_memory_importance_score(item) for item in ordered_items), default=0)
        importance_label = {3: "high", 2: "medium", 1: "low"}.get(importance, "")

        merged_memories.append(
            {
                "memory_type": memory_type,
                "entity_name": canonical_name,
                "chapter_index_start": min(chapter_indices) if chapter_indices else None,
                "chapter_index_end": max(chapter_indices) if chapter_indices else None,
                "source_memory_count": len(ordered_items),
                "source_memory_ids": source_ids,
                "payload": {
                    "summary": summaries[0] if summaries else "",
                    "recent_facts": summaries[:5],
                    "current_status": statuses[0] if statuses else "",
                    "importance": importance_label,
                    "source_chapters": sorted(set(source_chapters)),
                },
            }
        )

    return sorted(
        merged_memories,
        key=lambda item: (
            _importance_sort_value(item["payload"].get("importance")),
            item["chapter_index_end"] or 0,
        ),
        reverse=True,
    )[:30]


def _importance_sort_value(importance: str | None) -> int:
    """供合并后的记忆排序使用。"""
    return {"high": 3, "medium": 2, "low": 1}.get(str(importance or "").lower(), 0)


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
        "foreshadowing": [_foreshadowing_to_context(item) for item in active_foreshadowing],
        "review_issues": [_review_issue_to_context(issue) for issue in active_review_issues],
        "sample_style_references": sample_style_references,
        "constraints": {
            "style_reference": brief.get("style_reference", ""),
            "forbidden_content": brief.get("forbidden_content", ""),
            "automation_strategy": brief.get("automation_strategy", ""),
            "chapter_word_range": chapter_word_range,
            "story_bible": story_bible.get("content", {}),
            "sample_style_vectors": [item.get("transferable_style_vector", {}) for item in sample_style_references],
        },
        "generation_guidance": {
            "chapter_goal": _build_chapter_goal(novel, target_chapter_index, brief),
            "chapter_word_range": chapter_word_range,
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
            "memory_count": len(memory_records),
            "merged_memory_count": len(merged_memories),
            "foreshadowing_count": len(active_foreshadowing),
            "open_review_issue_count": len(active_review_issues),
            "sample_analysis_count": len(sample_style_references),
        },
    }


def _build_chapter_word_range(brief: dict) -> dict:
    """从起始需求中读取单章字数范围，并做基础纠偏。"""
    min_words = _safe_int(brief.get("chapter_word_min"), 2000)
    max_words = _safe_int(brief.get("chapter_word_max"), 3000)
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
