# NovelForge Windows

NovelForge Windows 是不依赖 NovelForge 服务端的本地桌面版本。应用复用 Web 版的完整前端设计，在同一个桌面进程内运行本地接口与后台任务；数据写入 SQLite 和本地文件。

## 当前能力

- 与 Web 版一致的工作台、布局、主题和交互；
- 作品、作品圣经、剧情事件与自动全书生成；
- 章节管理、章节画布、AI 生成/审校、人工介入和 Markdown 导出；
- 结构化记忆、时间线、伏笔和审校问题；
- 资料检索、热梗库及本地 RAG；
- TXT/MD 本地样本导入、AI 分析、章节切分、标注、笔记与 RAG；
- 创作模型、审校模型、Embedding、Tavily、个性化和外观设置；
- API Key 使用当前 Windows 用户的 DPAPI 加密；
- 默认数据目录：`%LOCALAPPDATA%\NovelForge`。

Windows 版不需要部署 NovelForge 服务器，也不依赖 Redis、PostgreSQL、MinIO 或 Docker。应用仅在本机回环地址启动内置接口，退出应用后即停止。调用模型、Embedding 或 Tavily 时，应用会直接连接用户配置的接口。

桌面壳默认使用 Chromium 的自适应 GPU 渲染，并继续关闭 Web 主题中高开销的实时背景模糊。若显卡驱动或远程桌面环境出现拖影、花屏，可从开始菜单启动“NovelForge（兼容渲染）”，或在启动前设置 `NOVELFORGE_DISABLE_GPU=1`。旧的 `NOVELFORGE_ENABLE_GPU=1` 开关仍兼容。

WebEngine 的 Cookie/localStorage 持久保留，HTTP 代码缓存按 Next build id 隔离，避免升级后混用旧 HTML、RSC 与 JavaScript。应用优先在 `127.0.0.1:47831` 启动；端口被占用时自动回退到临时回环端口。桌面 API 会拒绝跨站浏览器请求，不会监听局域网地址。

0.2.5 起，侧栏跳转只保留一个在途路由和最后一次点击，并在 SPA 路由提交后短时推进 Qt 真实视图重绘。这不是加载动画；页面内容仍直接切换，用于避免 Chromium 已更新而 Windows 桌面仍残留旧纹理。

0.2.6 起，这一推进覆盖所有可能产生新帧的操作（滚动、点击、输入、侧栏动画、窗口尺寸变化），并在窗口可见期间保持 500 ms 心跳重绘：混合显卡机器上偶发的撕裂帧/黑屏卡住（Qt/DWM 呈现了过期纹理而无后续补帧）会在约 0.2–0.5 秒内自愈，不再停留到用户截图。窗口初始尺寸也钳制到屏幕可用区域。定位方法与数据见 [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md)。

## 开发启动

要求 Windows 10/11 和 Python 3.11+。双击：

```text
启动NovelForge-Windows.cmd
```

首次启动会在本目录创建 `.venv` 并安装依赖。也可以手动执行：

```powershell
cd D:\CodeProject\AgentProject\NovelForge\Windows
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m novelforge_windows
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
cd frontend
npm run test:markdown-import
npm run build
```

真实 Qt WebEngine 交互性能测试（分别覆盖兼容渲染和默认 GPU）：

```powershell
.\.venv\Scripts\python.exe scripts\profile_interactions.py --output .runtime\perf\software --rendering software
.\.venv\Scripts\python.exe scripts\profile_interactions.py --output .runtime\perf\gpu --rendering gpu
```

真实 Win32 鼠标输入与可见帧语义一致性压力测试：

```powershell
.\.venv\Scripts\python.exe scripts\diagnose_rapid_tabs.py --output .runtime\perf\rapid-gpu --rendering gpu --input win32 --clicks 160 --interval-ms 20 --screen-capture-interval-ms 20 --verify-current-page-last
.\.venv\Scripts\python.exe scripts\diagnose_rapid_tabs.py --output .runtime\perf\rapid-software --rendering software --input win32 --clicks 120 --interval-ms 80
```

本轮定位方法、根因和前后数据见 [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md)。

## 打包 EXE

```powershell
winget install JRSoftware.InnoSetup
.\scripts\build.ps1
```

可安装的 x64 安装包输出为：

```text
release/NovelForge-Windows-x64-Setup.exe
```

`dist/NovelForge/` 是安装器使用的独立程序目录。目标电脑无需安装 Python。当前构建未进行商业代码签名，首次安装时 Windows 可能显示“未知发布者”；正式发布前应使用受信任的代码签名证书签名。

## 目录结构

```text
Windows/
├─ frontend/                    # Web 设计的桌面静态前端
├─ backend/api/app/             # 本地 API 与业务能力
├─ backend/worker/worker/       # 进程内后台任务
├─ src/novelforge_windows/      # 桌面壳、本地运行时、DPAPI
├─ tests/                       # 本地全链路测试
├─ scripts/build.ps1            # 前端、PyInstaller、安装器构建
└─ 启动NovelForge-Windows.cmd
```

## 与 Web 版的功能差异

Windows 版只删除了不适合单机使用的账号/用户管理，以及公共样本发布、搜索、投票、举报和多人协作。其余可本地运行的 Web 能力均已保留，后台任务改为进程内队列。

详细的 Web/Windows 能力映射见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。
