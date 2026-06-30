from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.chapters import router as chapters_router
from app.api.dashboard import router as dashboard_router
from app.api.foreshadowing import router as foreshadowing_router
from app.api.health import router as health_router
from app.api.memory import router as memory_router
from app.api.novels import router as novels_router
from app.api.reviews import router as reviews_router
from app.api.tasks import router as tasks_router
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
)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(novels_router)
app.include_router(chapters_router)
app.include_router(memory_router)
app.include_router(foreshadowing_router)
app.include_router(reviews_router)
app.include_router(tasks_router)
app.include_router(dashboard_router)


@app.get("/")
def root() -> dict[str, str]:
    return {"name": "NovelForge API", "status": "ok"}
