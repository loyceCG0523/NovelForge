# Windows 交互性能记录（0.2.5 / 0.2.6）

## 0.2.6：呈现层卡死的泛化修复（花屏/黑屏自愈）

0.2.5 只在 SPA 路由提交（`urlChanged`）后推进重绘。后续用户截图报告另一类花屏：侧栏纹理沿对角线撕裂进内容区、侧栏底部出现缺块棋盘格，且卡住不自愈。用 `scripts/diagnose_present_artifacts.py`（Qt 合成输入 + 真实桌面像素 + 静置后与 CDP 截图对照）复现确认：

- 侧栏折叠/展开的 180 ms 网格过渡 + 立即滚动时，**窗口在静置 1.2 s 后整屏纯黑**，而 Chromium 内部早已渲染出正确页面（CDP 截图正常）——Qt/DWM 阶段把一张空纹理留在屏幕上，没有后续呈现；
- **一次显式 `QWebEngineView.update()` 立即恢复正确画面**（距离从 207 回落到噪声底 7.7），证明问题是"呈现丢失/撕裂后无人补帧"，而不是内容损坏；
- 用户截图里的对角线撕裂与棋盘格是同一机制的另一种采样时刻（纹理写到一半被取样 / 瓦片尚未重光栅）。

修复（不改渲染通路、保持 GPU 加速）：

1. `urlChanged` 后的 4 帧推进保持不变；
2. 视图内的滚轮、鼠标、键盘、触摸与尺寸变化事件武装同样的推进，延长到 12 帧（约 190 ms，覆盖侧栏过渡动画的收尾帧）；软件渲染模式不叠加推进（呈现本身慢，避免无效 CPU 占用）；
3. 窗口可见期间每 500 ms（软件渲染 1000 ms）心跳推进一次 `update()`：即使没有任何输入（异步 DOM 更新、轮询），卡住的画面也会在半秒内自愈；
4. 侧栏用户菜单的"模型与创作设置 / 外观设置"原来直接 `router.push`，绕过 0.2.5 的单在途路由门禁，现统一走同一意图合并队列；
5. 窗口初始尺寸钳制到屏幕可用区域（本机 1280×800 逻辑像素，原默认 1440×900 会超出屏幕）。

验证（同一台 RTX 4070 Laptop 混合显卡机器，dpr 2）：

| 场景 | 修复前 | 修复后 |
| --- | ---: | ---: |
| toggle 场景静置不一致 | 1/16（整屏黑屏卡住） | 0/24 |
| mixed 场景静置不一致 | — | 0/24 |
| 快速侧栏切换（120 次点击）混合帧 | 0 | 0 |
| 可见响应中位数 / P90 | 72.8 / 82.3 ms | 73.3 / 79.3 ms |
| Long Task | 0 | 0 |

备选方案记录：`--disable-gpu-compositing`（软件合成、保留 GPU 光栅）在同场景也测得 0/16，可作为极端环境下的兜底开关，但它改变了共享纹理呈现通路、缺乏更广泛环境的验证；在"补帧自愈"已确认充分的前提下不作为默认。若以后仍有机器复现，再考虑按环境自动切换或迁移 WebView2/Tauri。

复现与验证命令：

```powershell
.\.venv\Scripts\python.exe scripts\diagnose_present_artifacts.py `
  --output .runtime\perf\present-toggle --rendering gpu --scenario toggle --iterations 24

.\.venv\Scripts\python.exe scripts\diagnose_present_artifacts.py `
  --output .runtime\perf\present-mixed --rendering gpu --scenario mixed --iterations 24
```

诊断脚本把窗口置顶但不抢焦点，输入全部在 Qt 内部合成，不移动真实鼠标。报告见 `.runtime/perf/present-toggle/`（修复前）、`present-fixed-toggle/`、`present-fixed-mixed/`（修复后）。

## 0.2.5：路由竞态与呈现滞后

这次问题不是单纯“GPU 开或关”，而是两层竞态叠加：

1. Next App Router 在前一次 `router.push` 尚未提交时继续启动新跳转，旧跳转可能晚于最后一次点击完成；
2. Chromium 已生成新帧后，Qt WebEngine 把 GPU 纹理交给 QWidget/DWM 的实际桌面提交仍可能滞后，CDP 截图正常但用户屏幕仍显示旧区域。

0.2.5 保持 GPU 加速，不增加加载动画。前端只允许一个不可取消的路由跳转在途，并只保留最新点击；Qt 在 SPA URL 提交后的 4 个刷新周期主动推进真实视图重绘。所有本地静态路由在空闲期依次预取，避免首次快速扫过菜单时并发加载页面树。

## 真实鼠标与可见画面对比

测试条件：同一台 Windows 机器、默认 GPU、1440×900 窗口、Win32 可信鼠标输入、8 个侧栏页面循环点击 120 次、间隔 80 ms。除 Chromium screencast 外，测试器还从窗口桌面 DC 连续抓取真实可见像素，因此能发现 Qt/DWM 阶段的残留。

| 指标 | 修改前 0.2.4 | 修改后 0.2.5 | 结果 |
| --- | ---: | ---: | ---: |
| 最终 DOM 与最后点击一致 | 是，16.8 ms | 是，15.8 ms | 一致 |
| 最终真实画面与最后点击一致 | 是 | 是 | 一致 |
| 新旧页面组件混合帧 | 2 | 0 | 消除 |
| DOM 已切换后旧画面最长残留 | 127.6 ms | 65.8 ms | -48.4% |
| 最多落后的路由提交数 | 2 | 1 | 减半 |
| 可见响应中位数 / P90 | 67.7 / 75.8 ms | 72.8 / 82.3 ms | 约增加 5–7 ms |
| Long Task | 0 | 0 | 无回归 |
| 快速阶段网络请求 / 完全重复 | 4 / 1 | 1 / 0 | 降低 |
| 主线程 Task / Script / Layout | 2092 / 673 / 218 ms | 1859 / 619 / 149 ms | 降低 |

可见响应有不到一个采样帧的延迟代价，但最坏旧画面残留缩短约一半，混合花屏消失；没有用软件渲染换稳定性。全软件模式实测会使可见延迟超过 1 秒并产生 Long Task，因此只保留为驱动异常时的兼容入口，不作为默认方案。

额外边界测试覆盖两种“其他页面 → 当前页面”时序：两次点击落在同一帧时，旧目标会在发起前被合并；旧目标已经发起甚至刚提交时，应用会立即导航回最后点击的页面。两种情况下最终页面都与最后点击一致，混合帧 0、Long Task 0。

## 为什么以前测试正常、安装后仍能看到问题

旧诊断只读 CDP `Page.startScreencast`，看到的是 Chromium 内部合成结果；截图所示故障发生在之后的 Qt GPU 纹理导入和 Windows 桌面呈现阶段，因此会漏检。0.2.5 的诊断同时保存 `frames/`（Chromium）和 `screen-frames/`（真实桌面像素），两套证据分别判断。

## Fanqie-novel-Downloader 参考结论

公开仓库只是发布壳，核心代码在私有仓库；可确认它采用 Rust + Tauri v2，Windows 使用系统 WebView2，并以轻量静态 JavaScript 前端打包。它少了一层 Qt WebEngine GPU 纹理到 QWidget 的合成，也没有 Next App Router/RSC 路由树切换，路线天然更轻。

这说明当前技术路线确实放大了问题，但不代表 FastAPI/SQLite 后端需要重写。0.2.5 先修复现有壳和路由根因；若在更多显卡/远程桌面环境中仍复现，下一步应把桌面壳迁到 WebView2/Tauri，继续复用本地 API 与业务前端，而不是继续叠加 Chromium GPU 开关。

## 复现命令与证据

```powershell
.\.venv\Scripts\python.exe scripts\diagnose_rapid_tabs.py `
  --output .runtime\perf\rapid-gpu `
  --data-dir .runtime\perf\userdata-repro `
  --rendering gpu --input win32 --clicks 120 --interval-ms 80 `
  --screen-capture-interval-ms 20 --verify-current-page-last
```

主要报告：

- `.runtime/perf/baseline-screen-gpu-80/report.json`：0.2.4 修改前；
- `.runtime/perf/ab-screen-qt-refresh4-80-v2/report.json`：0.2.5 同条件结果；
- `.runtime/perf/after-current-page-last-gpu80/report.json`：同帧点击当前页的竞态验证；
- `.runtime/perf/after-current-page-last-inflight-gpu80/report.json`：旧跳转发起后的最终意图验证。
