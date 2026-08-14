"""样本分析接口。"""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4
from uuid import UUID
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.novel import Novel
from app.models.sample_analysis import SampleAnalysis
from app.models.sample_collaboration import SampleAnnotation, SampleTextSegment
from app.models.user import User
from app.schemas.sample_analysis import SampleAnalysisLibraryRead, SampleAnalysisRead
from app.schemas.task import AgentRunRequest
from app.services.agent_orchestrator import enqueue_agent_task, enqueue_standalone_agent_task
from app.services.object_storage import remove_object, sanitize_filename, upload_fileobj


router = APIRouter(prefix="/api/novels/{novel_id}/sample-analyses", tags=["sample-analyses"])
library_router = APIRouter(prefix="/api/sample-analyses", tags=["sample-analyses"])


def _sample_source_title(title: str | None) -> str:
    """样本可以脱离源作品存在，源作品缺失时展示为独立样本库。"""
    return title or "独立样本库"


def _compact_report_for_read(report: dict | None) -> dict:
    """列表接口不再把历史全量统计报告传给浏览器。"""
    report = report or {}
    profile = report.get("reference_profile") or {}
    if not profile:
        strategy = report.get("llm_style_strategy") or {}
        language_rules = [
            *(strategy.get("dialogue_guidelines") or []),
            *(strategy.get("generation_guidelines") or []),
        ]
        profile = {
            "available": bool(strategy.get("available")),
            "model": strategy.get("model", ""),
            "overall_evaluation": strategy.get("style_summary", ""),
            "language_principles": [str(value)[:220] for value in language_rules[:4]],
            "avoid_errors": [
                str(value)[:220]
                for value in (strategy.get("anti_ai_guidelines") or [])[:3]
            ],
        }
    return {
        "schema_version": report.get("schema_version", "sample_analysis.v5"),
        "stage": report.get("stage", ""),
        "sample": report.get("sample") or {},
        "reference_profile": profile,
    }


def _analysis_read_payload(analysis: SampleAnalysis) -> dict:
    payload = SampleAnalysisRead.model_validate(analysis).model_dump()
    payload["metrics"] = {}
    payload["report"] = _compact_report_for_read(payload.get("report"))
    return payload


def _annotation_index_status(stats: dict) -> str:
    """根据可验证计数生成稳定状态，不再读取旧版 rag_index 报告。"""
    if int(stats.get("annotation_count") or 0) == 0:
        return "unannotated"
    indexed = int(stats.get("indexed_count") or 0)
    pending_index = int(stats.get("pending_index_count") or 0)
    if indexed > 0:
        return "partial" if pending_index > 0 else "ready"
    if pending_index > 0:
        return "pending_index"
    if int(stats.get("pending_review_count") or 0) > 0:
        return "pending_review"
    return "no_trusted_annotations"


def _annotation_index_stats_by_analysis(
    db: Session,
    analysis_ids: list[UUID],
) -> dict[UUID, dict]:
    if not analysis_ids:
        return {}
    trusted = SampleAnnotation.status.in_(["trusted", "trusted_private"])
    indexed = trusted & SampleAnnotation.embedding.is_not(None) & (
        SampleAnnotation.embedding_model != ""
    )
    rows = db.execute(
        select(
            SampleAnnotation.sample_analysis_id,
            func.count(SampleAnnotation.id).label("annotation_count"),
            func.count(SampleAnnotation.id).filter(trusted).label("trusted_count"),
            func.count(SampleAnnotation.id).filter(indexed).label("indexed_count"),
            func.count(SampleAnnotation.id)
            .filter(SampleAnnotation.status == "pending")
            .label("pending_review_count"),
            func.count(SampleAnnotation.id)
            .filter(trusted & ~indexed)
            .label("pending_index_count"),
            func.count(SampleAnnotation.id)
            .filter(SampleAnnotation.status == "quarantined")
            .label("quarantined_count"),
        )
        .where(SampleAnnotation.sample_analysis_id.in_(analysis_ids))
        .group_by(SampleAnnotation.sample_analysis_id)
    ).all()
    result: dict[UUID, dict] = {}
    for row in rows:
        stats = {
            "annotation_count": int(row.annotation_count or 0),
            "trusted_count": int(row.trusted_count or 0),
            "indexed_count": int(row.indexed_count or 0),
            "pending_review_count": int(row.pending_review_count or 0),
            "pending_index_count": int(row.pending_index_count or 0),
            "quarantined_count": int(row.quarantined_count or 0),
        }
        result[row.sample_analysis_id] = {
            **stats,
            "status": _annotation_index_status(stats),
        }
    return result


def _library_rows(db: Session, current_user: User, completed_only: bool) -> list[dict]:
    """按用户读取样本库，并附带可选的源作品名称。"""
    statement = (
        select(SampleAnalysis, Novel.title)
        .outerjoin(Novel, SampleAnalysis.novel_id == Novel.id)
        .where(
            SampleAnalysis.owner_id == current_user.id,
            SampleAnalysis.status != "removed",
        )
        .order_by(SampleAnalysis.updated_at.desc())
    )
    if completed_only:
        statement = statement.where(SampleAnalysis.status.in_(["completed", "active"]))

    rows = db.execute(statement).all()
    index_stats = _annotation_index_stats_by_analysis(
        db,
        [analysis.id for analysis, _novel_title in rows],
    )
    return [
        {
            **_analysis_read_payload(analysis),
            "source_novel_title": _sample_source_title(novel_title),
            "annotation_index": index_stats.get(
                analysis.id,
                {
                    "status": "unannotated",
                    "annotation_count": 0,
                    "trusted_count": 0,
                    "indexed_count": 0,
                    "pending_review_count": 0,
                    "pending_index_count": 0,
                    "quarantined_count": 0,
                },
            ),
        }
        for analysis, novel_title in rows
    ]


def _validate_sample_file(file: UploadFile) -> int:
    """校验样本文本文件大小，并返回字节数。"""
    file.file.seek(0, 2)
    file_size = file.file.tell()
    file.file.seek(0)
    if file_size < 200:
        raise HTTPException(status_code=400, detail="样本文本太短，至少需要 200 字符左右的内容")
    if file_size > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="单个样本文本不能超过50MB")
    suffix = (file.filename or "").lower().rsplit(".", 1)[-1]
    if suffix not in {"txt", "md", "text"}:
        raise HTTPException(status_code=400, detail="只支持TXT或MD文本文件")
    return file_size


def _file_hash(file: UploadFile) -> str:
    digest = hashlib.sha256()
    while chunk := file.file.read(1024 * 1024):
        digest.update(chunk)
    file.file.seek(0)
    return digest.hexdigest()


def _remove_sample_objects(db: Session, analysis: SampleAnalysis) -> None:
    remove_object(analysis.source_object_key)
    segments = db.scalars(
        select(SampleTextSegment).where(
            SampleTextSegment.sample_analysis_id == analysis.id
        )
    ).all()
    for segment in segments:
        remove_object(segment.source_object_key)


@library_router.get("/library", response_model=list[SampleAnalysisLibraryRead])
def list_sample_analysis_library(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """列出当前用户可复用的已完成样本报告，供作品管理页选择引用。"""
    return _library_rows(db=db, current_user=current_user, completed_only=True)


@library_router.get("", response_model=list[SampleAnalysisLibraryRead])
def list_standalone_sample_analyses(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """列出当前用户的全部样本分析记录，样本库不依赖任何作品存在。"""
    return _library_rows(db=db, current_user=current_user, completed_only=False)


@library_router.post("", response_model=SampleAnalysisRead, status_code=status.HTTP_202_ACCEPTED)
async def create_standalone_sample_analysis(
    sample_title: str = Form(...),
    source_author: str = Form(""),
    source_genre: str = Form(""),
    visibility: str = Form("private"),
    reuse_policy: str = Form("reference_only"),
    rights_declared: bool = Form(False),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SampleAnalysis:
    """上传独立样本文本并创建异步分析任务。

    样本分析是用户级样本库资产，不要求先创建小说；后续作品只选择引用这些报告。
    """
    file_size = _validate_sample_file(file)
    if visibility not in {"private", "public"}:
        raise HTTPException(status_code=400, detail="样本可见性不受支持")
    if reuse_policy not in {"reference_only", "excerpt_reuse"}:
        raise HTTPException(status_code=400, detail="复用许可不受支持")
    if visibility == "public" and not rights_declared:
        raise HTTPException(status_code=400, detail="公开样本前必须确认拥有分享权利")
    content_hash = _file_hash(file)
    analysis_id = uuid4()
    safe_name = sanitize_filename(file.filename or f"{sample_title}.txt")
    object_key = f"users/{current_user.id}/sample-analyses/{analysis_id}/{safe_name}"
    upload_fileobj(
        object_name=object_key,
        file_obj=file.file,
        length=file_size,
        content_type=file.content_type or "text/plain",
    )

    analysis = SampleAnalysis(
        id=analysis_id,
        owner_id=current_user.id,
        novel_id=None,
        status="queued",
        sample_title=sample_title,
        source_author=source_author,
        source_genre=source_genre,
        source_file_name=safe_name,
        source_object_key=object_key,
        source_file_size=file_size,
        summary="样本已上传，等待生成总体评价、语言表达原则和应避免错误。",
        visibility=visibility,
        publication_status="published" if visibility == "public" else "private",
        reuse_policy=reuse_policy,
        rights_declared=rights_declared,
        content_hash=content_hash,
        published_at=datetime.now(UTC) if visibility == "public" else None,
        report={
            "stage": "queued",
            "sample": {"title": sample_title, "genre": source_genre},
        },
    )
    db.add(analysis)
    db.commit()
    db.refresh(analysis)

    task = enqueue_standalone_agent_task(
        db=db,
        payload=AgentRunRequest(
            task_type="analyze_sample",
            input_payload={
                "analysis_id": str(analysis.id),
                "object_key": object_key,
                "source_file_name": safe_name,
            },
        ),
    )
    analysis.task_id = task.id
    analysis.report = {
        **(analysis.report or {}),
        "task_id": str(task.id),
    }
    db.commit()
    db.refresh(analysis)
    return analysis


@library_router.delete("/{analysis_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_standalone_sample_analysis(
    analysis_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """从用户样本库删除一份样本分析报告。"""
    analysis = db.get(SampleAnalysis, analysis_id)
    if analysis is None or analysis.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Sample analysis not found")
    community_annotations = db.scalar(
        select(func.count(SampleAnnotation.id)).where(
            SampleAnnotation.sample_analysis_id == analysis.id,
            SampleAnnotation.creator_id != current_user.id,
        )
    ) or 0
    if analysis.visibility == "public" and community_annotations:
        analysis.visibility = "private"
        analysis.publication_status = "removed"
        analysis.status = "removed"
        analysis.version += 1
        db.commit()
        return
    _remove_sample_objects(db, analysis)
    db.delete(analysis)
    db.commit()


@library_router.get("/{analysis_id}/experience-document")
def download_sample_experience_document(
    analysis_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    """下载大模型总结生成的 Markdown 创作经验文档。"""
    analysis = db.get(SampleAnalysis, analysis_id)
    if analysis is None or analysis.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Sample analysis not found")
    document = (analysis.report or {}).get("experience_document") or {}
    markdown = str(document.get("markdown") or "").strip()
    if not markdown:
        raise HTTPException(status_code=409, detail="该样本尚未生成经验文档，请先更新分析")
    safe_title = sanitize_filename(analysis.sample_title or "样本")
    filename = f"{safe_title}-创作经验文档.md"
    return PlainTextResponse(
        markdown + "\n",
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@library_router.post(
    "/{analysis_id}/reindex",
    response_model=SampleAnalysisRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def reindex_standalone_sample_analysis(
    analysis_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SampleAnalysis:
    """重新生成三项总体分析，并把旧阅读分段安全转换为章节。"""
    analysis = db.get(SampleAnalysis, analysis_id)
    if analysis is None or analysis.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Sample analysis not found")
    if analysis.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="该样本已有分析任务正在运行")

    task = enqueue_standalone_agent_task(
        db=db,
        payload=AgentRunRequest(
            task_type="analyze_sample",
            input_payload={
                "analysis_id": str(analysis.id),
                "object_key": analysis.source_object_key,
                "source_file_name": analysis.source_file_name,
                "reindex": True,
            },
        ),
    )
    analysis.task_id = task.id
    analysis.status = "queued"
    analysis.error_message = ""
    analysis.summary = "已进入队列，准备重新生成三项总体分析与章节目录。"
    analysis.report = {
        **(analysis.report or {}),
        "stage": "queued",
        "task_id": str(task.id),
    }
    db.commit()
    db.refresh(analysis)
    return analysis


@router.get("", response_model=list[SampleAnalysisRead])
def list_sample_analyses(
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> list[dict]:
    """列出当前作品旧式绑定的样本分析报告；新页面使用独立样本库接口。"""
    return [
        _analysis_read_payload(analysis)
        for analysis in db.scalars(
            select(SampleAnalysis)
            .where(SampleAnalysis.novel_id == novel.id)
            .order_by(SampleAnalysis.updated_at.desc())
        ).all()
    ]


@router.post("", response_model=SampleAnalysisRead, status_code=status.HTTP_202_ACCEPTED)
async def create_sample_analysis(
    sample_title: str = Form(...),
    source_author: str = Form(""),
    source_genre: str = Form(""),
    file: UploadFile = File(...),
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> SampleAnalysis:
    """上传样本文本并创建异步分析任务。

    API 只负责保存文件和入队，长篇样本由 Worker 最多十路并行总结经验。
    """
    file_size = _validate_sample_file(file)
    content_hash = _file_hash(file)
    analysis_id = uuid4()
    safe_name = sanitize_filename(file.filename or f"{sample_title}.txt")
    object_key = f"users/{novel.owner_id}/sample-analyses/{analysis_id}/{safe_name}"
    upload_fileobj(
        object_name=object_key,
        file_obj=file.file,
        length=file_size,
        content_type=file.content_type or "text/plain",
    )

    analysis = SampleAnalysis(
        id=analysis_id,
        owner_id=novel.owner_id,
        novel_id=novel.id,
        status="queued",
        sample_title=sample_title,
        source_author=source_author,
        source_genre=source_genre or novel.genre,
        source_file_name=safe_name,
        source_object_key=object_key,
        source_file_size=file_size,
        summary="样本已上传，等待生成总体评价、语言表达原则和应避免错误。",
        content_hash=content_hash,
        report={
            "stage": "queued",
            "sample": {"title": sample_title, "genre": source_genre or novel.genre},
        },
    )
    db.add(analysis)
    db.commit()
    db.refresh(analysis)

    task = enqueue_agent_task(
        db=db,
        novel=novel,
        payload=AgentRunRequest(
            task_type="analyze_sample",
            input_payload={
                "analysis_id": str(analysis.id),
                "object_key": object_key,
                "source_file_name": safe_name,
            },
        ),
    )
    analysis.task_id = task.id
    analysis.report = {
        **(analysis.report or {}),
        "task_id": str(task.id),
    }
    db.commit()
    db.refresh(analysis)
    return analysis


@router.delete("/{analysis_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sample_analysis(
    analysis_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> None:
    """删除一份样本分析报告。"""
    analysis = db.get(SampleAnalysis, analysis_id)
    if analysis is None or analysis.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Sample analysis not found")
    _remove_sample_objects(db, analysis)
    db.delete(analysis)
    db.commit()
