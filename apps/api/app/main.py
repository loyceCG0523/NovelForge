"""FastAPI 应用入口。

这里只负责装配中间件和路由；业务逻辑放在 api/services/models 等模块里。
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.auto_runs import router as auto_runs_router
from app.api.chapters import router as chapters_router
from app.api.dashboard import router as dashboard_router
from app.api.foreshadowing import router as foreshadowing_router
from app.api.health import router as health_router
from app.api.memory import router as memory_router
from app.api.meme_library import router as meme_library_router
from app.api.novels import router as novels_router
from app.api.reviews import router as reviews_router
from app.api.research import router as research_router
from app.api.sample_analyses import library_router as sample_analyses_library_router
from app.api.sample_analyses import router as sample_analyses_router
from app.api.story_bibles import router as story_bibles_router
from app.api.story_events import router as story_events_router
from app.api.tasks import router as tasks_router
from app.api.timeline import router as timeline_router
from app.api.users import router as users_router


app = FastAPI(title="NovelForge API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(auto_runs_router)
app.include_router(users_router)
app.include_router(novels_router)
app.include_router(chapters_router)
app.include_router(memory_router)
app.include_router(meme_library_router)
app.include_router(foreshadowing_router)
app.include_router(reviews_router)
app.include_router(research_router)
app.include_router(sample_analyses_library_router)
app.include_router(sample_analyses_router)
app.include_router(story_bibles_router)
app.include_router(story_events_router)
app.include_router(tasks_router)
app.include_router(timeline_router)
app.include_router(dashboard_router)


@app.get("/")
def root() -> dict[str, str]:
    """API 根路径，用于快速确认服务已启动。"""
    return {"name": "NovelForge API", "status": "ok"}
