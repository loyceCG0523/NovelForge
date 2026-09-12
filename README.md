# NovelForge

AI 辅助长篇小说创作系统。从“起始需求文档”创建作品后，系统生成全书级作品圣经与节奏计划；可按剧情事件一键生成连续章节，或启动整本书自动生产（LangGraph 总控）。每章经过上下文快照、字数护栏、Reviewer→Writer 修订闭环、反 AI 句式检查与事件级跨章审校，并自动沉淀结构化记忆、伏笔、时间线支撑长篇连续性。辅助能力包括热梗库与联网资料检索。模型接入使用用户自带的 OpenAI 兼容 API Key（Embedding 用阿里云百炼，联网检索用 Tavily）。

> 说明：Web 版的样本分析/协同标注模块（含双通道 RAG 与可信门槛）已于 2026-09 整体下线（人工标注成本过高），对应数据表经迁移 `20260912_0022` 删除；Windows 版仍保留私人样本能力，两版自此在该功能上分叉。

仓库同时维护两个彼此独立的版本：

| 目录 | 版本 | 运行方式 |
| --- | --- | --- |
| [`Web/`](Web/) | Web 版（多用户服务端） | Next.js + FastAPI + Worker + PostgreSQL/Redis/MinIO |
| [`Windows/`](Windows/) | Windows 本地版（当前 0.2.8） | Web 前端桌面壳 + 本地 API/进程内任务 + SQLite/本地文件 |

Web 版保留原有登录、多用户和服务端任务架构。Windows 版复用 Web 的完整前端设计与可本地运行的业务能力，不需要部署 NovelForge 服务器；仅移除账号体系和公共社区能力。数据默认保存在当前 Windows 用户的本地应用数据目录，安装包由 `Windows/scripts/build.ps1` 产出（PyInstaller + Inno Setup）。

两个版本分别维护依赖和启动脚本，互不引用运行时代码。

更多细节：

- Web 版文档：[`Web/docs/`](Web/docs/)（产品方案、架构与当前进度）
- Windows 版文档：[`Windows/docs/`](Windows/docs/)（架构差异、交互性能与渲染稳定性记录）
