"""作品管理接口。

这里的 Novel 是用户在平台上的“作品项目”，后续章节、记忆、伏笔和任务都挂在它下面。
"""

from datetime import datetime
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.auto_novel_run import AutoNovelRun
from app.models.chapter import Chapter
from app.models.event_chapter_plan import EventChapterPlan
from app.models.foreshadowing import Foreshadowing
from app.models.generation_task import GenerationTask
from app.models.memory_item import MemoryItem
from app.models.novel import Novel
from app.models.research_source import ResearchSource
from app.models.review_issue import ReviewIssue
from app.models.sample_analysis import SampleAnalysis
from app.models.story_bible import StoryBible
from app.models.story_event import StoryEvent
from app.models.timeline_entry import TimelineEntry
from app.models.user import User
from app.schemas.novel import NovelCreate, NovelDeleteConfirm, NovelRead, NovelUpdate
from app.schemas.task import AgentRunRequest
from app.services.agent_orchestrator import enqueue_agent_task
from app.services.story_bible_builder import build_fallback_story_bible, upsert_story_bible


router = APIRouter(prefix="/api/novels", tags=["novels"])


def _safe_export_filename(
    title: str,
    suffix: str,
    *,
    exported_at: datetime | None = None,
) -> str:
    """生成带分钟级时间戳的安全下载文件名。"""
    safe_title = "".join(char if char not in r'\/:*?"<>|' else "_" for char in (title or "novel")).strip()
    timestamp = (exported_at or datetime.now()).strftime("%Y%m%d_%H%M")
    return f"{safe_title or 'novel'}_{timestamp}.{suffix}"


def _chapter_display_title(chapter: Chapter) -> str:
    """统一导出时的章节标题格式。"""
    title = (chapter.title or "").strip()
    prefix = f"第 {chapter.chapter_index} 章"
    if not title:
        return prefix
    # 兼容“第1章：标题”“第 1 章 标题”等历史存储格式，避免导出时重复章节序号。
    normalized_title = re.sub(
        r"^第\s*\d+\s*章\s*(?:[:：、.\-—]\s*)?",
        "",
        title,
        count=1,
    ).strip()
    return f"{prefix} {normalized_title}" if normalized_title else prefix


def _unique_nonempty(values: list[object]) -> list[str]:
    """按出现顺序去重，供导出元信息汇总不同章节的实际模型。"""
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _build_export_metadata(
    novel: Novel,
    chapters: list[Chapter],
    preferences: dict | None,
    *,
    exported_at: datetime | None = None,
) -> list[tuple[str, str]]:
    """生成脱敏导出信息；优先使用章节实际记录，缺失时回退当前账号设置。"""
    preferences = preferences or {}
    writer_settings = preferences.get("llm") if isinstance(preferences.get("llm"), dict) else {}
    reviewer_settings = (
        preferences.get("review_llm")
        if isinstance(preferences.get("review_llm"), dict)
        else {}
    )
    web_search = (
        preferences.get("web_search")
        if isinstance(preferences.get("web_search"), dict)
        else {}
    )

    recorded_writer_models: list[object] = []
    recorded_reviewer_models: list[object] = []
    for chapter in chapters:
        snapshot = chapter.context_snapshot or {}
        word_guard = snapshot.get("word_guard") if isinstance(snapshot.get("word_guard"), dict) else {}
        review_cycle = (
            snapshot.get("chapter_review_cycle")
            if isinstance(snapshot.get("chapter_review_cycle"), dict)
            else {}
        )
        recorded_writer_models.extend(
            [word_guard.get("model"), review_cycle.get("writer_model")]
        )
        recorded_reviewer_models.append(review_cycle.get("reviewer_model"))

    writer_models = _unique_nonempty(recorded_writer_models)
    if not writer_models:
        writer_models = _unique_nonempty([writer_settings.get("model")])

    reviewer_models = _unique_nonempty(recorded_reviewer_models)
    independent_review = bool(reviewer_settings.get("enabled"))
    if not reviewer_models:
        reviewer_models = _unique_nonempty(
            [
                reviewer_settings.get("model")
                if independent_review
                else (writer_settings.get("model") or (writer_models[0] if writer_models else ""))
            ]
        )

    brief = novel.brief or {}
    chapter_min = str(brief.get("chapter_word_min") or "").strip()
    chapter_max = str(brief.get("chapter_word_max") or "").strip()
    chapter_range = (
        f"{chapter_min}-{chapter_max} 字"
        if chapter_min and chapter_max
        else "未设置"
    )
    metadata = [
        ("正文生成模型", "、".join(writer_models) or "未记录"),
        ("质量审校模型", "、".join(reviewer_models) or "未记录"),
        ("审校配置", "独立审校模型 API" if independent_review else "与正文模型共用 API"),
        ("Tavily 网络检索", "已开启" if web_search.get("enabled") else "未开启"),
        ("单章目标字数", chapter_range),
        ("全书目标字数", f"{novel.target_words:,} 字"),
    ]
    if str(brief.get("story_era") or "").strip():
        metadata.append(("故事年代", str(brief["story_era"]).strip()))
    if str(brief.get("story_location") or "").strip():
        metadata.append(("故事地点", str(brief["story_location"]).strip()))
    metadata.append(("导出时间", (exported_at or datetime.now()).strftime("%Y-%m-%d %H:%M")))
    return metadata


def _build_txt_export(
    novel: Novel,
    chapters: list[Chapter],
    metadata: list[tuple[str, str]] | None = None,
) -> str:
    """导出纯文本版本，适合直接发布、复制或本地阅读。"""
    lines = [novel.title, ""]
    if metadata:
        lines.extend(["【生成信息】"])
        lines.extend(f"{label}：{value}" for label, value in metadata)
        lines.extend(["", "------------------------------", ""])
    if novel.genre:
        lines.extend([f"题材：{novel.genre}", ""])
    if novel.premise:
        lines.extend([novel.premise, ""])

    for chapter in chapters:
        lines.extend([
            _chapter_display_title(chapter),
            "",
            (chapter.content or "").strip(),
            "",
        ])
    return "\n".join(lines).strip() + "\n"


def _build_markdown_export(
    novel: Novel,
    chapters: list[Chapter],
    metadata: list[tuple[str, str]] | None = None,
) -> str:
    """导出 Markdown 版本，保留清晰的书名和章节层级。"""
    lines = [f"# {novel.title}", ""]
    if metadata:
        lines.extend(["## 生成信息", ""])
        lines.extend(f"- {label}：{value}" for label, value in metadata)
        lines.extend(["", "---", ""])
    if novel.genre:
        lines.extend([f"- 题材：{novel.genre}", ""])
    if novel.premise:
        lines.extend([f"> {novel.premise}", ""])

    for chapter in chapters:
        lines.extend([
            f"## {_chapter_display_title(chapter)}",
            "",
            (chapter.content or "").strip(),
            "",
        ])
    return "\n".join(lines).strip() + "\n"


def _validate_delete_confirmation(payload: NovelDeleteConfirm) -> None:
    """校验删除作品确认码，降低误删概率。"""
    if (
        len(payload.expected_code) != 4
        or len(payload.confirmation_code) != 4
        or not payload.expected_code.isdigit()
        or not payload.confirmation_code.isdigit()
        or payload.expected_code != payload.confirmation_code
    ):
        raise HTTPException(status_code=400, detail="删除确认码不正确")


def _delete_novel_graph(db: Session, novel: Novel) -> None:
    """按外键依赖顺序删除作品派生内容，保留可复用样本分析资产。"""
    db.execute(delete(AutoNovelRun).where(AutoNovelRun.novel_id == novel.id))
    db.execute(delete(ResearchSource).where(ResearchSource.novel_id == novel.id))
    db.execute(delete(TimelineEntry).where(TimelineEntry.novel_id == novel.id))
    db.execute(delete(EventChapterPlan).where(EventChapterPlan.novel_id == novel.id))
    db.execute(delete(ReviewIssue).where(ReviewIssue.novel_id == novel.id))
    db.execute(
        update(SampleAnalysis)
        .where(SampleAnalysis.novel_id == novel.id)
        .values(novel_id=None, task_id=None)
    )
    db.execute(delete(StoryEvent).where(StoryEvent.novel_id == novel.id))
    db.execute(delete(MemoryItem).where(MemoryItem.novel_id == novel.id))
    db.execute(delete(Foreshadowing).where(Foreshadowing.novel_id == novel.id))
    db.execute(delete(StoryBible).where(StoryBible.novel_id == novel.id))
    db.execute(delete(GenerationTask).where(GenerationTask.novel_id == novel.id))
    db.execute(delete(Chapter).where(Chapter.novel_id == novel.id))
    db.delete(novel)


@router.post("", response_model=NovelRead, status_code=status.HTTP_201_CREATED)
def create_novel(
    payload: NovelCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Novel:
    """创建作品项目，并自动准备作品圣经。"""
    novel = Novel(owner_id=current_user.id, **payload.model_dump())
    db.add(novel)
    db.commit()
    db.refresh(novel)

    # 先同步写入一份兜底 StoryBible，避免 Worker 尚未执行时后续 Agent 没有全书约束。
    fallback_bible = build_fallback_story_bible(novel)
    upsert_story_bible(db=db, novel=novel, content=fallback_bible, source="auto_fallback_from_create")

    # 再异步触发 StoryBible Agent。若 Redis 暂时不可用，不影响作品创建本身。
    try:
        enqueue_agent_task(
            db=db,
            novel=novel,
            payload=AgentRunRequest(
                task_type="build_story_bible",
                input_payload={"source": "auto_after_create_novel"},
            ),
        )
    except Exception:
        db.rollback()
    return novel


@router.get("", response_model=list[NovelRead])
def list_novels(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Novel]:
    """列出当前用户拥有的作品，避免跨用户暴露作品数据。"""
    statement = (
        select(Novel)
        .where(Novel.owner_id == current_user.id)
        .order_by(Novel.created_at.desc())
    )
    return list(db.scalars(statement).all())


@router.get("/{novel_id}", response_model=NovelRead)
def get_novel(novel: Novel = Depends(get_owned_novel)) -> Novel:
    """读取单个作品；所有权检查由 get_owned_novel 统一完成。"""
    return novel


@router.get("/{novel_id}/export")
def export_novel_content(
    export_format: str = Query("markdown", alias="format", pattern="^(txt|markdown|md)$"),
    novel: Novel = Depends(get_owned_novel),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """导出当前作品的全部章节正文，支持 txt 和 markdown。"""
    chapters = list(
        db.scalars(
            select(Chapter)
            .where(Chapter.novel_id == novel.id)
            .order_by(Chapter.chapter_index.asc())
        ).all()
    )
    exported_at = datetime.now()
    metadata = _build_export_metadata(
        novel,
        chapters,
        current_user.preferences,
        exported_at=exported_at,
    )
    normalized_format = "markdown" if export_format == "md" else export_format
    if normalized_format == "txt":
        content = _build_txt_export(novel, chapters, metadata)
        suffix = "txt"
        media_type = "text/plain; charset=utf-8"
    else:
        content = _build_markdown_export(novel, chapters, metadata)
        suffix = "md"
        media_type = "text/markdown; charset=utf-8"

    filename = _safe_export_filename(novel.title, suffix, exported_at=exported_at)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.patch("/{novel_id}", response_model=NovelRead)
def update_novel(
    payload: NovelUpdate,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> Novel:
    """更新作品基础信息或起始需求 brief。"""
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(novel, key, value)
    db.commit()
    db.refresh(novel)
    return novel


@router.delete("/{novel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_novel(
    payload: NovelDeleteConfirm,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> None:
    """删除作品及其章节、任务、记忆、事件等内容；样本分析保留在独立样本库。"""
    _validate_delete_confirmation(payload)
    _delete_novel_graph(db=db, novel=novel)
    db.commit()
