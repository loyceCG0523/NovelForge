"""NovelForge 后台任务 Worker。

API 负责创建任务并写入 Redis 队列；Worker 独立消费队列并执行耗时的 Agent 工作。
当前版本使用模拟生成逻辑打通闭环，后续接入 LLM/LangGraph 时，优先替换各个
handle_* 函数内部实现，而不是改变任务队列和任务状态协议。
"""

import argparse
import sys
from pathlib import Path
from uuid import UUID

from redis import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import func, select
from sqlalchemy.orm import Session


# Worker 是独立 Python 包，为了复用 API 层的配置、模型和服务，这里把 apps/api 加入导入路径。
API_DIR = Path(__file__).resolve().parents[2] / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.chapter import Chapter  # noqa: E402
from app.models.generation_task import GenerationTask  # noqa: E402
from app.models.memory_item import MemoryItem  # noqa: E402
from app.models.novel import Novel  # noqa: E402
from app.models.review_issue import ReviewIssue  # noqa: E402
from app.services.chapter_context_builder import build_chapter_context  # noqa: E402


def get_redis_client() -> Redis:
    """创建 Redis 客户端；队列名由统一配置读取，避免 API 和 Worker 不一致。"""
    return Redis.from_url(settings.redis_url, decode_responses=True)


def mark_task(
    db: Session,
    task: GenerationTask,
    status: str,
    progress: int,
    result_payload: dict | None = None,
    error_message: str = "",
) -> None:
    """更新任务状态、进度和结果。

    所有任务状态都集中通过这个函数落库，方便后续加审计日志或失败重试。
    """
    task.status = status
    task.progress = progress
    task.error_message = error_message
    if result_payload is not None:
        task.result_payload = result_payload
    db.commit()
    db.refresh(task)


def build_simulated_chapter(context: dict) -> tuple[str, str, str]:
    """基于 ChapterContext 生成模拟章节。

    这里不是最终的小说生成能力，而是为了验证“上下文快照 -> 章节草稿”的数据闭环。
    真正接入 LLM 时，可以把 context 交给 PromptBuilder，再由 LLMClient 返回标题、摘要和正文。
    """
    novel = context["novel"]
    brief = novel.get("brief", {})
    target = context["target"]
    chapter_index = target["chapter_index"]
    recent_chapters = context["recent_chapters"]
    memories = context["memories"]
    foreshadowing = context["foreshadowing"]
    review_issues = context["review_issues"]
    guidance = context["generation_guidance"]

    title = f"第 {chapter_index} 章 灰塔回声"
    summary = (
        f"本章基于《{novel['title']}》的起始需求与上下文快照推进剧情，"
        f"承接最近 {len(recent_chapters)} 章，并参考 {len(memories)} 条结构化记忆。"
    )

    recent_text = "暂无最近章节，当前章节将承担建立主线悬念和世界规则的作用。"
    if recent_chapters:
        recent_text = "；".join(
            f"第 {chapter['chapter_index']} 章《{chapter['title'] or '未命名'}》"
            for chapter in recent_chapters
        )

    memory_text = "暂无结构化记忆。"
    if memories:
        memory_text = "、".join(memory["entity_name"] for memory in memories[:5])

    foreshadowing_text = "暂无待推进伏笔。"
    if foreshadowing:
        foreshadowing_text = "、".join(item["title"] for item in foreshadowing[:5])

    risk_text = "暂无开放风险。"
    if review_issues:
        risk_text = "；".join(issue["message"] for issue in review_issues[:3])

    content = "\n\n".join(
        [
            title,
            f"夜色压在{brief.get('worldview', novel.get('genre') or '灰塔边境城市')}上，像一层没有温度的玻璃。",
            f"本章目标：{guidance['chapter_goal']}",
            f"连续性承接：{recent_text}",
            f"本章需要参考的结构化记忆：{memory_text}",
            f"本章可推进的伏笔：{foreshadowing_text}",
            f"生成前风险提醒：{risk_text}",
            f"{brief.get('protagonist', '主角')}在新的场景中再次面对旧线索。系统没有让他突然获得答案，而是让他通过动作、观察和选择逐步逼近真相。",
            f"剧情继续向“{brief.get('plot_direction', novel.get('premise') or '主线悬念')}”推进，同时遵守风格约束：{context['constraints'].get('style_reference') or '保持克制、具体、少解释'}。",
            "这是基于 ChapterContextBuilder 生成的模拟章节草稿。后续接入 LLM 时，将把同一份上下文快照转换为 prompt，并保留当前快照用于追踪和复盘。",
        ]
    )
    return title, summary, content


def get_target_chapter_index(db: Session, task: GenerationTask, novel: Novel) -> int:
    """确定本次任务要生成或重写哪一章。"""
    if task.chapter_id:
        chapter = db.get(Chapter, task.chapter_id)
        if chapter is None or chapter.novel_id != novel.id:
            raise ValueError("Task chapter does not belong to the novel")
        return chapter.chapter_index

    task_input = (task.result_payload or {}).get("input", {})
    requested_index = task_input.get("chapter_index")
    if requested_index:
        return int(requested_index)

    return (db.scalar(select(func.max(Chapter.chapter_index)).where(Chapter.novel_id == novel.id)) or 0) + 1


def handle_generate_chapter(db: Session, task: GenerationTask, novel: Novel) -> dict:
    """执行章节生成任务，并把上下文快照写回章节记录。"""
    target_chapter_index = get_target_chapter_index(db, task, novel)
    task_input = (task.result_payload or {}).get("input", {})
    context = build_chapter_context(
        db=db,
        novel=novel,
        target_chapter_index=target_chapter_index,
        task_input=task_input,
    )
    title, summary, content = build_simulated_chapter(context)

    chapter = db.get(Chapter, task.chapter_id) if task.chapter_id else None
    # 允许 Worker 幂等运行：如果目标章节已存在，就更新草稿；不存在则创建新章节。
    if chapter is None:
        chapter = db.scalar(
            select(Chapter).where(
                Chapter.novel_id == novel.id,
                Chapter.chapter_index == target_chapter_index,
            )
        )

    if chapter is None:
        chapter = Chapter(
            novel_id=novel.id,
            chapter_index=target_chapter_index,
            title=title,
            status="done",
            summary=summary,
            content=content,
            word_count=len(content),
            context_snapshot=context,
        )
        db.add(chapter)
    else:
        chapter.title = chapter.title or title
        chapter.status = "done"
        chapter.summary = summary
        chapter.content = content
        chapter.word_count = len(content)
        chapter.context_snapshot = context

    novel.current_chapter_index = max(novel.current_chapter_index, chapter.chapter_index)
    db.commit()
    db.refresh(chapter)

    return {
        "chapter_id": str(chapter.id),
        "chapter_index": chapter.chapter_index,
        "title": chapter.title,
        "word_count": chapter.word_count,
        "context_stats": context["stats"],
    }


def handle_plan_novel(novel: Novel) -> dict:
    """模拟整书规划 Agent，后续会替换为真正的大纲规划链路。"""
    brief = novel.brief or {}
    return {
        "plan_type": "simulated_novel_plan",
        "title": novel.title,
        "core_hook": brief.get("selling_points", "核心卖点待补充"),
        "three_act_outline": [
            "第一阶段：建立主角困境、世界规则和核心悬念。",
            "第二阶段：通过连续事件扩大冲突，并让伏笔逐步显影。",
            "第三阶段：集中回收关键伏笔，完成主角选择和主题闭合。",
        ],
    }


def handle_sync_memory(db: Session, novel: Novel) -> dict:
    """模拟结构化记忆同步，后续会从章节正文抽取人物/地点/道具状态。"""
    memory = MemoryItem(
        novel_id=novel.id,
        memory_type="system_note",
        entity_name="模拟记忆同步",
        payload={
            "source": "worker_simulation",
            "summary": "Worker 已模拟抽取一次结构化记忆。后续将替换为章节实体抽取结果。",
        },
    )
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return {"memory_id": str(memory.id), "memory_type": memory.memory_type}


def handle_anti_ai_review(db: Session, task: GenerationTask, novel: Novel) -> dict:
    """模拟反 AI 风格审校，先产出风险记录供工作台展示。"""
    issue = ReviewIssue(
        novel_id=novel.id,
        chapter_id=task.chapter_id,
        issue_type="anti_ai_style",
        severity="medium",
        status="open",
        message="模拟审校：检测到可能存在解释性表达，后续接入真实反 AI 审校规则。",
        payload={"source": "worker_simulation"},
    )
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return {"issue_id": str(issue.id), "issue_type": issue.issue_type}


def execute_task(db: Session, task_id: str) -> None:
    """按任务类型分发到对应处理器，并维护 queued/running/completed/failed 状态。"""
    task = db.get(GenerationTask, UUID(task_id))
    if task is None:
        print(f"Task not found: {task_id}")
        return

    novel = db.get(Novel, task.novel_id)
    if novel is None:
        mark_task(db, task, "failed", 100, error_message="Novel not found")
        return

    mark_task(db, task, "running", 10)

    try:
        if task.task_type == "generate_chapter":
            output = handle_generate_chapter(db, task, novel)
        elif task.task_type == "plan_novel":
            output = handle_plan_novel(novel)
        elif task.task_type == "sync_memory":
            output = handle_sync_memory(db, novel)
        elif task.task_type == "anti_ai_review":
            output = handle_anti_ai_review(db, task, novel)
        else:
            output = {
                "task_type": task.task_type,
                "note": "Unknown task type handled by simulation placeholder.",
            }

        result_payload = {
            **(task.result_payload or {}),
            "agent": "worker_simulation",
            "output": output,
        }
        mark_task(db, task, "completed", 100, result_payload=result_payload)
        print(f"Task completed: {task_id} ({task.task_type})")
    except Exception as exc:
        db.rollback()
        task = db.get(GenerationTask, UUID(task_id))
        if task is not None:
            mark_task(db, task, "failed", 100, error_message=str(exc))
        print(f"Task failed: {task_id} - {exc}")


def consume_once(redis_client: Redis) -> bool:
    """从 Redis 队列消费一个任务；没有任务时返回 False，便于 --once 测试。"""
    try:
        item = redis_client.blpop(settings.agent_task_queue, timeout=5)
    except RedisTimeoutError:
        return False

    if item is None:
        return False

    _, task_id = item
    with SessionLocal() as db:
        execute_task(db, task_id)
    return True


def run_worker(once: bool) -> None:
    """启动 Worker 主循环。"""
    redis_client = get_redis_client()
    print(f"NovelForge worker listening on queue: {settings.agent_task_queue}")

    if once:
        consumed = consume_once(redis_client)
        if not consumed:
            print("No queued task found.")
        return

    while True:
        consume_once(redis_client)


def main() -> None:
    """命令行入口，支持常驻消费或只消费一次。"""
    parser = argparse.ArgumentParser(description="NovelForge worker")
    parser.add_argument("--once", action="store_true", help="Process one queued task and exit.")
    args = parser.parse_args()
    run_worker(once=args.once)


if __name__ == "__main__":
    main()
