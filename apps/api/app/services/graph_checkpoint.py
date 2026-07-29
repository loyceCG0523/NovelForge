"""数据库持久化的 LangGraph 状态检查点。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.generation_graph_checkpoint import GenerationGraphCheckpoint
from app.models.generation_task import GenerationTask


def load_graph_checkpoint(
    db: Session,
    *,
    task: GenerationTask,
    graph_name: str,
) -> GenerationGraphCheckpoint | None:
    return db.scalar(
        select(GenerationGraphCheckpoint).where(
            GenerationGraphCheckpoint.task_id == task.id,
            GenerationGraphCheckpoint.graph_name == graph_name,
        )
    )


def save_graph_checkpoint(
    db: Session,
    *,
    task: GenerationTask,
    graph_name: str,
    node_name: str,
    state: dict[str, Any],
    status: str = "running",
) -> GenerationGraphCheckpoint:
    checkpoint = load_graph_checkpoint(db, task=task, graph_name=graph_name)
    if checkpoint is None:
        checkpoint = GenerationGraphCheckpoint(
            novel_id=task.novel_id,
            task_id=task.id,
            graph_name=graph_name,
            version=1,
        )
        db.add(checkpoint)
    else:
        checkpoint.version = int(checkpoint.version or 0) + 1
    checkpoint.node_name = node_name
    checkpoint.status = status
    checkpoint.state = state
    db.commit()
    db.refresh(checkpoint)
    return checkpoint
