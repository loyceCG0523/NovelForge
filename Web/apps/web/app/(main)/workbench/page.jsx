"use client";

import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import useSWR from "swr";

import AppShellRegion from "@/components/AppShellRegion";
import EmptyState from "@/components/EmptyState";
import TaskExecutionPanel from "@/components/TaskExecutionPanel";
import { apiFetch, isTaskInFlight } from "@/lib/api";
import { useLiveRefresh } from "@/lib/useLiveRefresh";

const productionModes = {
  auto: {
    label: "自动模式",
    short: "连续生成完整作品，达到字数与叙事闭环后结束。",
    detail: "系统会以剧情事件为单位连续规划、生成章节、审校和修复。适合你已经完成初始需求，希望系统尽量不打断地推进到完整作品的情况。",
    confirm: "确认启动自动模式"
  },
  human_in_loop: {
    label: "人审模式",
    short: "先出事件大纲和章节计划，确认后再批量生成章节。",
    detail: "系统每次只生成一个剧情事件的大纲和章节计划，然后暂停等待你编辑或确认。确认后才会批量生成该事件章节。适合你想把控大纲、节奏和关键剧情走向的情况。",
    confirm: "确认启动人审模式"
  },
  tomato_trial: {
    label: "番茄模式",
    short: "按全书节奏推进，在 8-10 万字事件边界暂停。",
    detail: "系统仍然按照你设置的作品总字数来控制整体节奏，但首轮只生成约 8-10 万字，并且一定在剧情事件边界暂停。适合先控制成本、观察作品成绩，后续表现理想再继续扩写。",
    confirm: "确认启动番茄模式"
  },
  test_run: {
    label: "测试模式",
    short: "只生成 1 个剧情事件的章节，完成后暂停供你检查质量。",
    detail: "系统会真实规划并生成 1 个剧情事件对应的章节，完成该事件后自动暂停。适合你想先看本书当前设定下的生成质量、风格和节奏，再决定是否继续批量生成的情况。",
    confirm: "确认启动测试模式"
  }
};

function WorkbenchContent() {
  // 工作台是成熟用户的主页面：它不直接编辑数据，而是聚合展示当前作品状态。
  const router = useRouter();
  const searchParams = useSearchParams();
  const [selectedId, setSelectedId] = useState("");
  // 作品列表与 Dashboard 走 SWR 缓存：切回页面先展示缓存，后台静默刷新。
  const { data: projects = [] } = useSWR("/api/novels");
  const { data: dashboard, mutate: mutateDashboard } = useSWR(
    selectedId ? `/api/novels/${selectedId}/dashboard` : null
  );
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [trackingTaskId, setTrackingTaskId] = useState("");
  const [taskEvents, setTaskEvents] = useState([]);
  const [revisionPatches, setRevisionPatches] = useState([]);
  const [modeMenuOpen, setModeMenuOpen] = useState(false);
  const [pendingProductionMode, setPendingProductionMode] = useState("");
  const [testRunScope, setTestRunScope] = useState("event");
  const modePickerRef = useRef(null);
  const taskEventCursorRef = useRef(0);

  const novelIdFromUrl = searchParams.get("novel");
  const storyEvent = dashboard?.current_story_event;
  const autoRun = dashboard?.current_auto_run;
  const activeAgentTask = useMemo(
    () => (dashboard?.latest_tasks || []).find(
      (task) => ["produce_novel", "generate_story_event", "continue_story_event", "check_story_event_quality"].includes(task.task_type) && isTaskInFlight(task.status)
    ),
    [dashboard]
  );
  const latestActivityTask = useMemo(
    () => (dashboard?.latest_tasks || []).find(
      (task) => ["generate_story_event", "continue_story_event", "check_story_event_quality"].includes(task.task_type)
    ) || (dashboard?.latest_tasks || []).find(
      (task) => ["produce_novel", "generate_story_event", "continue_story_event", "check_story_event_quality"].includes(task.task_type)
    ),
    [dashboard]
  );
  const activityTaskId = storyEvent?.task_id || trackingTaskId || latestActivityTask?.id || "";
  const activityTask = useMemo(
    () => (dashboard?.latest_tasks || []).find((task) => task.id === activityTaskId) || latestActivityTask,
    [activityTaskId, dashboard, latestActivityTask]
  );
  const activityTaskFailure = activityTask?.status === "failed"
    ? `本次生成失败：${activityTask.error_message || "请查看正文 Worker 日志后重试"}`
    : "";
  const dashboardNeedsRefresh = Boolean(
    selectedId
    && (
      trackingTaskId
      || activeAgentTask
      || autoRun?.status === "running"
      || isTaskInFlight(autoRun?.task_status)
    )
  );

  useEffect(() => {
    setTaskEvents([]);
    setRevisionPatches([]);
    taskEventCursorRef.current = 0;
  }, [selectedId, activityTaskId]);

  useLiveRefresh({
    enabled: Boolean(selectedId && activityTaskId),
    intervalMs: 1500,
    refresh: async () => {
      const patchesPromise = apiFetch(`/api/novels/${selectedId}/tasks/${activityTaskId}/revision-patches`);
      const incomingEvents = [];
      let cursor = taskEventCursorRef.current;
      for (let page = 0; page < 20; page += 1) {
        const batch = await apiFetch(
          `/api/novels/${selectedId}/tasks/${activityTaskId}/events?after_sequence=${cursor}&limit=500`
        );
        incomingEvents.push(...batch);
        if (batch.length) cursor = Number(batch.at(-1)?.sequence_no || cursor);
        if (batch.length < 500) break;
      }
      const patches = await patchesPromise;
      if (incomingEvents.length) {
        taskEventCursorRef.current = cursor;
        setTaskEvents((current) => {
          const bySequence = new Map(
            current.map((event) => [Number(event.sequence_no), event])
          );
          incomingEvents.forEach((event) => {
            bySequence.set(Number(event.sequence_no), event);
          });
          return [...bySequence.values()].sort(
            (left, right) => Number(left.sequence_no) - Number(right.sequence_no)
          );
        });
      }
      setRevisionPatches(patches);
    },
    onError: (err) => {
      if (!String(err.message).includes("404")) setError(err.message);
    }
  });

  useEffect(() => {
    // 作品列表由 SWR 供给；这里只负责在 URL 参数或列表变化时挑定当前作品。
    if (!projects.length) return;
    const nextId = novelIdFromUrl || selectedId || projects[0]?.id || "";
    if (nextId && nextId !== selectedId) setSelectedId(nextId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [novelIdFromUrl, projects]);

  useEffect(() => {
    if (!trackingTaskId && activeAgentTask?.id) {
      setTrackingTaskId(activeAgentTask.id);
    }
  }, [activeAgentTask, trackingTaskId]);

  useEffect(() => {
    if (!modeMenuOpen) return undefined;

    function closeOnOutsideClick(event) {
      if (!modePickerRef.current?.contains(event.target)) {
        setModeMenuOpen(false);
      }
    }

    document.addEventListener("mousedown", closeOnOutsideClick);
    return () => document.removeEventListener("mousedown", closeOnOutsideClick);
  }, [modeMenuOpen]);

  useLiveRefresh({
    enabled: dashboardNeedsRefresh,
    intervalMs: 1800,
    refresh: () => mutateDashboard(),
    onError: (err) => setError(err.message)
  });

  useLiveRefresh({
    enabled: Boolean(selectedId && trackingTaskId),
    intervalMs: 2500,
    refresh: async () => {
      const task = await apiFetch(`/api/novels/${selectedId}/tasks/${trackingTaskId}`);
      await mutateDashboard();
      setError("");
      if (task.status === "completed") {
        setMessage(task.task_type === "produce_novel" ? "自动生产任务已完成，工作台数据已刷新" : "剧情事件任务已完成，工作台数据已刷新");
        setTrackingTaskId("");
      } else if (task.status === "waiting") {
        setMessage(task.result_payload?.graph_status || "当前阶段已结束，工作台状态已刷新。");
        setTrackingTaskId("");
      } else if (task.status === "cancelled") {
        setMessage("已暂停：未完成的任务已回收，可重新继续生成。");
        setTrackingTaskId("");
      } else if (task.status === "failed") {
        setError(`Agent 执行失败：${task.error_message || "请查看 Worker 日志"}`);
        setTrackingTaskId("");
      } else {
        setMessage(`${task.result_payload?.graph_status || "自动生产 Agent"}：${task.progress || 0}%`);
      }
    },
    onError: (err) => setError(err.message)
  });

  async function runAgentTask(taskType = "generate_chapter", inputPayload = {}) {
    // 这里创建的是异步 Agent 任务；真正执行由 apps/worker 消费 Redis 队列完成。
    setMessage("");
    setError("");
    try {
      const task = await apiFetch(`/api/novels/${selectedId}/tasks/agent-runs`, {
        method: "POST",
        body: JSON.stringify({
          task_type: taskType,
          input_payload: {
            source: "web-workbench",
            current_chapter_index: dashboard?.novel?.current_chapter_index || 0,
            ...inputPayload
          }
        })
      });
      setMessage(`已创建 Agent 任务：${task.id}，正在等待 Worker 执行`);
      setTrackingTaskId(task.id);
      await mutateDashboard();
    } catch (err) {
      setError(err.message);
    }
  }

  async function retryMemorySync(memoryTaskId) {
    if (!selectedId || !activityTaskId) return;
    setError("");
    try {
      await apiFetch(`/api/novels/${selectedId}/tasks/${activityTaskId}/memory-sync/${memoryTaskId}/retry`, {
        method: "POST"
      });
      setMessage("失败章节的记忆与时间线已重新排队，正文无需重跑。");
    } catch (err) {
      setError(err.message);
    }
  }

  async function retryEventRevision() {
    if (!selectedId || !activityTaskId) return;
    setError("");
    try {
      await apiFetch(`/api/novels/${selectedId}/tasks/${activityTaskId}/event-revision/retry`, {
        method: "POST"
      });
      setMessage("失败章节的事件补丁已单独排队，将复用原修订蓝图。其他章节不会重跑。");
    } catch (err) {
      setError(err.message);
    }
  }

  async function startAutoProduction(productionMode = "auto", selectedTestRunScope = "event") {
    setPendingProductionMode("");
    setModeMenuOpen(false);
    setMessage("");
    setError("");
    try {
      const task = await apiFetch(`/api/novels/${selectedId}/auto-runs/start`, {
        method: "POST",
        body: JSON.stringify({
          max_event_count: 20,
          production_mode: productionMode,
          test_run_scope: selectedTestRunScope
        })
      });
      setTrackingTaskId(task.id);
      setMessage({
        auto: "已启动自动模式：系统会按剧情事件连续推进。",
        human_in_loop: "已启动 Human-in-loop 模式：系统会先生成事件大纲和章节计划，等待你确认。",
        tomato_trial: "已启动番茄模式：系统按全书节奏推进，并在 8-10 万字事件边界暂停。",
        test_run: selectedTestRunScope === "first_chapter" ? "已启动测试模式：系统会生成首章后自动暂停。" : "已启动测试模式：系统会生成 1 个剧情事件的章节，完成后自动暂停。"
      }[productionMode] || "已启动自动生产。");
      await mutateDashboard();
    } catch (err) {
      setError(err.message);
    }
  }

  async function pauseAutoProduction() {
    setMessage("");
    setError("");
    try {
      await apiFetch(`/api/novels/${selectedId}/auto-runs/pause`, { method: "POST" });
      setMessage("已请求暂停。当前单次模型调用返回后会立刻停止，不会继续生成后续章节。");
      await mutateDashboard();
    } catch (err) {
      setError(err.message);
    }
  }

  async function resumeAutoProduction() {
    setMessage("");
    setError("");
    try {
      const task = await apiFetch(`/api/novels/${selectedId}/auto-runs/resume`, { method: "POST" });
      setTrackingTaskId(task.id);
      mutateDashboard((current) => {
        if (!current?.current_auto_run) return current;
        return {
          ...current,
          current_auto_run: {
            ...current.current_auto_run,
            task_id: task.id,
            task_status: task.status || "queued",
            task_progress: task.progress || 0,
            status: "running",
            stage: "queued",
            last_error: ""
          }
        };
      }, { revalidate: false });
      setMessage(task.task_type === "continue_story_event" ? "已开始修正当前章节，正在继续该剧情事件。" : "已继续自动生产，任务已进入队列。");
      await mutateDashboard();
    } catch (err) {
      setError(err.message);
    }
  }

  function renderProductionAction() {
    if (!selectedId) return null;
    if (autoRun?.status === "running") {
      return <button className="secondary-button" onClick={pauseAutoProduction}>暂停自动生产</button>;
    }
    if (autoRun?.status === "paused" && activeAgentTask) {
      return <button className="secondary-button" disabled>正在停止当前步骤…</button>;
    }
    if (autoRun?.payload?.human_loop_status === "waiting_plan_confirmation" && autoRun?.payload?.pending_human_event_id) {
      return (
        <button
          className="primary-button"
          onClick={() => router.push(`/story-events?novel=${selectedId}&event=${autoRun.payload.pending_human_event_id}`)}
        >
          编辑/确认章节计划
        </button>
      );
    }
    if (autoRun?.status === "paused" || autoRun?.status === "failed") {
      const needsWordRevision = autoRun?.payload?.stop_reason === "chapter_word_revision_required";
      const failedQualityRevision = autoRun?.payload?.stop_reason === "event_generation_failed";
      const needsQualityRevision = [
        "test_first_chapter_quality_revision_required",
        "event_quality_revision_required",
        "event_generation_failed"
      ].includes(autoRun?.payload?.stop_reason);
      const resumeLabel = needsWordRevision
        ? "继续修正本章字数"
        : (failedQualityRevision ? "重试失败的事件生成" : (needsQualityRevision ? "继续修复事件质量" : "继续自动生产"));
      return <button className="primary-button" disabled={Boolean(trackingTaskId || activeAgentTask)} onClick={resumeAutoProduction}>{resumeLabel}</button>;
    }
    return (
      <div className="writing-mode-picker" ref={modePickerRef}>
        <button
          className={`writing-mode-trigger ${modeMenuOpen ? "open" : ""}`}
          disabled={Boolean(trackingTaskId || activeAgentTask)}
          type="button"
          aria-haspopup="listbox"
          aria-expanded={modeMenuOpen}
          onClick={() => setModeMenuOpen((current) => !current)}
        >
          <span>
            <em>撰写模式</em>
            <strong>请选择生成方式</strong>
          </span>
          <i aria-hidden="true">
            <svg viewBox="0 0 20 20" focusable="false">
              <path d="M5.5 7.5L10 12l4.5-4.5" />
            </svg>
          </i>
        </button>
        {modeMenuOpen ? (
          <div className="writing-mode-menu" role="listbox">
            {Object.entries(productionModes).map(([mode, item]) => (
              <button
                className="writing-mode-option"
                key={mode}
                type="button"
                role="option"
                onClick={() => {
                  setModeMenuOpen(false);
                  if (mode === "test_run") setTestRunScope("event");
                  setPendingProductionMode(mode);
                }}
              >
                <span className="mode-option-mark" aria-hidden="true" />
                <span className="mode-option-copy">
                  <strong>{item.label}</strong>
                  <small>{item.short}</small>
                </span>
              </button>
            ))}
          </div>
        ) : null}
      </div>
    );
  }

  const pendingMode = pendingProductionMode ? productionModes[pendingProductionMode] : null;
  const isTestRunConfirmation = pendingProductionMode === "test_run";
  const testRunDetail = testRunScope === "first_chapter"
    ? "系统会先规划一个剧情事件，但只生成该事件的首章；首章完成后自动暂停。适合以较低成本检查文风、开场节奏和当前设定下的生成质量。"
    : "系统会先规划一个完整剧情事件，再生成该事件的全部章节；完成该事件后自动暂停。适合检查剧情闭环、章节衔接、文风和整体节奏。";
  const confirmationLabel = isTestRunConfirmation
    ? (testRunScope === "first_chapter" ? "确认生成首章" : "确认生成完整事件")
    : pendingMode?.confirm;
  return (
    <>
    <AppShellRegion
      title="创作工作台"
      actions={
        <div className="workbench-actions">
          <select className="workbench-project-select" value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}
          </select>
          {renderProductionAction()}
        </div>
      }
    />
      {pendingMode ? (
        <div className="modal-backdrop" role="presentation" onClick={() => setPendingProductionMode("")}>
          <div className="confirm-dialog production-mode-dialog" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}>
            <div>
              <div className="panel-title">{pendingMode.label}</div>
              <div className="panel-subtitle">{isTestRunConfirmation ? "启动前请确认本次测试范围。" : "启动前请确认你选择的撰写模式。"}</div>
            </div>
            <div className="production-mode-explain">
              <strong>{isTestRunConfirmation ? "本次测试会怎么工作？" : "这个模式会怎么工作？"}</strong>
              <p>{isTestRunConfirmation ? testRunDetail : pendingMode.detail}</p>
            </div>
            {pendingProductionMode === "test_run" ? (
              <div className="test-run-scope-options" role="radiogroup" aria-label="测试范围">
                <strong>本次测试范围</strong>
                <label className={testRunScope === "event" ? "active" : ""}>
                  <input type="radio" name="test-run-scope" value="event" checked={testRunScope === "event"} onChange={() => setTestRunScope("event")} />
                  <span><b>生成完整事件</b><small>规划并生成一个剧情事件的全部章节后暂停。</small></span>
                </label>
                <label className={testRunScope === "first_chapter" ? "active" : ""}>
                  <input type="radio" name="test-run-scope" value="first_chapter" checked={testRunScope === "first_chapter"} onChange={() => setTestRunScope("first_chapter")} />
                  <span><b>生成首章</b><small>规划剧情事件后只生成首章，再自动暂停。</small></span>
                </label>
              </div>
            ) : null}
            <div className="inline-actions dialog-actions">
              <button type="button" className="secondary-button" onClick={() => setPendingProductionMode("")}>取消</button>
              <button type="button" className="primary-button" onClick={() => startAutoProduction(pendingProductionMode, testRunScope)}>{confirmationLabel}</button>
            </div>
          </div>
        </div>
      ) : null}
      {projects.length === 0 ? (
        <EmptyState
          title="还没有可创作的作品"
          description="先在作品管理里填写起始需求文档，创建作品后再进入工作台生成剧情事件。"
          action={<a className="primary-button" href="/projects">去创建作品</a>}
        />
      ) : (
        <>
          {(message || error || activityTaskFailure) ? (
            <div className={(error || activityTaskFailure) ? "error-box" : "hint-panel workbench-notice"}>
              {error || activityTaskFailure || message}
            </div>
          ) : null}
          <TaskExecutionPanel
            events={taskEvents}
            patches={revisionPatches}
            active={Boolean(activeAgentTask || trackingTaskId)}
            taskStatus={activityTask?.status || ""}
            taskError={activityTask?.error_message || ""}
            onRetryMemory={retryMemorySync}
            onRetryEventRevision={retryEventRevision}
          />
        </>
      )}
    </>
  );
}

export default function WorkbenchPage() {
  return (
    <Suspense fallback={<main className="route-loading">正在载入创作工作台...</main>}>
      <WorkbenchContent />
    </Suspense>
  );
}
