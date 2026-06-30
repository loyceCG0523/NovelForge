# NovelForge Web

NovelForge 前端基础框架，风格参考 `docs/product/ui-prototypes`，当前覆盖登录、注册、作品管理、创作工作台、样本分析、个性化和用户中心。

## 本地启动

确保后端已启动：

```powershell
conda activate novelforge-api
cd D:\CodeProject\AgentProject\NovelForge\apps\api
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

启动前端：

```powershell
cd D:\CodeProject\AgentProject\NovelForge\apps\web
copy .env.local.example .env.local
npm install
npm run dev
```

访问：

```text
http://127.0.0.1:3000
```

## 页面

- `/login`: 登录。
- `/register`: 注册。
- `/projects`: 作品管理，可创建真实作品。
- `/workbench`: 创作工作台，可读取 dashboard 并创建 Agent 预留任务。
- `/sample-analysis`: 样本分析预留。
- `/personalization`: 创作偏好与自动化策略预留。
- `/user`: 用户中心。

## Agent 预留

前端工作台的“启动 Agent 任务”会调用：

```text
POST /api/novels/{novel_id}/tasks/agent-runs
```

当前后端会写入 `generation_tasks`，后续接入 LangGraph/Celery 后无需更改前端主流程。
