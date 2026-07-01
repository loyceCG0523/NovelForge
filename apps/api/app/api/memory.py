"""结构化记忆接口。

MemoryItem 保存人物、地点、道具、组织、剧情状态等可查询事实，是长篇连续性的主要支撑。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.memory_item import MemoryItem
from app.models.novel import Novel
from app.schemas.memory import MemoryItemCreate, MemoryItemRead


router = APIRouter(prefix="/api/novels/{novel_id}/memory", tags=["memory"])


@router.post("", response_model=MemoryItemRead, status_code=status.HTTP_201_CREATED)
def create_memory_item(
    payload: MemoryItemCreate,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> MemoryItem:
    """新增一条结构化记忆。"""
    memory_item = MemoryItem(novel_id=novel.id, **payload.model_dump())
    db.add(memory_item)
    db.commit()
    db.refresh(memory_item)
    return memory_item


@router.get("", response_model=list[MemoryItemRead])
def list_memory_items(
    memory_type: str | None = None,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> list[MemoryItem]:
    """按更新时间倒序列出记忆，可按类型过滤。"""
    statement = select(MemoryItem).where(MemoryItem.novel_id == novel.id)
    if memory_type:
        statement = statement.where(MemoryItem.memory_type == memory_type)
    statement = statement.order_by(MemoryItem.updated_at.desc())
    return list(db.scalars(statement).all())


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory_item(
    memory_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> None:
    """删除结构化记忆。"""
    memory_item = db.get(MemoryItem, memory_id)
    if memory_item is None or memory_item.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Memory item not found")
    db.delete(memory_item)
    db.commit()
