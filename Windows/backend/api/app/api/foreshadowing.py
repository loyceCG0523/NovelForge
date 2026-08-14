"""伏笔管理接口。

伏笔记录用于追踪“何时埋下、何时推进、何时回收”，避免长篇生成中遗漏重要线索。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.foreshadowing import Foreshadowing
from app.models.novel import Novel
from app.schemas.foreshadowing import ForeshadowingCreate, ForeshadowingRead


router = APIRouter(prefix="/api/novels/{novel_id}/foreshadowing", tags=["foreshadowing"])


@router.post("", response_model=ForeshadowingRead, status_code=status.HTTP_201_CREATED)
def create_foreshadowing(
    payload: ForeshadowingCreate,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> Foreshadowing:
    """创建伏笔记录。"""
    item = Foreshadowing(novel_id=novel.id, **payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.get("", response_model=list[ForeshadowingRead])
def list_foreshadowing(
    status_filter: str | None = None,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> list[Foreshadowing]:
    """列出作品伏笔，可按 planted/pending/resolved 等状态过滤。"""
    statement = select(Foreshadowing).where(Foreshadowing.novel_id == novel.id)
    if status_filter:
        statement = statement.where(Foreshadowing.status == status_filter)
    statement = statement.order_by(Foreshadowing.updated_at.desc())
    return list(db.scalars(statement).all())


@router.patch("/{foreshadowing_id}/status", response_model=ForeshadowingRead)
def update_foreshadowing_status(
    foreshadowing_id: UUID,
    status_value: str,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> Foreshadowing:
    """更新伏笔状态，供工作台或 Agent 标记推进/回收进度。"""
    item = db.get(Foreshadowing, foreshadowing_id)
    if item is None or item.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Foreshadowing not found")
    item.status = status_value
    db.commit()
    db.refresh(item)
    return item
