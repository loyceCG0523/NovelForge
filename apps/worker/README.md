# NovelForge Worker

Worker 负责消费 Redis 中的 Agent 任务队列，并将执行结果回写 PostgreSQL。

当前阶段先实现模拟 Agent，不调用大模型。它用于跑通完整链路：

```text
前端点击 Agent 任务
  -> FastAPI 创建 generation_tasks
  -> Redis 队列
  -> Worker 消费任务
  -> 回写章节、记忆、风险和任务状态
  -> 工作台 dashboard 展示最新数据
```

## 本地启动

先确保基础服务和后端依赖已经安装：

```powershell
docker compose up -d
conda activate novelforge-api
cd D:\CodeProject\AgentProject\NovelForge\apps\worker
python -m pip install -r requirements.txt
```

常驻启动：

```powershell
python -m worker.main
```

只处理一个任务后退出：

```powershell
python -m worker.main --once
```

## 当前支持任务

- `generate_chapter`: 基于 `ChapterContextBuilder` 组装上下文，模拟生成下一章正文，并写入 `chapters`。
- `plan_novel`: 根据起始需求生成模拟规划结果，写入任务结果。
- `sync_memory`: 创建一条模拟结构化记忆。
- `anti_ai_review`: 创建一条模拟反 AI 审校风险。

后续接入 LangGraph 时，优先替换：

```text
apps/worker/worker/main.py
```

章节生成会把完整上下文保存到：

```text
chapters.context_snapshot
```

当前上下文包含：

- 起始需求文档
- 最近 3 章全文和摘要
- 结构化记忆
- 未完成伏笔
- 开放审校风险
- 风格、禁忌内容和自动化策略
