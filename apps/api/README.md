# NovelForge API

NovelForge API 是长篇小说自动生产系统的业务后端，负责用户认证、作品管理、作品圣经、章节、剧情事件、结构化记忆、样本分析、审校风险和异步任务入队。

当前实现已经不是接口占位层，而是可与前端、PostgreSQL、Redis、MinIO 和 Worker 联动的本地可运行后端。

## 本地启动

先启动基础服务：

```powershell
cd D:\CodeProject\AgentProject\NovelForge
docker compose up -d
```

启动 API：

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

健康检查：

```text
GET /api/health
```

## 技术栈

| 模块 | 当前选型 |
| --- | --- |
| Web API | FastAPI |
| 数据校验 | Pydantic v2 |
| ORM | SQLAlchemy 2 |
| 数据迁移 | Alembic |
| 数据库 | PostgreSQL 16 + pgvector 镜像 |
| 队列 | Redis |
| 对象存储 | MinIO，S3 兼容接口 |
| 鉴权 | JWT + bcrypt |
| LLM 调用 | httpx + OpenAI-compatible Chat Completions |
| Agent 编排 | Worker 侧 LangGraph |

## 主要路由

| 路由文件 | 作用 |
| --- | --- |
| `app/api/auth.py` | 注册、登录、当前用户 |
| `app/api/users.py` | 用户资料、创作偏好、LLM API Key 和自动化策略 |
| `app/api/novels.py` | 作品 CRUD；创建作品后自动入队生成作品圣经 |
| `app/api/story_bibles.py` | 作品圣经读取、保存、刷新任务入口 |
| `app/api/auto_runs.py` | 整本书自动生产的开始、暂停、继续和状态读取 |
| `app/api/story_events.py` | 剧情事件、章节计划、局部重跑、事件级质量审校 |
| `app/api/chapters.py` | 章节创建、查询、编辑、删除；删除章节会同步清理相关记忆和事件计划 |
| `app/api/memory.py` | 结构化记忆查询、删除、清空 |
| `app/api/foreshadowing.py` | 伏笔记录和状态推进 |
| `app/api/reviews.py` | 审校风险和处理状态 |
| `app/api/sample_analyses.py` | 样本文本上传、异步分片分析任务创建、报告查询和删除 |
| `app/api/tasks.py` | 通用任务创建和任务状态查询 |
| `app/api/dashboard.py` | 创作工作台聚合数据 |

## 核心数据模型

| 模型 | 说明 |
| --- | --- |
| `User` | 账号、密码哈希、展示名、偏好配置 |
| `Novel` | 作品基础信息、起始需求文档、当前章节进度 |
| `StoryBible` | 作品圣经，保存世界观、人物、主线、风格约束等结构化设定 |
| `StoryEvent` | 一个闭环剧情事件，通常覆盖 6-12 章 |
| `EventChapterPlan` | 剧情事件下的章节计划和章节生成状态 |
| `Chapter` | 章节正文、摘要、字数、上下文快照 |
| `MemoryItem` | 人物、关系、地点、道具、事件、时间线、世界规则等结构化记忆 |
| `ReviewIssue` | 连续性、质量、事件级审校风险和自动修复历史 |
| `SampleAnalysis` | 样本分析报告、对象存储 key、分片进度、风格向量 |
| `AutoNovelRun` | 整本书自动生产运行状态 |
| `GenerationTask` | Worker 消费的异步任务记录 |

## 异步任务协议

所有耗时任务都通过 `GenerationTask` 入队：

```text
API 创建 generation_tasks
  -> Redis 队列 novelforge:agent_tasks
  -> Worker 消费任务
  -> Worker 更新 status/progress/result_payload
  -> 前端轮询任务或业务聚合接口
```

当前任务类型：

| task_type | 说明 |
| --- | --- |
| `produce_novel` | 整本书自动生产总控 |
| `build_story_bible` | 根据起始需求生成或刷新作品圣经 |
| `generate_story_event` | 规划并生成一个 6-12 章闭环剧情事件 |
| `continue_story_event` | 从某个事件计划继续生成后续章节 |
| `generate_chapter` | 单章生成或重跑 |
| `check_story_event_quality` | 事件级质量审校 |
| `analyze_sample` | 从 MinIO 流式读取样本并分片聚合风格报告 |

## 样本分析架构

样本分析已改为适配百万字级样本的异步架构：

```text
前端上传 TXT/MD
  -> API 保存到 MinIO
  -> API 创建 SampleAnalysis 记录，状态 queued
  -> API 创建 analyze_sample 任务
  -> Worker 流式读取 object key
  -> 按 8000 字左右切片
  -> 对每个 chunk 抽取工程指标
  -> Map-Reduce 聚合为全书报告
  -> 写回 sample_analyses.metrics/report
```

数据库只保存结构化指标和对象存储 key，不把原文直接写入 PostgreSQL。后续章节生成只读取已完成样本报告中的风格向量。

## 章节上下文构建

`app/services/chapter_context_builder.py` 会为章节生成组装：

- 作品基础信息和起始需求文档。
- 作品圣经。
- 目标章节信息。
- 最近 3 章全文与摘要。
- 合并后的结构化记忆。
- 未完成伏笔。
- 开放或系统延后处理的审校风险。
- 已完成样本分析的风格向量。
- 单章字数范围、风格参考、禁忌内容和自动化策略。

上下文快照会保存到 `chapters.context_snapshot`，用于复盘每章生成时模型到底看到了什么。

## LLM 配置

用户在前端设置页填写个人 API Key。后端通过 `app/services/llm_client.py` 读取用户偏好并调用 OpenAI-compatible 接口。

没有配置 API Key 时，Worker 会走模拟生成逻辑，保证本地链路可以跑通。

## 推荐调试顺序

1. `POST /api/auth/register`
2. `POST /api/auth/login`
3. `POST /api/novels`
4. 等待 `build_story_bible` 任务完成。
5. `POST /api/novels/{novel_id}/sample-analyses` 上传样本文本。
6. 启动 Worker，等待 `analyze_sample` 完成。
7. `POST /api/novels/{novel_id}/auto-runs/start`
8. 查询 `/api/novels/{novel_id}/dashboard`
9. 查询 `/api/novels/{novel_id}/story-events`
10. 查询 `/api/novels/{novel_id}/chapters`

## 相关目录

```text
apps/api/app/api/          # HTTP 路由
apps/api/app/models/       # SQLAlchemy 模型
apps/api/app/schemas/      # Pydantic Schema
apps/api/app/services/     # 业务服务、LLM、上下文、样本分析、审校
apps/api/alembic/versions/ # 数据库迁移
```
