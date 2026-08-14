# Windows 本地版架构

## 边界

Windows 版与 Web 版共享产品设计，但安装后不依赖 Web 版目录或外部 NovelForge 服务。桌面应用是一个本地进程：

```text
PySide6 + Qt WebEngine（Web 静态前端）
  -> 本机回环 FastAPI
  -> SQLite / 本地对象文件 / Windows DPAPI
  -> 进程内后台任务
  -> 用户配置的模型、Embedding、Tavily 接口（可选）
```

应用会在随机的本机回环端口启动内置 FastAPI，并随应用退出；不监听外网地址，不需要 Redis、PostgreSQL、MinIO、Docker 或独立 Worker。

## Web 能力映射

| Web 版能力 | Windows 版处理方式 |
| --- | --- |
| 注册、登录、JWT | 删除；使用当前 Windows 用户环境 |
| PostgreSQL + pgvector | 本地 SQLite；向量相似度在本地计算 |
| Redis 异步队列 | 进程内后台任务队列 |
| MinIO | `%LOCALAPPDATA%\NovelForge\objects` 本地目录 |
| 用户偏好 | SQLite 本地偏好记录 |
| 用户 API Key | Windows DPAPI 当前用户加密 |
| 多人样本协作、投票、举报 | 删除 |
| 公共样本发布 | 删除，仅保留个人本地样本库 |
| 作品、章节、章节画布与导出 | 完整保留 |
| 作品圣经、剧情事件、自动全书生成 | 完整保留，进程内异步执行 |
| 记忆、时间线、伏笔、审校 | 完整保留 |
| 资料检索、热梗库、样本分析与 RAG | 完整保留为本地数据和本地任务 |

## 数据位置

默认根目录为 `%LOCALAPPDATA%\NovelForge`：

```text
NovelForge/
├─ novelforge-local.db
├─ objects/
└─ web-profile/
```

开发和测试可以通过 `NOVELFORGE_WINDOWS_DATA_DIR` 指向独立目录。

旧版桌面原型的 `novelforge.db` 会在首次启动时自动迁移一次。

## 运行关系

```text
Qt WebEngine -> loopback API -> services -> SQLite/local files
                                  -> in-process task queue
```
