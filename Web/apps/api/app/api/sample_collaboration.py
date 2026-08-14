"""公共/私人样本空间与协同标注接口。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.session import SessionLocal, get_db
from app.models.sample_analysis import SampleAnalysis
from app.models.sample_collaboration import (
    SampleAnnotation,
    SampleAnnotationReport,
    SampleAnnotationRevision,
    SampleAnnotationVote,
    SampleCollaborationEvent,
    SampleTextSegment,
)
from app.models.user import User
from app.schemas.sample_analysis import (
    SampleAnalysisLibraryRead,
    SampleAnnotationCategoryRead,
    SampleAnnotationCreate,
    SampleAnnotationNoteBatchRead,
    SampleAnnotationNoteDraftRead,
    SampleAnnotationNoteDraftRequest,
    SampleAnnotationRead,
    SampleAnnotationReportWrite,
    SampleAnnotationUpdate,
    SampleAnnotationVoteWrite,
    SampleChapterIndexRead,
    SamplePublicationUpdate,
    SampleTextSegmentRead,
)
from app.schemas.task import AgentRunRequest
from app.services.agent_orchestrator import enqueue_standalone_agent_task
from app.services.object_storage import read_text_object
from app.services.sample_collaboration import (
    ANNOTATION_CATEGORIES,
    REPORT_REASONS,
    annotation_read_payload,
    annotation_read_payloads,
    build_annotation_hash,
    emit_collaboration_event,
    enforce_collaboration_rate_limit,
    get_accessible_sample,
    load_and_validate_selection,
    load_selection_context,
    publish_collaboration_event,
    recompute_annotation_consensus,
    selection_context_from_content,
    validate_categories,
)
from app.services.sample_annotation_note import (
    AnnotationNoteJob,
    AnnotationNoteGenerationError,
    generate_annotation_note,
    generate_annotation_notes_parallel,
)


router = APIRouter(prefix="/api/sample-collaboration", tags=["sample-collaboration"])
MAX_BATCH_ANNOTATION_NOTES = 40


def _report_for_read(report: dict | None) -> dict:
    report = report or {}
    profile = report.get("reference_profile") or {}
    return {
        "schema_version": report.get("schema_version", "sample_analysis.v5"),
        "stage": report.get("stage", ""),
        "sample": report.get("sample") or {},
        "reference_profile": {
            "available": bool(profile.get("available")),
            "model": str(profile.get("model") or ""),
            "overall_evaluation": str(
                profile.get("overall_evaluation") or profile.get("summary") or ""
            )[:1000],
            "language_principles": list(
                profile.get("language_principles") or profile.get("language_rules") or []
            )[:8],
            "avoid_errors": list(
                profile.get("avoid_errors") or profile.get("anti_ai_rules") or []
            )[:8],
            "reason": str(profile.get("reason") or "")[:500],
        },
    }


def _work_payload(db: Session, analysis: SampleAnalysis) -> dict:
    annotation_count = db.scalar(
        select(func.count(SampleAnnotation.id)).where(
            SampleAnnotation.sample_analysis_id == analysis.id
        )
    ) or 0
    trusted_count = db.scalar(
        select(func.count(SampleAnnotation.id)).where(
            SampleAnnotation.sample_analysis_id == analysis.id,
            SampleAnnotation.status.in_(["trusted", "trusted_private"]),
        )
    ) or 0
    return {
        "id": analysis.id,
        "novel_id": analysis.novel_id,
        "task_id": analysis.task_id,
        "status": analysis.status,
        "sample_title": analysis.sample_title,
        "source_author": analysis.source_author,
        "source_genre": analysis.source_genre,
        "source_file_name": analysis.source_file_name,
        "source_file_size": analysis.source_file_size,
        "source_word_count": analysis.source_word_count,
        "chapter_count": analysis.chapter_count,
        "chunk_count": analysis.chunk_count,
        "analyzed_chunk_count": analysis.analyzed_chunk_count,
        "error_message": analysis.error_message,
        "summary": analysis.summary,
        "metrics": {},
        "report": _report_for_read(analysis.report),
        "visibility": analysis.visibility,
        "publication_status": analysis.publication_status,
        "reuse_policy": analysis.reuse_policy,
        "rights_declared": analysis.rights_declared,
        "version": analysis.version,
        "published_at": analysis.published_at,
        "source_novel_title": "协同样本库",
        "annotation_count": int(annotation_count),
        "trusted_annotation_count": int(trusted_count),
    }


def _enqueue_annotation_index(db: Session, annotation: SampleAnnotation) -> None:
    enqueue_standalone_agent_task(
        db=db,
        payload=AgentRunRequest(
            task_type="index_sample_annotation",
            input_payload={"annotation_id": str(annotation.id)},
        ),
    )


@router.get("/categories", response_model=list[SampleAnnotationCategoryRead])
def list_annotation_categories(
    _current_user: User = Depends(get_current_user),
) -> list[dict]:
    return [{"key": key, "label": label} for key, label in ANNOTATION_CATEGORIES.items()]


@router.get("/works/public")
def list_public_sample_works(
    q: str = Query(default="", max_length=100),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=30, ge=1, le=100),
    _current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    filters = [
        SampleAnalysis.visibility == "public",
        SampleAnalysis.publication_status == "published",
        SampleAnalysis.status.in_(["completed", "active"]),
    ]
    query = q.strip()
    if query:
        pattern = f"%{query}%"
        filters.append(
            or_(
                SampleAnalysis.sample_title.ilike(pattern),
                SampleAnalysis.source_author.ilike(pattern),
                SampleAnalysis.source_genre.ilike(pattern),
            )
        )
    works = db.scalars(
        select(SampleAnalysis)
        .where(*filters)
        .order_by(SampleAnalysis.published_at.desc(), SampleAnalysis.updated_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [_work_payload(db, work) for work in works]


@router.get("/works/{analysis_id}")
def get_sample_work(
    analysis_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    analysis = get_accessible_sample(db, analysis_id, current_user.id)
    return _work_payload(db, analysis)


@router.patch("/works/{analysis_id}/publication")
def update_sample_publication(
    analysis_id: UUID,
    payload: SamplePublicationUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    analysis = get_accessible_sample(db, analysis_id, current_user.id, owner_only=True)
    if analysis.version != payload.version:
        raise HTTPException(status_code=409, detail="样本信息已更新，请刷新后重试")
    if payload.visibility == "public" and not payload.rights_declared:
        raise HTTPException(status_code=400, detail="公开样本前必须确认拥有分享权利")
    previous_visibility = analysis.visibility
    analysis.visibility = payload.visibility
    analysis.publication_status = "published" if payload.visibility == "public" else "private"
    analysis.reuse_policy = payload.reuse_policy
    analysis.rights_declared = payload.rights_declared
    analysis.published_at = datetime.now(UTC) if payload.visibility == "public" else None
    analysis.version += 1
    annotations = db.scalars(
        select(SampleAnnotation).where(SampleAnnotation.sample_analysis_id == analysis.id)
    ).all()
    if previous_visibility != payload.visibility and payload.visibility == "public":
        # 私人标注的可信只代表样本所有者认可，公开后必须重新经过社区共识。
        for annotation in annotations:
            if annotation.status == "trusted_private":
                annotation.status = "pending"
                annotation.trust_score = 0.35
                annotation.embedding = None
                annotation.embedding_model = ""
    elif previous_visibility != payload.visibility:
        # 转为私人后，仅所有者自己的标注进入私人检索库；社区标注保留审计记录。
        for annotation in annotations:
            if annotation.creator_id == current_user.id:
                annotation.status = "trusted_private"
                annotation.trust_score = 1.0
    event = emit_collaboration_event(
        db,
        analysis_id=analysis.id,
        actor_id=current_user.id,
        event_type="publication_updated",
        payload={
            "visibility": analysis.visibility,
            "publication_status": analysis.publication_status,
            "reuse_policy": analysis.reuse_policy,
        },
    )
    db.commit()
    db.refresh(analysis)
    publish_collaboration_event(event)
    return _work_payload(db, analysis)


@router.get("/works/{analysis_id}/segments", response_model=list[SampleTextSegmentRead])
def list_sample_segments(
    analysis_id: UUID,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=1, ge=1, le=10),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    get_accessible_sample(db, analysis_id, current_user.id)
    segments = db.scalars(
        select(SampleTextSegment)
        .where(
            SampleTextSegment.sample_analysis_id == analysis_id,
            SampleTextSegment.sequence_no > after_sequence,
        )
        .order_by(SampleTextSegment.sequence_no)
        .limit(limit)
    ).all()
    return [
        {
            "id": segment.id,
            "sequence_no": segment.sequence_no,
            "title": segment.title,
            "unit_type": segment.unit_type,
            "start_offset": segment.start_offset,
            "end_offset": segment.end_offset,
            "content": read_text_object(segment.source_object_key),
            "content_hash": segment.content_hash,
        }
        for segment in segments
    ]


@router.get(
    "/works/{analysis_id}/chapters/index",
    response_model=list[SampleChapterIndexRead],
)
def list_sample_chapter_index(
    analysis_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """只返回章节目录和标注数，不把整本原文一次性发送到浏览器。"""
    get_accessible_sample(db, analysis_id, current_user.id)
    rows = db.execute(
        select(SampleTextSegment, func.count(SampleAnnotation.id))
        .outerjoin(SampleAnnotation, SampleAnnotation.segment_id == SampleTextSegment.id)
        .where(SampleTextSegment.sample_analysis_id == analysis_id)
        .group_by(SampleTextSegment.id)
        .order_by(SampleTextSegment.sequence_no)
    ).all()
    return [
        {
            "id": chapter.id,
            "sequence_no": chapter.sequence_no,
            "title": chapter.title or f"第 {chapter.sequence_no} 章",
            "unit_type": chapter.unit_type,
            "annotation_count": int(annotation_count or 0),
        }
        for chapter, annotation_count in rows
    ]


@router.get("/works/{analysis_id}/chapters", response_model=list[SampleTextSegmentRead])
def list_sample_chapters(
    analysis_id: UUID,
    sequence_no: int | None = Query(default=None, ge=1),
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=1, ge=1, le=10),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    get_accessible_sample(db, analysis_id, current_user.id)
    filters = [SampleTextSegment.sample_analysis_id == analysis_id]
    if sequence_no is not None:
        filters.append(SampleTextSegment.sequence_no == sequence_no)
    else:
        filters.append(SampleTextSegment.sequence_no > after_sequence)
    chapters = db.scalars(
        select(SampleTextSegment)
        .where(*filters)
        .order_by(SampleTextSegment.sequence_no)
        .limit(1 if sequence_no is not None else limit)
    ).all()
    return [
        {
            "id": chapter.id,
            "sequence_no": chapter.sequence_no,
            "title": chapter.title or f"第 {chapter.sequence_no} 章",
            "unit_type": chapter.unit_type,
            "start_offset": chapter.start_offset,
            "end_offset": chapter.end_offset,
            "content": read_text_object(chapter.source_object_key),
            "content_hash": chapter.content_hash,
        }
        for chapter in chapters
    ]


@router.get("/works/{analysis_id}/annotations", response_model=list[SampleAnnotationRead])
def list_sample_annotations(
    analysis_id: UUID,
    segment_id: UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    analysis = get_accessible_sample(db, analysis_id, current_user.id)
    filters = [SampleAnnotation.sample_analysis_id == analysis.id]
    if segment_id:
        filters.append(SampleAnnotation.segment_id == segment_id)
    if analysis.owner_id != current_user.id:
        filters.append(SampleAnnotation.status != "trusted_private")
    annotations = db.scalars(
        select(SampleAnnotation)
        .where(*filters)
        .order_by(SampleAnnotation.start_offset, SampleAnnotation.created_at)
    ).all()
    return annotation_read_payloads(db, list(annotations), current_user)


@router.post(
    "/works/{analysis_id}/annotations",
    response_model=SampleAnnotationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_sample_annotation(
    analysis_id: UUID,
    payload: SampleAnnotationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    enforce_collaboration_rate_limit(current_user.id, "annotation", limit=30)
    analysis = get_accessible_sample(db, analysis_id, current_user.id)
    segment = db.get(SampleTextSegment, payload.segment_id)
    if segment is None or segment.sample_analysis_id != analysis.id:
        raise HTTPException(status_code=404, detail="原文章节不存在")
    categories = validate_categories(payload.categories)
    selected = load_and_validate_selection(
        segment,
        start_offset=payload.start_offset,
        end_offset=payload.end_offset,
        quote_text=payload.quote_text,
    )
    annotation = SampleAnnotation(
        sample_analysis_id=analysis.id,
        segment_id=segment.id,
        creator_id=current_user.id,
        start_offset=payload.start_offset,
        end_offset=payload.end_offset,
        quote_text=selected,
        categories=categories,
        note=payload.note.strip(),
        status="trusted_private" if analysis.visibility == "private" else "pending",
        trust_score=1.0 if analysis.visibility == "private" else 0.35,
        version=1,
        content_hash=build_annotation_hash(selected, categories, payload.note),
    )
    db.add(annotation)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="你已经标注过这个选区，可以直接修改原标注") from exc
    event = emit_collaboration_event(
        db,
        analysis_id=analysis.id,
        actor_id=current_user.id,
        event_type="annotation_created",
        payload={"annotation_id": str(annotation.id), "segment_id": str(segment.id)},
    )
    db.commit()
    db.refresh(annotation)
    publish_collaboration_event(event)
    if annotation.status == "trusted_private":
        _enqueue_annotation_index(db, annotation)
    return annotation_read_payload(db, annotation, current_user)


@router.post(
    "/works/{analysis_id}/annotation-note-draft",
    response_model=SampleAnnotationNoteDraftRead,
)
def create_annotation_note_draft(
    analysis_id: UUID,
    payload: SampleAnnotationNoteDraftRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SampleAnnotationNoteDraftRead:
    """按人工选区和标签生成可编辑说明，不自动创建或修改标注。"""
    enforce_collaboration_rate_limit(current_user.id, "annotation_note", limit=12)
    analysis = get_accessible_sample(db, analysis_id, current_user.id)
    segment = db.get(SampleTextSegment, payload.segment_id)
    if segment is None or segment.sample_analysis_id != analysis.id:
        raise HTTPException(status_code=404, detail="原文章节不存在")
    categories = validate_categories(payload.categories)
    selected, surrounding = load_selection_context(
        segment,
        start_offset=payload.start_offset,
        end_offset=payload.end_offset,
        quote_text=payload.quote_text,
    )
    try:
        draft = generate_annotation_note(
            preferences=current_user.preferences or {},
            quote_text=selected,
            surrounding_text=surrounding,
            categories=categories,
        )
    except AnnotationNoteGenerationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"说明生成失败：{exc}") from exc
    return SampleAnnotationNoteDraftRead(
        note=draft.note,
        provider=draft.provider,
        model=draft.model,
        used_web_search=draft.used_web_search,
        source_count=draft.source_count,
    )


@router.post(
    "/works/{analysis_id}/chapters/{segment_id}/annotation-notes/batch",
    response_model=SampleAnnotationNoteBatchRead,
)
def batch_create_annotation_notes(
    analysis_id: UUID,
    segment_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SampleAnnotationNoteBatchRead:
    """并行补全本人本章的空说明，绝不覆盖已有或并发写入的说明。"""
    enforce_collaboration_rate_limit(
        current_user.id,
        "annotation_note_batch",
        limit=4,
        window_seconds=60,
    )
    analysis = get_accessible_sample(db, analysis_id, current_user.id)
    segment = db.get(SampleTextSegment, segment_id)
    if segment is None or segment.sample_analysis_id != analysis.id:
        raise HTTPException(status_code=404, detail="原文章节不存在")
    resolved_analysis_id = analysis.id
    resolved_segment_id = segment.id
    analysis_visibility = analysis.visibility
    current_user_id = current_user.id
    preferences = dict(current_user.preferences or {})

    owned_annotations = list(
        db.scalars(
            select(SampleAnnotation)
            .where(
                SampleAnnotation.sample_analysis_id == analysis.id,
                SampleAnnotation.segment_id == resolved_segment_id,
                SampleAnnotation.creator_id == current_user_id,
            )
            .order_by(SampleAnnotation.start_offset, SampleAnnotation.created_at)
        ).all()
    )
    missing_annotations = [
        annotation for annotation in owned_annotations if not annotation.note.strip()
    ]
    candidates = missing_annotations[:MAX_BATCH_ANNOTATION_NOTES]
    remaining_count = max(0, len(missing_annotations) - len(candidates))
    if not candidates:
        return SampleAnnotationNoteBatchRead(
            skipped_existing_count=len(owned_annotations),
        )

    chapter_content = read_text_object(segment.source_object_key)
    jobs: list[AnnotationNoteJob] = []
    snapshot_versions: dict[str, int] = {}
    failures: list[dict] = []
    for annotation in candidates:
        try:
            categories = validate_categories(annotation.categories or [])
            selected, surrounding = selection_context_from_content(
                chapter_content,
                start_offset=annotation.start_offset,
                end_offset=annotation.end_offset,
                quote_text=annotation.quote_text,
            )
        except HTTPException as exc:
            failures.append(
                {"annotation_id": annotation.id, "message": str(exc.detail)}
            )
            continue
        annotation_id = str(annotation.id)
        snapshot_versions[annotation_id] = annotation.version
        jobs.append(
            AnnotationNoteJob(
                annotation_id=annotation_id,
                quote_text=selected,
                surrounding_text=surrounding,
                categories=categories,
            )
        )

    # 模型调用期间释放只读事务和数据库连接；后续逐条重新加锁校验。
    db.rollback()
    results = generate_annotation_notes_parallel(
        preferences=preferences,
        jobs=jobs,
    )
    generated_payloads: list[dict] = []
    skipped_changed_count = 0
    for result in results:
        annotation_uuid = UUID(result.annotation_id)
        if result.draft is None:
            failures.append(
                {
                    "annotation_id": annotation_uuid,
                    "message": result.error or "模型没有返回有效说明",
                }
            )
            continue
        try:
            annotation = db.scalar(
                select(SampleAnnotation)
                .where(
                    SampleAnnotation.id == annotation_uuid,
                    SampleAnnotation.sample_analysis_id == resolved_analysis_id,
                    SampleAnnotation.segment_id == resolved_segment_id,
                    SampleAnnotation.creator_id == current_user_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                annotation is None
                or annotation.version != snapshot_versions[result.annotation_id]
                or annotation.note.strip()
            ):
                db.rollback()
                skipped_changed_count += 1
                continue

            db.add(
                SampleAnnotationRevision(
                    annotation_id=annotation.id,
                    editor_id=current_user_id,
                    revision_no=annotation.version,
                    snapshot={
                        "categories": annotation.categories,
                        "note": annotation.note,
                        "status": annotation.status,
                        "trust_score": annotation.trust_score,
                        "content_hash": annotation.content_hash,
                    },
                )
            )
            annotation.note = result.draft.note
            annotation.version += 1
            annotation.content_hash = build_annotation_hash(
                annotation.quote_text,
                annotation.categories,
                annotation.note,
            )
            annotation.embedding = None
            annotation.embedding_model = ""
            if analysis_visibility == "public":
                annotation.status = "pending"
                annotation.trust_score = 0.35
                db.execute(
                    delete(SampleAnnotationVote).where(
                        SampleAnnotationVote.annotation_id == annotation.id
                    )
                )
                db.execute(
                    delete(SampleAnnotationReport).where(
                        SampleAnnotationReport.annotation_id == annotation.id
                    )
                )
            else:
                annotation.status = "trusted_private"
                annotation.trust_score = 1.0
            db.commit()
            db.refresh(annotation)
            generated_payloads.append(
                annotation_read_payload(db, annotation, current_user)
            )
            if analysis_visibility == "private":
                _enqueue_annotation_index(db, annotation)
        except SQLAlchemyError:
            db.rollback()
            failures.append(
                {
                    "annotation_id": annotation_uuid,
                    "message": "说明已生成，但保存时发生并发冲突，请重试",
                }
            )

    if generated_payloads:
        batch_event = emit_collaboration_event(
            db,
            analysis_id=resolved_analysis_id,
            actor_id=current_user_id,
            event_type="annotation_notes_batch_updated",
            payload={
                "annotation_ids": [str(item["id"]) for item in generated_payloads],
                "generated_count": len(generated_payloads),
            },
        )
        db.commit()
        publish_collaboration_event(batch_event)

    return SampleAnnotationNoteBatchRead(
        generated_count=len(generated_payloads),
        skipped_existing_count=len(owned_annotations) - len(missing_annotations),
        skipped_changed_count=skipped_changed_count,
        failed_count=len(failures),
        remaining_count=remaining_count,
        annotations=generated_payloads,
        failures=failures,
    )


@router.patch("/annotations/{annotation_id}", response_model=SampleAnnotationRead)
def update_sample_annotation(
    annotation_id: UUID,
    payload: SampleAnnotationUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    annotation = db.get(SampleAnnotation, annotation_id)
    if annotation is None or annotation.creator_id != current_user.id:
        raise HTTPException(status_code=404, detail="Annotation not found")
    analysis = get_accessible_sample(db, annotation.sample_analysis_id, current_user.id)
    if annotation.version != payload.version:
        raise HTTPException(status_code=409, detail="标注已被更新，请刷新后重试")
    categories = validate_categories(payload.categories)
    selection_changed = (
        annotation.start_offset != payload.start_offset
        or annotation.end_offset != payload.end_offset
        or annotation.quote_text != payload.quote_text
    )
    if selection_changed:
        segment = db.get(SampleTextSegment, annotation.segment_id)
        if segment is None or segment.sample_analysis_id != analysis.id:
            raise HTTPException(status_code=404, detail="原文章节不存在")
        selected = load_and_validate_selection(
            segment,
            start_offset=payload.start_offset,
            end_offset=payload.end_offset,
            quote_text=payload.quote_text,
        )
    else:
        selected = annotation.quote_text
    db.add(
        SampleAnnotationRevision(
            annotation_id=annotation.id,
            editor_id=current_user.id,
            revision_no=annotation.version,
            snapshot={
                "start_offset": annotation.start_offset,
                "end_offset": annotation.end_offset,
                "quote_text": annotation.quote_text,
                "categories": annotation.categories,
                "note": annotation.note,
                "status": annotation.status,
                "trust_score": annotation.trust_score,
                "content_hash": annotation.content_hash,
            },
        )
    )
    annotation.start_offset = payload.start_offset
    annotation.end_offset = payload.end_offset
    annotation.quote_text = selected
    annotation.categories = categories
    annotation.note = payload.note.strip()
    annotation.version += 1
    annotation.content_hash = build_annotation_hash(
        selected, categories, annotation.note
    )
    annotation.embedding = None
    annotation.embedding_model = ""
    if analysis.visibility == "public":
        annotation.status = "pending"
        annotation.trust_score = 0.35
        db.execute(delete(SampleAnnotationVote).where(SampleAnnotationVote.annotation_id == annotation.id))
        db.execute(delete(SampleAnnotationReport).where(SampleAnnotationReport.annotation_id == annotation.id))
    else:
        annotation.status = "trusted_private"
        annotation.trust_score = 1.0
    event = emit_collaboration_event(
        db,
        analysis_id=analysis.id,
        actor_id=current_user.id,
        event_type="annotation_updated",
        payload={"annotation_id": str(annotation.id), "version": annotation.version},
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="你已经标注过这个选区，请调整范围或修改已有标注",
        ) from exc
    db.refresh(annotation)
    publish_collaboration_event(event)
    if annotation.status == "trusted_private":
        _enqueue_annotation_index(db, annotation)
    return annotation_read_payload(db, annotation, current_user)


@router.delete("/annotations/{annotation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sample_annotation(
    annotation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    annotation = db.get(SampleAnnotation, annotation_id)
    if annotation is None or annotation.creator_id != current_user.id:
        raise HTTPException(status_code=404, detail="Annotation not found")
    analysis_id = annotation.sample_analysis_id
    get_accessible_sample(db, analysis_id, current_user.id)
    db.delete(annotation)
    event = emit_collaboration_event(
        db,
        analysis_id=analysis_id,
        actor_id=current_user.id,
        event_type="annotation_deleted",
        payload={"annotation_id": str(annotation_id)},
    )
    db.commit()
    publish_collaboration_event(event)


@router.put("/annotations/{annotation_id}/vote", response_model=SampleAnnotationRead)
def vote_sample_annotation(
    annotation_id: UUID,
    payload: SampleAnnotationVoteWrite,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    enforce_collaboration_rate_limit(current_user.id, "vote", limit=120)
    annotation = db.get(SampleAnnotation, annotation_id)
    if annotation is None:
        raise HTTPException(status_code=404, detail="Annotation not found")
    analysis = get_accessible_sample(db, annotation.sample_analysis_id, current_user.id)
    if analysis.visibility != "public":
        raise HTTPException(status_code=409, detail="私人标注不参与社区投票")
    if annotation.creator_id == current_user.id:
        raise HTTPException(status_code=400, detail="不能给自己的标注投票")
    vote = db.scalar(
        select(SampleAnnotationVote).where(
            SampleAnnotationVote.annotation_id == annotation.id,
            SampleAnnotationVote.user_id == current_user.id,
        )
    )
    if payload.value == 0:
        if vote:
            db.delete(vote)
    elif vote:
        vote.value = payload.value
    else:
        db.add(
            SampleAnnotationVote(
                annotation_id=annotation.id,
                user_id=current_user.id,
                value=payload.value,
            )
        )
    db.flush()
    needs_index = recompute_annotation_consensus(db, annotation)
    event = emit_collaboration_event(
        db,
        analysis_id=analysis.id,
        actor_id=current_user.id,
        event_type="annotation_voted",
        payload={"annotation_id": str(annotation.id), "status": annotation.status},
    )
    db.commit()
    db.refresh(annotation)
    publish_collaboration_event(event)
    if needs_index:
        _enqueue_annotation_index(db, annotation)
    return annotation_read_payload(db, annotation, current_user)


@router.post("/annotations/{annotation_id}/reports", status_code=status.HTTP_202_ACCEPTED)
def report_sample_annotation(
    annotation_id: UUID,
    payload: SampleAnnotationReportWrite,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    enforce_collaboration_rate_limit(current_user.id, "report", limit=20)
    annotation = db.get(SampleAnnotation, annotation_id)
    if annotation is None:
        raise HTTPException(status_code=404, detail="Annotation not found")
    analysis = get_accessible_sample(db, annotation.sample_analysis_id, current_user.id)
    if analysis.visibility != "public" or annotation.creator_id == current_user.id:
        raise HTTPException(status_code=400, detail="该标注不能由当前用户举报")
    if payload.reason not in REPORT_REASONS:
        raise HTTPException(status_code=400, detail="不支持的举报原因")
    report = db.scalar(
        select(SampleAnnotationReport).where(
            SampleAnnotationReport.annotation_id == annotation.id,
            SampleAnnotationReport.reporter_id == current_user.id,
        )
    )
    if report:
        report.reason = payload.reason
        report.detail = payload.detail.strip()
        report.status = "open"
    else:
        db.add(
            SampleAnnotationReport(
                annotation_id=annotation.id,
                reporter_id=current_user.id,
                reason=payload.reason,
                detail=payload.detail.strip(),
                status="open",
            )
        )
    db.flush()
    recompute_annotation_consensus(db, annotation)
    event = emit_collaboration_event(
        db,
        analysis_id=analysis.id,
        actor_id=current_user.id,
        event_type="annotation_reported",
        payload={"annotation_id": str(annotation.id), "status": annotation.status},
    )
    db.commit()
    publish_collaboration_event(event)
    return {"accepted": True, "annotation_status": annotation.status}


@router.get("/works/{analysis_id}/events")
def stream_sample_collaboration_events(
    analysis_id: UUID,
    after_sequence: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    get_accessible_sample(db, analysis_id, current_user.id)
    subscriber_id = current_user.id

    async def generate():
        cursor = after_sequence
        idle_ticks = 0
        while True:
            with SessionLocal() as event_db:
                analysis = event_db.get(SampleAnalysis, analysis_id)
                can_access = analysis is not None and (
                    analysis.owner_id == subscriber_id
                    or (
                        analysis.visibility == "public"
                        and analysis.publication_status == "published"
                    )
                )
                if not can_access:
                    break
                events = event_db.scalars(
                    select(SampleCollaborationEvent)
                    .where(
                        SampleCollaborationEvent.sample_analysis_id == analysis_id,
                        SampleCollaborationEvent.sequence_no > cursor,
                    )
                    .order_by(SampleCollaborationEvent.sequence_no)
                    .limit(100)
                ).all()
                for event in events:
                    cursor = event.sequence_no
                    data = json.dumps(
                        {
                            "sequence_no": event.sequence_no,
                            "event_type": event.event_type,
                            "payload": event.payload,
                            "created_at": event.created_at.isoformat(),
                        },
                        ensure_ascii=False,
                    )
                    yield f"id: {event.sequence_no}\nevent: {event.event_type}\ndata: {data}\n\n"
            if events:
                idle_ticks = 0
            else:
                idle_ticks += 1
                if idle_ticks >= 15:
                    yield ": heartbeat\n\n"
                    idle_ticks = 0
            await asyncio.sleep(1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
