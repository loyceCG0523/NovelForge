# NovelForge Web

NovelForge Web 是长篇小说自动生产系统的前端控制台，基于 Next.js App Router 和 React 实现。当前页面已经接入真实后端接口，不再是纯 HTML 原型。

## 本地启动

先启动后端和 Worker 依赖服务：

```powershell
cd D:\CodeProject\AgentProject\NovelForge
docker compose up -d
```

启动 API：

```powershell
conda activate novelforge-api
cd D:\CodeProject\AgentProject\NovelForge\apps\api
python -m alembic upgrade head
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

生产构建检查：

```powershell
npm run build
```

## 技术栈

| 模块 | 当前实现 |
| --- | --- |
| 前端框架 | Next.js App Router |
| UI | React + 原生 CSS |
| 鉴权 | JWT 存入 localStorage |
| API 调用 | `lib/api.js` 统一封装 |
| 样本上传 | `FormData` 上传到 FastAPI |
| 任务状态 | 前端轮询任务或业务聚合接口 |

## 当前主导航

侧边栏保留真正的产品功能：

| 页面 | 路由 | 说明 |
| --- | --- | --- |
| 创作工作台 | `/workbench` | 单部作品的自动生产状态、剧情事件、风险和生产控制 |
| 剧情事件 | `/story-events` | 查看一个闭环剧情事件、章节计划、事件级风险和重跑入口 |
| 章节管理 | `/chapters` | 维护章节列表、摘要、正文、上下文快照和章节风险 |
| 作品管理 | `/projects` | 多作品管理、起始需求文档编辑、作品圣经状态 |
| 样本分析 | `/sample-analysis` | 上传 TXT/MD 样本，异步生成风格工程特征报告 |
| 设置 | `/personalization` | 账号信息、创作偏好、自动化策略、LLM API Key |

兼容路由：

| 页面 | 说明 |
| --- | --- |
| `/login` | 登录 |
| `/register` | 注册 |
| `/chapter-canvas` | 专注阅读章节正文 |
| `/memory` | 结构化记忆维护页，不作为主导航入口 |
| `/user` | 兼容旧用户中心路由，跳转到设置页 |

## 核心页面说明

### 作品管理

`/projects` 用于管理多部作品。用户可以：

- 新建作品。
- 编辑已有作品的起始需求文档。
- 保存并刷新作品圣经。
- 查看作品圣经当前状态。
- 进入工作台。

创建作品后，后端会自动写入兜底作品圣经，并入队 `build_story_bible` 任务。

### 创作工作台

`/workbench` 是主生产入口。当前支持：

- 选择作品。
- 查看当前自动生产状态。
- 开始或继续整本书自动生产。
- 查看当前剧情事件进度。
- 查看风险提醒和自动修复历史。
- 快速跳转章节管理、剧情事件、结构化记忆。

工作台通过 `/api/novels/{novel_id}/dashboard` 获取聚合数据。

### 剧情事件

`/story-events` 用于管理 6-12 章左右的闭环大事件。当前支持：

- 查看事件目标、冲突、章节范围和生成进度。
- 查看每章计划及状态。
- 单章重跑。
- 从某个章节计划继续生成。
- 查看事件级质量评分与风险。

### 章节管理与章节画布

`/chapters` 用于维护章节草稿和上下文快照。当前支持：

- 编辑章节标题、序号、状态、摘要、正文。
- 展开正文或上下文快照，让编辑区占据右侧空间。
- 删除章节，并同步清理相关结构化记忆和事件计划。
- 查看章节级审校风险与自动修复历史。

`/chapter-canvas` 是专注阅读页，去掉编辑区，只展示章节正文和必要的章节导航。

### 样本分析

`/sample-analysis` 已从“粘贴文本同步分析”升级为正式异步架构：

```text
选择作品
  -> 上传 TXT/MD 样本文本
  -> API 保存到 MinIO
  -> Worker 执行 analyze_sample
  -> 前端轮询 sample_analyses
  -> 展示结构化风格报告
```

报告包含：

- 文风指纹。
- 情绪曲线。
- 伏笔模式。
- 对白风格。
- 节奏模型。
- 可迁移风格向量。

只有 `completed` 的样本报告会进入后续章节生成上下文。

### 设置

`/personalization` 当前整合账号与个性化配置：

- 登录邮箱、套餐、数据保留等账号信息。
- 创作偏好。
- 自动化策略。
- LLM API Key、Base URL、模型名。

API Key 仅保存在当前用户偏好中，Worker 生成章节时会按用户配置调用 OpenAI-compatible LLM。

## API 调用约定

前端统一通过 [lib/api.js](D:/CodeProject/AgentProject/NovelForge/apps/web/lib/api.js) 调用后端：

- 自动读取 `novelforge_token`。
- 自动加 `Authorization: Bearer ...`。
- JSON 请求自动加 `Content-Type: application/json`。
- `FormData` 请求不手动设置 `Content-Type`，避免破坏文件上传边界。
- 非 2xx 响应会抛出错误并显示到页面。

## 视觉设计

当前 UI 继承 `docs/product/ui-prototypes` 的深色侧边栏、白色工作区、低饱和青绿色主色。实际产品页已根据功能简化：

- 侧边栏只放主功能。
- 起始需求归入作品管理。
- 最新章节和生产队列已从工作台移除。
- 风险提醒由系统自动处理，前端只展示问题和修复历史。

## 当前验证

已验证：

- `npm run build` 通过。
- `/sample-analysis` 已能作为静态路由构建。
- `FormData` 上传支持已接入。
- 登录、作品、工作台、剧情事件、章节、样本分析等页面均在 App Router 下正常识别。
