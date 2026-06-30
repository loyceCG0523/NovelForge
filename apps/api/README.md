# NovelForge API

NovelForge 后端基础框架，当前覆盖用户认证、作品管理、章节管理、结构化记忆、伏笔、审校风险、生成任务和 Agent 任务预留入口。

## 本地启动

```powershell
conda activate novelforge-api
cd D:\CodeProject\AgentProject\NovelForge\apps\api
python -m pip install -r requirements.txt
python -m alembic upgrade head
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

接口文档：

```text
http://127.0.0.1:8000/docs
```

## 主要模块

- `app/api/auth.py`: 注册、登录、当前用户。
- `app/api/novels.py`: 当前用户的作品管理。
- `app/api/chapters.py`: 章节创建与查询。
- `app/api/memory.py`: 结构化记忆。
- `app/api/foreshadowing.py`: 伏笔记录与状态推进。
- `app/api/reviews.py`: 审校风险与处理状态。
- `app/api/tasks.py`: 生成任务与 Agent 任务入口。
- `app/api/dashboard.py`: 创作工作台聚合数据。
- `app/services/agent_orchestrator.py`: 智能体工作流预留层。

## 推荐测试顺序

1. `POST /api/auth/register`
2. 在 `/docs` 右上角 `Authorize` 登录。
3. `POST /api/novels`
4. `POST /api/novels/{novel_id}/chapters`
5. `POST /api/novels/{novel_id}/memory`
6. `POST /api/novels/{novel_id}/foreshadowing`
7. `POST /api/novels/{novel_id}/reviews`
8. `POST /api/novels/{novel_id}/tasks/agent-runs`
9. `GET /api/novels/{novel_id}/dashboard`

## Agent 预留说明

当前 `/api/novels/{novel_id}/tasks/agent-runs` 会创建一条 `generation_tasks` 记录，状态为 `queued`。

后续接入 LangGraph/Celery 时，优先改造：

```text
app/services/agent_orchestrator.py
```

预期演进方向：

- 接收章节生成、记忆同步、伏笔审查、反 AI 审校等任务。
- 将任务写入 Redis/Celery 队列。
- Worker 执行 LangGraph 工作流。
- 执行结果回写 `generation_tasks`、`chapters`、`memory_items`、`review_issues`。
