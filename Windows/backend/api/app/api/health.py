"""健康检查接口。"""

from fastapi import APIRouter, HTTPException

from app.db.session import check_database


router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health_check() -> dict[str, str]:
    """检查 API 与数据库是否可用，供浏览器、本地脚本或部署平台探活。"""
    try:
        check_database()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"database": "error", "message": str(exc)},
        ) from exc

    return {"status": "ok", "database": "ok"}
