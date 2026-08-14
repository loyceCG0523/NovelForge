"""API 共享依赖。"""

from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.novel import Novel
from app.models.user import User


def get_owned_novel(
    novel_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Novel:
    """读取作品并校验所有权。

    对外统一返回 404，而不是 403，可以避免暴露“这个作品 ID 是否存在”的信息。
    """
    novel = db.get(Novel, novel_id)
    if novel is None or novel.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Novel not found")
    return novel
