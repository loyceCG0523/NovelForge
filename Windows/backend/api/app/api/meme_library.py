"""系统内置与用户扩展热梗库接口。"""

from datetime import datetime
from urllib.parse import quote

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.meme_entry import MemeEntry
from app.models.user import User
from app.schemas.meme_library import (
    MemeEntryCreate,
    MemeEntryRead,
    MemeEntryUpdate,
    MemeImportResult,
    MemeIndexResult,
)
from app.services.meme_library import (
    MAX_MEME_IMPORT_BYTES,
    build_meme_library_workbook,
    import_user_meme_entries,
    index_visible_meme_entries,
    normalize_manual_meme_entry,
)


router = APIRouter(prefix="/api/meme-library", tags=["meme-library"])


def can_manage_meme_entry(item: MemeEntry, user_id: UUID) -> bool:
    """内置条目全局可管理，用户条目只能由所属用户管理。"""
    return item.namespace == "builtin" or item.owner_id == user_id


@router.get("", response_model=list[MemeEntryRead])
def list_meme_entries(
    query: str = Query(default="", max_length=120),
    source_type: str = Query(default="all", pattern="^(all|builtin|user)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[MemeEntry]:
    statement = select(MemeEntry).where(
        or_(MemeEntry.namespace == "builtin", MemeEntry.owner_id == current_user.id)
    )
    if source_type != "all":
        statement = statement.where(MemeEntry.source_type == source_type)
    search = query.strip()
    if search:
        statement = statement.where(
            or_(
                MemeEntry.phrase.ilike(f"%{search}%"),
                MemeEntry.meaning.ilike(f"%{search}%"),
                MemeEntry.suitable_scenes.ilike(f"%{search}%"),
            )
        )
    return list(
        db.scalars(
            statement.order_by(
                MemeEntry.source_type.asc(),
                MemeEntry.updated_at.desc(),
            ).limit(1000)
        ).all()
    )


@router.get("/export")
def export_meme_entries(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    entries = list(
        db.scalars(
            select(MemeEntry)
            .where(
                or_(
                    MemeEntry.namespace == "builtin",
                    MemeEntry.owner_id == current_user.id,
                )
            )
            .order_by(MemeEntry.source_type.asc(), MemeEntry.phrase.asc())
        ).all()
    )
    content = build_meme_library_workbook(entries)
    filename = f"热梗知识库_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.post("/import", response_model=MemeImportResult, status_code=status.HTTP_201_CREATED)
async def import_meme_entries(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MemeImportResult:
    content = await file.read(MAX_MEME_IMPORT_BYTES + 1)
    try:
        result = import_user_meme_entries(
            db,
            owner=current_user,
            filename=file.filename or "热梗库.xlsx",
            content=content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    index_status = "skipped"
    indexed = 0
    try:
        index_result = index_visible_meme_entries(db, owner=current_user)
        index_status = index_result["status"]
        indexed = int(index_result["indexed"])
    except Exception as exc:
        db.rollback()
        index_status = "failed"
        result["errors"] = [*result["errors"], f"条目已导入，但向量索引失败：{exc}"][:50]
    return MemeImportResult(
        **result,
        indexed=indexed,
        index_status=index_status,
    )


@router.post("/rebuild-index", response_model=MemeIndexResult)
def rebuild_meme_index(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MemeIndexResult:
    try:
        return MemeIndexResult(**index_visible_meme_entries(db, owner=current_user, force=True))
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"热梗向量索引失败：{exc}") from exc


@router.post("", response_model=MemeEntryRead, status_code=status.HTTP_201_CREATED)
def create_meme_entry(
    payload: MemeEntryCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MemeEntry:
    try:
        values = normalize_manual_meme_entry(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    namespace = f"user:{current_user.id}"
    duplicate = db.scalar(
        select(MemeEntry).where(
            MemeEntry.namespace == namespace,
            MemeEntry.normalized_phrase == values["normalized_phrase"],
        )
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="用户扩展库中已存在同名热梗")
    item = MemeEntry(
        owner_id=current_user.id,
        namespace=namespace,
        source_type="user",
        enabled=payload.enabled,
        review_status="approved",
        library_version="user-manual",
        embedding=None,
        embedding_model="",
        metadata_payload={"source": "manual"},
        **values,
    )
    db.add(item)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="用户扩展库中已存在同名热梗") from exc
    db.refresh(item)
    return item


@router.patch("/{entry_id}", response_model=MemeEntryRead)
def update_meme_entry(
    entry_id: UUID,
    payload: MemeEntryUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MemeEntry:
    item = db.get(MemeEntry, entry_id)
    if item is None or not can_manage_meme_entry(item, current_user.id):
        raise HTTPException(status_code=404, detail="热梗不存在或不可编辑")
    updates = payload.model_dump(exclude_unset=True)
    content_fields = {
        "phrase",
        "meaning",
        "suitable_scenes",
    }
    if content_fields.intersection(updates):
        merged = {
            "phrase": item.phrase,
            "meaning": item.meaning,
            "suitable_scenes": item.suitable_scenes,
            **{
                key: value
                for key, value in updates.items()
                if key in content_fields
            },
        }
        try:
            values = normalize_manual_meme_entry(merged)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        duplicate = db.scalar(
            select(MemeEntry).where(
                MemeEntry.namespace == item.namespace,
                MemeEntry.normalized_phrase == values["normalized_phrase"],
                MemeEntry.id != item.id,
            )
        )
        if duplicate is not None:
            raise HTTPException(status_code=409, detail="当前热梗库中已存在同名热梗")
        for key, value in values.items():
            setattr(item, key, value)
        item.embedding = None
        item.embedding_model = ""
        item.library_version = f"{item.source_type}-manual"
        item.metadata_payload = {
            **(item.metadata_payload or {}),
            "source": "manual",
            "edited_by_user_id": str(current_user.id),
        }
    if "enabled" in updates and updates["enabled"] is not None:
        item.enabled = bool(updates["enabled"])
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="当前热梗库中已存在同名热梗") from exc
    db.refresh(item)
    return item


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_meme_entry(
    entry_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    item = db.get(MemeEntry, entry_id)
    if item is None or not can_manage_meme_entry(item, current_user.id):
        raise HTTPException(status_code=404, detail="热梗不存在或不可删除")
    db.delete(item)
    db.commit()
