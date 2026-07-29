# NovelForge 当前进度与系统功能说明

更新时间：2026-07-11

## 1. 当前项目状态

NovelForge 已经从 UI 原型和基础 CRUD，推进到“可本地运行的长篇小说 Agent 生产系统雏形”。

当前已具备：

- 注册、登录、用户偏好和 LLM 配置。
- 多作品管理和起始需求文档维护。
- 创建作品后自动生成作品圣经。
- 以剧情事件为单位自动生产 6-12 章闭环剧情。
- 已加入全书节奏控制：目标字数作为篇幅预算，叙事闭环作为完结条件。
- 自动生产支持四种工作模式：全自动、Human-in-loop、番茄试写、测试模式。
- 章节生成、上下文快照、结构化记忆同步。
- 连续性审校、事件级质量审校和自动修订。
- 样本分析：上传 TXT/MD 到 MinIO，Worker 异步分片分析并聚合报告。
- 前端控制台：工作台、剧情事件、章节管理、章节画布、作品管理、样本分析、设置。

## 2. 本地基础设施

通过项目根目录 `docker-compose.yml` 启动：

| 服务 | 作用 |
| --- | --- |
| PostgreSQL + pgvector | 主业务数据库，后续支持向量检索 |
| Redis | Agent 任务队列 |
| MinIO | S3 兼容对象存储，用于样本文本等大文件 |

启动命令：

```powershell
cd D:\CodeProject\AgentProject\NovelForge
docker compose up -d
```

## 3. 应用组成

```text
NovelForge/
  apps/
    api/       # FastAPI 后端
    web/       # Next.js 前端
    worker/    # Redis Worker + LangGraph
  docs/
    engineering/
    product/
  docker-compose.yml
  .env.example
```

## 4. 后端能力

后端目录：

```text
apps/api
```

当前主要能力：

| 模块 | 状态 |
| --- | --- |
| 用户认证 | 已完成注册、登录、JWT、当前用户 |
| 用户偏好 | 已支持创作偏好、自动化策略、LLM API Key |
| 作品管理 | 已支持作品 CRUD、起始需求文档、章节字数范围 |
| 作品圣经 | 已支持兜底圣经、自动生成任务、读取和保存 |
| 章节管理 | 已支持创建、编辑、删除、上下文快照 |
| 剧情事件 | 已支持事件规划、章节计划、局部重跑、继续生成 |
| 自动生产 | 已支持 `AutoNovelRun`、`produce_novel` 总控任务和全书 pacing_plan |
| 结构化记忆 | 已支持自动抽取、实体合并、手动删除、清空 |
| 审校风险 | 已支持连续性风险、事件级风险、自动修复历史 |
| 样本分析 | 已支持文件上传、MinIO 存储、异步分片分析 |
| 任务队列 | 已支持 Redis 入队和 Worker 消费 |

## 5. Worker 能力

Worker 目录：

```text
apps/worker
```

当前支持任务：

| task_type | 说明 |
| --- | --- |
| `produce_novel` | 整本书自动生产 |
| `build_story_bible` | 生成或刷新作品圣经 |
| `generate_story_event` | 规划并生成一个闭环剧情事件 |
| `continue_story_event` | 从某个章节计划继续生成 |
| `generate_chapter` | 生成或重写章节 |
| `check_story_event_quality` | 事件级质量审校 |
| `analyze_sample` | 样本分片分析和聚合报告 |

Worker 当前使用 Redis 队列：

```text
API 创建 generation_tasks
  -> Redis 队列
  -> Worker 消费
  -> 执行业务任务
  -> 回写任务状态和业务表
```

## 6. LangGraph 使用进展

当前已接入 LangGraph 的核心流程：

```text
apps/worker/worker/graphs/novel_production_graph.py
apps/worker/worker/graphs/event_generation_graph.py
```

`NovelProductionGraph` 负责整本书级生产循环。

`EventGenerationGraph` 负责一个大事件内的章节规划、章节生成、审校、修订和事件质量检查。

这意味着项目已经不是“一章一章直接生成”，而是围绕“剧情事件”进行有状态、多节点、可回写的 Agent 工作流。

当前自动生产不再把 `current_words >= target_words` 直接视为作品完结，而是使用双条件：

```text
目标字数进入允许完结窗口
  + 最近剧情事件声明完成主线/人物/伏笔/结局闭环
  -> 标记作品完成
```

`pacing_plan` 会把目标字数拆成开篇、主线展开、中段转折、高压升级、终局推进、结局收束等阶段。事件规划和章节生成都会收到当前阶段、剩余字数和完结窗口信息；进入 `final_arc` / `ending` 后，系统会优先收束主线、人物弧光和伏笔，避免继续开启大型新支线。

自动生产当前支持四种模式：

| 模式 | 行为 |
| --- | --- |
| 全自动 | 按剧情事件连续规划、生成、审校，直到满足字数窗口和叙事闭环 |
| Human-in-loop | 每次只生成一个事件大纲和章节计划，等待用户编辑/确认后再批量生成该事件章节 |
| 番茄试写 | 全书节奏仍按用户初始目标字数计算，但首轮在 8-10 万字附近的事件边界暂停，用于控制试写成本 |
| 测试模式 | 真实生成 1 个剧情事件对应章节后暂停，用户检查质量后可决定是否继续自动生产 |

## 7. 章节上下文

`ChapterContextBuilder` 当前会组装：

- 作品基础信息。
- 起始需求文档。
- 作品圣经。
- 目标章节。
- 最近 3 章全文和摘要。
- 合并后的结构化记忆。
- 未完成伏笔。
- 开放或系统延后处理的审校风险。
- 已完成样本分析风格向量。
- 单章字数范围。
- 风格参考、禁忌内容和自动化策略。

上下文快照写入：

```text
chapters.context_snapshot
```

## 8. 结构化记忆

当前结构化记忆支持：

- 自动从章节正文抽取。
- 人物、关系、地点、道具、事件、时间线、世界规则、章节摘要等类型。
- 规范化实体名，减少“女主许清禾”和“许清禾”重复。
- 合并同一实体的多条记忆，进入 Prompt 时使用实体级摘要，而不是所有历史记录堆叠。
- 删除章节时同步清理对应自动记忆。
- 前端可删除单个记忆组或清空全部记忆。

## 9. 审校与自动修订

当前 QualityAgent 包含：

- `ContinuityChecker`: 章节连续性审校。
- `EventQualityChecker`: 剧情事件级质量检查。
- `RevisionAgent`: 根据风险自动修订章节。

当前产品策略：

```text
发现风险
  -> 系统自动判断和处理
  -> 可修复则自动修订
  -> 修订后重新审校
  -> 前端只展示问题和修复历史
```

这符合“用户填写起始需求后尽量不参与生成过程”的产品原则。

## 10. 样本分析

样本分析已经升级为正式异步架构：

```text
前端上传 TXT/MD
  -> API 保存文件到 MinIO
  -> 创建 sample_analyses 记录
  -> 创建 analyze_sample 任务
  -> Worker 流式读取对象存储
  -> 约 8000 字切片
  -> 分片抽取指标
  -> 聚合为 sample_analysis.v2 报告
  -> 写回结构化报告和风格向量
```

分析维度：

| 维度 | 指标 |
| --- | --- |
| 文风指纹 | 句长、比喻密度、感官覆盖、叙事距离、对白占比、段落节奏 |
| 情绪曲线 | 情绪标签、波动、高潮、低谷 |
| 伏笔模式 | 埋设密度、兑现密度、兑现周期、误导策略 |
| 对白风格 | 话语长度、潜台词、打断、解释性对白 |
| 节奏模型 | 冲突间隔、信息释放密度、章尾钩子 |

样本原文不写入数据库；只有完成状态的样本分析会进入后续章节生成上下文。

## 11. 前端能力

前端目录：

```text
apps/web
```

当前页面：

| 路由 | 状态 |
| --- | --- |
| `/login` | 已接入后端登录 |
| `/register` | 已接入后端注册 |
| `/projects` | 作品管理、起始需求文档、作品圣经状态 |
| `/workbench` | 自动生产入口和风险/事件状态 |
| `/story-events` | 剧情事件、章节计划、事件风险 |
| `/chapters` | 章节编辑、上下文快照、章节风险 |
| `/chapter-canvas` | 章节阅读画布 |
| `/sample-analysis` | 样本上传、异步状态、结构化报告 |
| `/memory` | 结构化记忆维护 |
| `/personalization` | 设置、偏好、LLM API Key |
| `/user` | 旧路由兼容跳转 |

## 12. 启动方式

### API

```powershell
conda activate novelforge-api
cd D:\CodeProject\AgentProject\NovelForge\apps\api
python -m alembic upgrade head
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### Worker

```powershell
conda activate novelforge-api
cd D:\CodeProject\AgentProject\NovelForge\apps\worker
python -m worker.main
```

### Web

```powershell
cd D:\CodeProject\AgentProject\NovelForge\apps\web
npm run dev
```

## 13. 已验证内容

最近验证通过：

- `python -B -m compileall app`
- `python -B -m compileall worker`
- `python -m alembic upgrade head`
- FastAPI 应用导入检查。
- MinIO 上传和流式读取。
- `SampleAnalysisAgent` 输出 `sample_analysis.v2` 和 `chunked_map_reduce` 报告。
- `npm run build`
- `git diff --check`

## 14. 下一步建议

下一阶段建议聚焦：

1. 完善章节生成质量：减少模拟逻辑，强化真实 LLM 输出稳定性。
2. 增加 Agent 步骤日志表，保存每个节点输入输出，便于调试和面试展示。
3. 增加样本分析报告与作品生成策略的可视化绑定关系。
4. 增加任务取消、重试和失败恢复。
5. 增加导出完整书稿功能。
6. 增加基础自动化测试，覆盖创建作品、样本分析、章节生成和事件生产链路。
