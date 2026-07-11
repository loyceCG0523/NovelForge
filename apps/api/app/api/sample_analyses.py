"""样本分析接口。"""

from uuid import uuid4
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.novel import Novel
from app.models.sample_analysis import SampleAnalysis
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


def _library_rows(db: Session, current_user: User, completed_only: bool) -> list[dict]:
    """按用户读取样本库，并附带可选的源作品名称。"""
    statement = (
        select(SampleAnalysis, Novel.title)
        .outerjoin(Novel, SampleAnalysis.novel_id == Novel.id)
        .where(SampleAnalysis.owner_id == current_user.id)
        .order_by(SampleAnalysis.updated_at.desc())
    )
    if completed_only:
        statement = statement.where(SampleAnalysis.status.in_(["completed", "active"]))

    return [
        {
            **SampleAnalysisRead.model_validate(analysis).model_dump(),
            "source_novel_title": _sample_source_title(novel_title),
        }
        for analysis, novel_title in db.execute(statement).all()
    ]


def _validate_sample_file(file: UploadFile) -> int:
    """校验样本文本文件大小，并返回字节数。"""
    file.file.seek(0, 2)
    file_size = file.file.tell()
    file.file.seek(0)
    if file_size < 200:
        raise HTTPException(status_code=400, detail="样本文本太短，至少需要 200 字符左右的内容")
    return file_size


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
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SampleAnalysis:
    """上传独立样本文本并创建异步分析任务。

    样本分析是用户级样本库资产，不要求先创建小说；后续作品只选择引用这些报告。
    """
    file_size = _validate_sample_file(file)
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
        summary="样本已上传，等待 SampleAnalysisAgent 分片分析。",
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
    remove_object(analysis.source_object_key)
    db.delete(analysis)
    db.commit()


@router.get("", response_model=list[SampleAnalysisRead])
def list_sample_analyses(
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> list[SampleAnalysis]:
    """列出当前作品旧式绑定的样本分析报告；新页面使用独立样本库接口。"""
    return list(
        db.scalars(
            select(SampleAnalysis)
            .where(SampleAnalysis.novel_id == novel.id)
            .order_by(SampleAnalysis.updated_at.desc())
        ).all()
    )


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

    API 只负责保存文件和入队，百万字级样本由 Worker 分片分析并聚合报告。
    """
    file_size = _validate_sample_file(file)
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
        summary="样本已上传，等待 SampleAnalysisAgent 分片分析。",
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
    remove_object(analysis.source_object_key)
    db.delete(analysis)
    db.commit()
