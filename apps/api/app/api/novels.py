"""作品管理接口。

这里的 Novel 是用户在平台上的“作品项目”，后续章节、记忆、伏笔和任务都挂在它下面。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.novel import Novel
from app.models.user import User
from app.schemas.novel import NovelCreate, NovelRead, NovelUpdate


router = APIRouter(prefix="/api/novels", tags=["novels"])


@router.post("", response_model=NovelRead, status_code=status.HTTP_201_CREATED)
def create_novel(
    payload: NovelCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Novel:
    """创建作品项目，并把当前登录用户记录为所有者。"""
    novel = Novel(owner_id=current_user.id, **payload.model_dump())
    db.add(novel)
    db.commit()
    db.refresh(novel)
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
