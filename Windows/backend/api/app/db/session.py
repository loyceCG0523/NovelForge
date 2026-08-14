"""数据库连接与会话管理。"""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.desktop_compat import configure_sqlite_engine


engine_options = {"pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False, "timeout": 30}
engine = create_engine(settings.database_url, **engine_options)
configure_sqlite_engine(engine)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Session:
    """FastAPI 依赖：为单个请求提供数据库 Session，并在请求结束后关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_database() -> None:
    """健康检查使用的最小数据库探活。"""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
