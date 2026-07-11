"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import AppShell from "@/components/AppShell";
import EmptyState from "@/components/EmptyState";
import MetricCard from "@/components/MetricCard";
import ReviewIssuePanel from "@/components/ReviewIssuePanel";
import { apiFetch } from "@/lib/api";

function WorkbenchContent() {
  // 工作台是成熟用户的主页面：它不直接编辑数据，而是聚合展示当前作品状态。
  const router = useRouter();
  const searchParams = useSearchParams();
  const [projects, setProjects] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [dashboard, setDashboard] = useState(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [trackingTaskId, setTrackingTaskId] = useState("");

  const novelIdFromUrl = searchParams.get("novel");
  const selectedProject = useMemo(
    () => projects.find((item) => item.id === selectedId),
    [projects, selectedId]
  );
  const storyEvent = dashboard?.current_story_event;
  const autoRun = dashboard?.current_auto_run;
  const activeAgentTask = useMemo(
    () => (dashboard?.latest_tasks || []).find(
      (task) => ["produce_novel", "generate_story_event", "continue_story_event", "check_story_event_quality"].includes(task.task_type) && ["queued", "running"].includes(task.status)
    ),
    [dashboard]
  );

  async function loadProjects() {
    // 先拿作品列表，再决定当前要展示 URL 指定作品还是默认第一部作品。
    const data = await apiFetch("/api/novels");
    setProjects(data);
    const nextId = novelIdFromUrl || selectedId || data[0]?.id || "";
    setSelectedId(nextId);
    return nextId;
  }

  async function loadDashboard(id) {
    // Dashboard 由后端聚合，避免前端同时请求章节、任务、风险等多个接口。
    if (!id) return;
    setDashboard(await apiFetch(`/api/novels/${id}/dashboard`));
  }

  useEffect(() => {
    loadProjects().then(loadDashboard).catch((err) => setError(err.message));
  }, [novelIdFromUrl]);

  useEffect(() => {
    if (selectedId) loadDashboard(selectedId).catch((err) => setError(err.message));
  }, [selectedId]);

  useEffect(() => {
    if (!trackingTaskId && activeAgentTask?.id) {
      setTrackingTaskId(activeAgentTask.id);
    }
  }, [activeAgentTask, trackingTaskId]);

  useEffect(() => {
    if (!selectedId || !trackingTaskId) return undefined;

    let stopped = false;
    async function pollTask() {
      try {
        const task = await apiFetch(`/api/novels/${selectedId}/tasks/${trackingTaskId}`);
        await loadDashboard(selectedId);
        if (stopped) return;

        if (task.status === "completed") {
          setMessage(task.task_type === "produce_novel" ? "自动生产任务已完成，工作台数据已刷新" : "剧情事件任务已完成，工作台数据已刷新");
          setTrackingTaskId("");
        } else if (task.status === "failed") {
          setError(`Agent 执行失败：${task.error_message || "请查看 Worker 日志"}`);
          setTrackingTaskId("");
        } else {
          setMessage(`${task.result_payload?.graph_status || "自动生产 Agent"}：${task.progress || 0}%`);
        }
      } catch (err) {
        if (!stopped) {
          setError(err.message);
          setTrackingTaskId("");
        }
      }
    }

    pollTask();
    const timer = window.setInterval(pollTask, 2500);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [selectedId, trackingTaskId]);

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
      await loadDashboard(selectedId);
    } catch (err) {
      setError(err.message);
    }
  }

  async function startAutoProduction() {
    setMessage("");
    setError("");
    try {
      const task = await apiFetch(`/api/novels/${selectedId}/auto-runs/start`, {
        method: "POST",
        body: JSON.stringify({
          chapter_count_per_event: 8,
          max_event_count: 20
        })
      });
      setTrackingTaskId(task.id);
      setMessage("已启动整本书自动生产，系统会按剧情事件连续推进。");
      await loadDashboard(selectedId);
    } catch (err) {
      setError(err.message);
    }
  }

  async function pauseAutoProduction() {
    setMessage("");
    setError("");
    try {
      await apiFetch(`/api/novels/${selectedId}/auto-runs/pause`, { method: "POST" });
      setMessage("已请求暂停，系统会在当前剧情事件完成后停下。");
      await loadDashboard(selectedId);
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
      setMessage("已继续整本书自动生产。");
      await loadDashboard(selectedId);
    } catch (err) {
      setError(err.message);
    }
  }

  function renderProductionAction() {
    if (!selectedId) return null;
    if (autoRun?.status === "running" || activeAgentTask?.task_type === "produce_novel") {
      return <button className="secondary-button" onClick={pauseAutoProduction}>暂停自动生产</button>;
    }
    if (autoRun?.status === "paused" || autoRun?.status === "failed") {
      return <button className="primary-button" disabled={Boolean(trackingTaskId || activeAgentTask)} onClick={resumeAutoProduction}>继续自动生产</button>;
    }
    return <button className="primary-button" disabled={Boolean(trackingTaskId || activeAgentTask)} onClick={startAutoProduction}>开始自动生成</button>;
  }

  return (
    <AppShell
      title="创作工作台"
      subtitle="聚焦当前剧情事件、系统审校状态和一键生成操作"
      actions={
        <>
          <select className="secondary-button" value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}
          </select>
          <button className="secondary-button" disabled={!selectedId} onClick={() => router.push(`/chapters?novel=${selectedId}`)}>章节管理</button>
          <button className="secondary-button" disabled={!selectedId} onClick={() => router.push(`/story-events?novel=${selectedId}`)}>剧情事件</button>
          {renderProductionAction()}
        </>
      }
    >
      {projects.length === 0 ? (
        <EmptyState
          title="还没有可创作的作品"
          description="先在作品管理里填写起始需求文档，创建作品后再进入工作台生成剧情事件。"
          action={<a className="primary-button" href="/projects">去创建作品</a>}
        />
      ) : (
        <>
          <section className="grid-4">
            <MetricCard label="当前作品" value={dashboard?.novel?.title || selectedProject?.title || "-"} note={dashboard?.novel?.genre || selectedProject?.genre || "未分类"} />
            <MetricCard label="章节数量" value={dashboard?.counts?.chapters ?? 0} note={`当前第 ${dashboard?.novel?.current_chapter_index ?? 0} 章`} tone="green" />
            <MetricCard label="正文总字数" value={`${(dashboard?.counts?.words ?? 0).toLocaleString()} 字`} note={`目标 ${(dashboard?.novel?.target_words ?? 0).toLocaleString()} 字`} tone="purple" />
            <MetricCard label="审校记录" value={dashboard?.review_issues?.length ?? 0} note="系统自动记录" tone="green" />
          </section>

          {(message || error) ? <div className={error ? "error-box" : "hint-panel"}>{error || message}</div> : null}

          <section className="panel production-panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">自动生产总控</div>
                <div className="panel-subtitle">用户完成起始需求后，系统按剧情事件自动生成、审校、修复并继续推进。</div>
              </div>
              <span className={`tag ${autoRun?.status === "running" ? "yellow" : autoRun?.status === "completed" ? "green" : "purple"}`}>
                {autoRun?.status || "未启动"}
              </span>
            </div>
            <div className="panel-body production-body">
              <div className="production-main">
                <div>
                  <span className="tag purple">NovelProductionAgent</span>
                  <h2>{autoRun?.stage || "等待开始自动生产"}</h2>
                  <p>{autoRun?.last_error || "系统会以一个完整剧情事件为单位推进，每轮生成多章并自动完成审校闭环。"}</p>
                </div>
                <div className="story-event-progress">
                  <strong>{(autoRun?.current_words || dashboard?.counts?.words || 0).toLocaleString()} / {(autoRun?.target_words || dashboard?.novel?.target_words || 0).toLocaleString()}</strong>
                  <span>正文总字数</span>
                  <div className="progress">
                    <span style={{ width: `${autoRun?.word_progress ?? Math.min(100, Math.round(((dashboard?.counts?.words || 0) / Math.max(dashboard?.novel?.target_words || 1, 1)) * 100))}%` }} />
                  </div>
                  <em>{activeAgentTask?.task_type === "produce_novel" ? activeAgentTask.status : "按事件边界推进"}</em>
                </div>
              </div>
              <div className="story-event-stats">
                <div><span>生产事件</span><strong>{autoRun?.produced_event_count || 0} / {autoRun?.max_event_count || 20}</strong></div>
                <div><span>当前阶段</span><strong>{autoRun?.stage || "未启动"}</strong></div>
                <div><span>生产状态</span><strong>{autoRun?.status || "idle"}</strong></div>
                <div><span>最近任务</span><strong>{activeAgentTask?.task_type || "无"}</strong></div>
              </div>
            </div>
          </section>

          <section className="panel story-event-panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">当前剧情事件</div>
                <div className="panel-subtitle">围绕一个完整大事件生成多章，并展示 LangGraph 执行进度。</div>
              </div>
              {storyEvent ? <span className={`tag ${storyEvent.task_status === "running" ? "yellow" : "green"}`}>{storyEvent.task_status}</span> : null}
              {storyEvent?.id ? <button className="secondary-button" onClick={() => router.push(`/story-events?novel=${selectedId}&event=${storyEvent.id}`)}>查看事件</button> : null}
            </div>
            {storyEvent ? (
              <div className="panel-body story-event-body">
                <div className="story-event-main">
                  <div>
                    <span className="tag purple">StoryPlanningAgent</span>
                    <h2>{storyEvent.event_title || "剧情事件生成中"}</h2>
                    <p>{storyEvent.event_goal || "事件目标正在规划中。"}</p>
                  </div>
                  <div className="story-event-progress">
                    <strong>{storyEvent.generated_chapter_count || 0} / {storyEvent.planned_chapter_count || 0}</strong>
                    <span>已生成章节</span>
                    <div className="progress">
                      <span style={{ width: `${storyEvent.planned_chapter_count ? Math.round((storyEvent.generated_chapter_count / storyEvent.planned_chapter_count) * 100) : storyEvent.progress || 4}%` }} />
                    </div>
                    <em>{storyEvent.graph_status || "等待 Graph 状态更新"}</em>
                  </div>
                </div>
                <div className="story-event-stats">
                  <div><span>核心冲突</span><strong>{storyEvent.core_conflict || "未生成"}</strong></div>
                  <div><span>章节范围</span><strong>{storyEvent.chapter_range?.start ? `第 ${storyEvent.chapter_range.start}-${storyEvent.chapter_range.end} 章` : "生成中"}</strong></div>
                  <div><span>自动修复</span><strong>{storyEvent.auto_repair_count || 0} 次</strong></div>
                  <div><span>后续处理</span><strong>{storyEvent.remaining_open_risks || 0} 条</strong></div>
                </div>
                <div className="event-plan-list">
                  {(storyEvent.chapter_plans || []).map((plan) => {
                    const generated = (storyEvent.generated_chapters || []).find((chapter) => chapter.chapter_index === plan.chapter_index);
                    return (
                      <article className={generated ? "event-plan-item done" : "event-plan-item"} key={`${storyEvent.task_id}-${plan.chapter_index}`}>
                        <div>
                          <span>第 {plan.chapter_index} 章 · {plan.function || "推进"}</span>
                          <strong>{generated?.title || plan.title || "未命名章节"}</strong>
                          <p>{plan.core_event || "章节核心事件待生成。"}</p>
                        </div>
                        <em>{generated ? `${generated.word_count || 0} 字` : "待生成"}</em>
                      </article>
                    );
                  })}
                </div>
                {storyEvent.next_event_hook ? <div className="hint-panel">下一事件钩子：{storyEvent.next_event_hook}</div> : null}
              </div>
            ) : (
              <div className="panel-body">
                <EmptyState title="暂无剧情事件" description="点击“开始自动生成”，系统会规划一个 8 章左右的闭环大事件并连续生成。" />
              </div>
            )}
          </section>

          <ReviewIssuePanel
            title="系统审校记录"
            subtitle="系统发现质量事项后会自动修复或转入后续处理，这里只展示过程记录。"
            issues={dashboard?.review_issues || dashboard?.open_review_issues || []}
            emptyTitle="暂无审校记录"
            emptyDescription="章节生成后的连续性审校和自动修复历史会显示在这里。"
          />
        </>
      )}
    </AppShell>
  );
}

export default function WorkbenchPage() {
  return (
    <Suspense fallback={<main className="route-loading">正在载入创作工作台...</main>}>
      <WorkbenchContent />
    </Suspense>
  );
}
