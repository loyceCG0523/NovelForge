"""结构化记忆接口。

MemoryItem 保存人物、地点、道具、组织、剧情状态等可查询事实，是长篇连续性的主要支撑。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.memory_item import MemoryItem
from app.models.novel import Novel
from app.schemas.memory import MemoryItemCreate, MemoryItemRead
from app.services.memory_extractor import build_consolidated_memory_context, delete_memory_group


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
) -> list[dict]:
    """列出规范化后的实体级记忆，避免同一人物/道具/事件重复展示。"""
    statement = select(MemoryItem).where(MemoryItem.novel_id == novel.id).order_by(MemoryItem.updated_at.desc())
    consolidated = build_consolidated_memory_context(list(db.scalars(statement).all()), limit=None)
    if memory_type:
        consolidated = [item for item in consolidated if item["memory_type"] == memory_type]
    return consolidated


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def clear_memory_items(
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> None:
    """清空当前作品的全部结构化记忆。"""
    db.execute(delete(MemoryItem).where(MemoryItem.novel_id == novel.id))
    db.commit()


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory_item(
    memory_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> None:
    """删除结构化记忆；如果当前卡片是合并视图，则删除整组来源记录。"""
    deleted_count = delete_memory_group(db=db, novel_id=novel.id, memory_id=memory_id)
    if deleted_count == 0:
        raise HTTPException(status_code=404, detail="Memory item not found")
    db.commit()
