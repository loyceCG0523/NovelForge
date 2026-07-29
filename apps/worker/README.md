# NovelForge Worker

NovelForge Worker 是后台 Agent 执行进程，负责消费 Redis 队列中的 `generation_tasks`，执行章节生成、剧情事件生成、作品圣经刷新、样本分析、审校和自动修订，并把结果回写 PostgreSQL。

当前 Worker 使用轻量 Redis 队列实现：

```text
FastAPI enqueue_agent_task
  -> Redis RPUSH novelforge:agent_tasks
  -> Worker BLPOP
  -> execute_task
  -> 更新 generation_tasks / chapters / memory_items / review_issues 等业务表
```

## 本地启动

先确保基础服务已启动：

```powershell
cd D:\CodeProject\AgentProject\NovelForge
docker compose up -d
```

安装依赖：

```powershell
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

## 当前核心 Agent

| Agent | 当前落地职责 |
| --- | --- |
| `NovelProductionAgent` | 整本书自动生产总控，驱动一个个剧情事件直到达到目标或暂停 |
| `StoryPlanningAgent` | 作品圣经生成、剧情事件规划、章节计划生成 |
| `ChapterWritingAgent` | 根据 ChapterContext 和 PromptBuilder 生成章节正文 |
| `SampleAnalysisAgent` | 对优秀小说样本做异步分片分析，输出风格工程特征 |
| `MemoryAgent` | 章节生成后抽取并合并结构化记忆 |
| `QualityAgent` | 连续性审校、事件级质量审校、自动修订章节 |

## 当前任务类型

| task_type | 处理函数或 Graph | 说明 |
| --- | --- | --- |
| `produce_novel` | `run_novel_production_graph` | 整本书生产总控 |
| `build_story_bible` | `handle_build_story_bible` | 生成或刷新作品圣经 |
| `generate_story_event` | `run_event_generation_graph` | 规划并生成一个 6-12 章闭环剧情事件 |
| `continue_story_event` | `handle_continue_story_event` | 从指定章节计划继续生成 |
| `generate_chapter` | `handle_generate_chapter` | 生成或重写单章 |
| `check_story_event_quality` | `handle_check_story_event_quality` | 对完整剧情事件做质量审校 |
| `analyze_sample` | `handle_analyze_sample` | 从 MinIO 流式读取样本，分片抽取指标并聚合报告 |

## LangGraph 使用位置

当前已有两个 Graph：

```text
apps/worker/worker/graphs/novel_production_graph.py
apps/worker/worker/graphs/event_generation_graph.py
```

`NovelProductionGraph` 负责整本书生产循环：

```text
读取 AutoNovelRun
  -> 确认作品圣经
  -> 创建剧情事件任务
  -> 生成一个闭环事件
  -> 更新整本书进度
  -> 未达到目标则继续下一事件
```

`EventGenerationGraph` 负责一个大事件：

```text
规划事件目标
  -> 生成 6-12 个章节计划
  -> 逐章构建上下文
  -> 调用章节生成
  -> 同步结构化记忆
  -> 连续性审校与自动修订
  -> 事件级质量审校
  -> 回写 story_events / event_chapter_plans
```

## 章节生成链路

`generate_chapter` 会执行：

```text
确定目标章节
  -> ChapterContextBuilder 组装上下文
  -> 根据用户 LLM 配置决定模拟生成或真实 LLM 生成
  -> 写入 chapters
  -> 同步剧情事件章节计划
  -> MemoryAgent 抽取结构化记忆
  -> QualityAgent 连续性审校
  -> 自动修订最多 2 次
  -> 若属于剧情事件，触发事件级质量刷新
```

章节上下文会保存到：

```text
chapters.context_snapshot
```

上下文包含：

- 起始需求文档。
- 作品圣经。
- 最近 3 章全文和摘要。
- 合并后的结构化记忆。
- 未完成伏笔。
- 开放或系统延后处理的审校风险。
- 已完成样本分析风格向量。
- 字数范围、风格参考、禁忌内容和自动化策略。

## 样本分析链路

`analyze_sample` 面向百万字级优秀小说样本，采用异步切片架构：

```text
API 上传文件到 MinIO
  -> Worker 读取 source_object_key
  -> iter_text_object_chunks 约 8000 字切片
  -> SampleAnalysisAgent 分片抽取指标
  -> 聚合为 sample_analysis.v2 报告
  -> 更新 sample_analyses.status = completed
```

分析维度：

- 文风指纹：句长、比喻密度、感官维度、叙事距离、对白占比、段落节奏。
- 情绪曲线：分片/章节情绪标签、高潮、低谷和波动。
- 伏笔模式：埋设密度、兑现密度、周期和误导策略。
- 对白风格：话语长度、潜台词、打断、解释性对白。
- 节奏模型：冲突间隔、信息释放密度、章尾钩子。

原文不写入数据库；最终只保存结构化报告和可迁移风格向量。

## 审校与自动修复

`QualityAgent` 的规则：

- 发现连续性问题后，不要求用户手动处理。
- 系统自动判断是否可修复。
- 可修复问题统一由章节流水线生成局部段落补丁。
- 修订后再次审校。
- `ReviewIssue.payload.repair_history` 保存自动处理历史。
- 前端展示问题、状态和修复历史，不把生成过程负担转移给用户。

## 失败处理

Worker 捕获异常后会：

- `generation_tasks.status = failed`
- `generation_tasks.error_message = 异常信息`
- 对特定业务实体同步状态：
  - `produce_novel` 失败会更新 `auto_novel_runs`
  - `generate_story_event` 失败会更新 `story_events`
  - `analyze_sample` 失败会更新 `sample_analyses`

## 当前验证

已验证：

- `python -B -m compileall worker`
- Worker 可导入 API 层模型与服务。
- `analyze_sample` 依赖的 MinIO 上传和流式读取可用。
- `SampleAnalysisAgent` 可输出 `sample_analysis.v2` 和 `chunked_map_reduce` 报告。
