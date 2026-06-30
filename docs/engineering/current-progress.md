# NovelForge 当前进度与系统功能说明

更新时间：2026-07-01

## 1. 当前项目状态

NovelForge 目前已经完成从 0 到本地可运行框架的搭建，具备前端、后端、数据库和基础服务的完整开发闭环。

当前系统已经不是单纯原型，而是一个可以运行、可以注册登录、可以创建作品、可以维护章节、可以展示工作台真实数据的基础产品框架。

## 2. 已完成的基础设施

### 本地基础服务

已通过 Docker Compose 启动：

- PostgreSQL + pgvector
- Redis
- MinIO

其中 PostgreSQL 已经接入 FastAPI 后端，当前健康检查结果可返回：

```json
{"status":"ok","database":"ok"}
```

### 后端运行环境

后端使用独立 conda 环境：

```text
novelforge-api
```

后端技术栈：

- FastAPI
- SQLAlchemy
- Alembic
- PostgreSQL / pgvector
- JWT
- bcrypt

### 前端运行环境

前端位于：

```text
apps/web
```

前端技术栈：

- Next.js
- React
- 原生 CSS

视觉风格参考：

```text
docs/product/ui-prototypes
```

## 3. 后端已完成功能

后端位于：

```text
apps/api
```

### 认证模块

已支持：

- 用户注册
- 用户登录
- JWT Token 签发
- 获取当前用户
- 密码 bcrypt 哈希保存

主要接口：

```text
POST /api/auth/register
POST /api/auth/login
POST /api/auth/token
GET  /api/auth/me
```

### 作品模块

已支持：

- 创建作品
- 查询当前用户作品列表
- 查询作品详情
- 更新作品信息
- 保存起始需求文档

起始需求文档保存在 `novels.brief` JSON 字段中，包含：

- 作品类型
- 卖点与读者期待
- 主角设定
- 世界观与核心规则
- 剧情方向
- 风格参考
- 禁忌内容
- 自动化策略

主要接口：

```text
POST  /api/novels
GET   /api/novels
GET   /api/novels/{novel_id}
PATCH /api/novels/{novel_id}
```

### 章节模块

已支持：

- 创建章节
- 查询章节列表
- 查询章节详情
- 更新章节
- 删除章节
- 自动统计章节字数

章节字段包括：

- 章节序号
- 标题
- 状态
- 摘要
- 正文
- 上下文快照
- 字数

主要接口：

```text
POST   /api/novels/{novel_id}/chapters
GET    /api/novels/{novel_id}/chapters
GET    /api/novels/{novel_id}/chapters/{chapter_id}
PATCH  /api/novels/{novel_id}/chapters/{chapter_id}
DELETE /api/novels/{novel_id}/chapters/{chapter_id}
```

### 结构化记忆模块

已支持基础数据接口：

- 创建结构化记忆
- 查询结构化记忆
- 删除结构化记忆

主要接口：

```text
POST   /api/novels/{novel_id}/memory
GET    /api/novels/{novel_id}/memory
DELETE /api/novels/{novel_id}/memory/{memory_id}
```

### 伏笔模块

已支持：

- 创建伏笔记录
- 查询伏笔记录
- 更新伏笔状态

主要接口：

```text
POST  /api/novels/{novel_id}/foreshadowing
GET   /api/novels/{novel_id}/foreshadowing
PATCH /api/novels/{novel_id}/foreshadowing/{foreshadowing_id}/status
```

### 审校风险模块

已支持：

- 创建审校风险
- 查询风险列表
- 更新风险处理状态

主要接口：

```text
POST  /api/novels/{novel_id}/reviews
GET   /api/novels/{novel_id}/reviews
PATCH /api/novels/{novel_id}/reviews/{issue_id}/status
```

### 生成任务模块

已支持：

- 创建生成任务
- 查询任务列表
- 查询任务详情
- 创建 Agent 预留任务

主要接口：

```text
POST /api/novels/{novel_id}/tasks
GET  /api/novels/{novel_id}/tasks
GET  /api/novels/{novel_id}/tasks/{task_id}
POST /api/novels/{novel_id}/tasks/agent-runs
```

### 工作台聚合接口

已支持工作台真实数据聚合：

```text
GET /api/novels/{novel_id}/dashboard
```

当前返回：

- 作品基础信息
- 起始需求文档
- 章节数量
- 正文总字数
- 结构化记忆数量
- 伏笔数量
- 开放风险数量
- 活跃任务数量
- 最新章节
- 最新任务
- 开放风险列表

## 4. 前端已完成功能

前端位于：

```text
apps/web
```

### 登录与注册

页面：

```text
/login
/register
```

已接入真实后端接口：

- 注册后保存 Token
- 登录后保存 Token
- 后续请求自动携带 Bearer Token

### 作品管理

页面：

```text
/projects
```

已完成：

- 作品列表展示
- 起始需求文档表单
- 创建作品
- 创建后进入工作台
- 跳转章节管理

### 章节管理

页面：

```text
/chapters
```

已完成：

- 选择作品
- 展示章节列表
- 新建章节
- 编辑章节标题、序号、状态、摘要、正文
- 保存章节
- 删除章节
- 统计当前作品章节数与总字数

### 创作工作台

页面：

```text
/workbench
```

已完成真实数据联动：

- 当前作品概览
- 章节数量
- 正文总字数
- 伏笔数量
- 开放风险数量
- 起始需求文档展示
- 最新章节展示
- 生产任务队列
- 风险提醒
- Agent 预留任务按钮
- 跳转章节管理

### 样本分析

页面：

```text
/sample-analysis
```

当前为功能预留页面，用于后续接入：

- 样本文本上传
- 风格分析
- AI 味规避规则提取
- 用户写作风格画像

### 个性化

页面：

```text
/personalization
```

当前为功能预留页面，用于后续配置：

- 创作偏好
- 自动化策略
- 记忆同步策略
- 风险处理策略

### 用户中心

页面：

```text
/user
```

当前展示：

- 当前用户信息
- 套餐状态预留
- 作品数量
- 数据保留说明

## 5. 当前数据链路

当前核心链路如下：

```text
用户注册 / 登录
  -> 获取 JWT Token
  -> 创建作品
  -> 保存起始需求文档
  -> 创建章节
  -> 工作台读取 dashboard
  -> 展示章节、字数、任务、风险等真实数据
```

Agent 预留链路如下：

```text
前端点击 Agent 任务按钮
  -> POST /api/novels/{novel_id}/tasks/agent-runs
  -> 后端创建 generation_tasks 记录
  -> 工作台任务队列展示
```

## 6. 智能体核心功能预留

当前智能体尚未真正调用大模型，但已经预留后端入口：

```text
apps/api/app/services/agent_orchestrator.py
```

当前预留任务类型包括：

- `plan_novel`
- `generate_chapter`
- `sync_memory`
- `anti_ai_review`

后续接入方向：

```text
FastAPI
  -> generation_tasks
  -> Redis / Celery
  -> Worker
  -> LangGraph Agent
  -> 回写章节、记忆、伏笔、风险、任务状态
```

## 7. 本地启动方式

### 启动 Docker 基础服务

在项目根目录执行：

```powershell
cd D:\CodeProject\AgentProject\NovelForge
docker compose up -d
```

### 启动后端

```powershell
conda activate novelforge-api
cd D:\CodeProject\AgentProject\NovelForge\apps\api
python -m alembic upgrade head
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

后端接口文档：

```text
http://127.0.0.1:8000/docs
```

### 启动前端

新开一个终端：

```powershell
cd D:\CodeProject\AgentProject\NovelForge\apps\web
npm run dev
```

前端访问：

```text
http://127.0.0.1:3000
```

## 8. 当前已验证内容

已验证：

- 后端语法检查通过
- 后端应用导入通过
- 前端生产构建通过
- PostgreSQL 健康检查通过
- 工作台、章节、作品等前端路由可以被 Next.js 正常构建识别

## 9. 下一步建议

建议下一步进入 Agent 执行层开发：

1. 引入 Worker 服务
2. 接入 Redis 队列
3. 让 `generation_tasks` 状态从 `queued -> running -> completed / failed` 自动流转
4. 先做模拟 Agent，不调用大模型
5. 再接入 LangGraph
6. 实现第一条真实智能体链路：起始需求文档 -> 小说规划 -> 第一章草稿

优先级最高的下一步：

```text
构建 worker + 任务状态流转 + 模拟章节生成
```

这一步完成后，NovelForge 就会从“可维护小说项目的数据系统”进入“可自动推进创作任务的 Agent 系统”。
