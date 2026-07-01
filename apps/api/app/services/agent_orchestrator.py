"""Agent 任务编排服务。

API 层只负责接收用户操作；真正耗时的生成任务通过这里写入数据库并推送到 Redis 队列。
"""

from sqlalchemy.orm import Session
from redis import Redis

from app.core.config import settings
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.schemas.task import AgentRunRequest


def push_task_to_queue(task_id: str) -> None:
    """把任务 ID 推入 Redis 队列，等待 Worker 消费。"""
    redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    redis_client.rpush(settings.agent_task_queue, task_id)


def enqueue_agent_task(db: Session, novel: Novel, payload: AgentRunRequest) -> GenerationTask:
    """创建任务记录并入队，返回可供前端轮询的任务实体。"""
    task = GenerationTask(
        novel_id=novel.id,
        chapter_id=payload.chapter_id,
        task_type=payload.task_type,
        status="queued",
        progress=0,
        result_payload={
            "input": payload.input_payload,
            "agent": "reserved",
            "note": "Agent workflow placeholder. Connect LangGraph/Celery here.",
        },
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    push_task_to_queue(str(task.id))
    return task
