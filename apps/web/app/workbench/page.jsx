"use client";

import { useEffect, useMemo, useState } from "react";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";

import AppShell from "@/components/AppShell";
import EmptyState from "@/components/EmptyState";
import MetricCard from "@/components/MetricCard";
import { apiFetch } from "@/lib/api";

function WorkbenchContent() {
  const searchParams = useSearchParams();
  const [projects, setProjects] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [dashboard, setDashboard] = useState(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const novelIdFromUrl = searchParams.get("novel");

  async function loadProjects() {
    const data = await apiFetch("/api/novels");
    setProjects(data);
    const nextId = novelIdFromUrl || data[0]?.id || "";
    setSelectedId(nextId);
    return nextId;
  }

  async function loadDashboard(id) {
    if (!id) return;
    setDashboard(await apiFetch(`/api/novels/${id}/dashboard`));
  }

  useEffect(() => {
    loadProjects().then(loadDashboard).catch((err) => setError(err.message));
  }, [novelIdFromUrl]);

  useEffect(() => {
    if (selectedId) loadDashboard(selectedId).catch((err) => setError(err.message));
  }, [selectedId]);

  const selectedProject = useMemo(() => projects.find((item) => item.id === selectedId), [projects, selectedId]);

  async function runAgentTask() {
    setMessage("");
    setError("");
    try {
      const task = await apiFetch(`/api/novels/${selectedId}/tasks/agent-runs`, {
        method: "POST",
        body: JSON.stringify({
          task_type: "generate_chapter",
          input_payload: { mode: "reserved", source: "web-workbench" }
        })
      });
      setMessage(`已创建 Agent 预留任务：${task.id}`);
      await loadDashboard(selectedId);
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <AppShell
      title="创作工作台"
      subtitle="实时查看作品状态、风险提醒和自动生产任务"
      actions={
        <>
          <select className="secondary-button" value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}
          </select>
          <button className="primary-button" disabled={!selectedId} onClick={runAgentTask}>启动 Agent 任务</button>
        </>
      }
    >
      {projects.length === 0 ? (
        <EmptyState
          title="还没有可创作的作品"
          description="先到作品管理创建一部小说，然后这里会展示章节、记忆、伏笔、审校风险和任务队列。"
          action={<a className="primary-button" href="/projects">去创建作品</a>}
        />
      ) : (
        <>
          <section className="grid-5">
            <MetricCard label="当前作品" value={selectedProject?.title || "-"} note={selectedProject?.genre || "未分类"} />
            <MetricCard label="章节数量" value={dashboard?.counts?.chapters ?? 0} note="已创建章节" tone="green" />
            <MetricCard label="结构化记忆" value={dashboard?.counts?.memory_items ?? 0} note="人物、地点、道具等" tone="purple" />
            <MetricCard label="伏笔记录" value={dashboard?.counts?.foreshadowing ?? 0} note="埋设与回收追踪" tone="yellow" />
            <MetricCard label="开放风险" value={dashboard?.counts?.open_review_issues ?? 0} note="待处理审校项" tone="red" />
          </section>

          {(message || error) ? <div className={error ? "error-box" : "hint-panel"}>{error || message}</div> : null}

          <section className="kanban">
            <div className="kanban-column">
              <div className="kanban-title">生产队列 <span className="tag">{dashboard?.counts?.active_tasks ?? 0}</span></div>
              {(dashboard?.latest_tasks || []).map((task) => (
                <article className="kanban-card" key={task.id}>
                  <span className="tag green">{task.status}</span>
                  <h2>{task.task_type}</h2>
                  <p>任务 ID：{task.id}</p>
                  <div className="progress"><span style={{ width: `${task.progress || 4}%` }} /></div>
                </article>
              ))}
              {dashboard?.latest_tasks?.length ? null : <article className="kanban-card"><h2>等待任务</h2><p>点击“启动 Agent 任务”创建占位任务。</p></article>}
            </div>

            <div className="kanban-column">
              <div className="kanban-title">风险提醒 <span className="tag red">{dashboard?.open_review_issues?.length ?? 0}</span></div>
              {(dashboard?.open_review_issues || []).map((issue) => (
                <article className="kanban-card" key={issue.id}>
                  <span className="tag red">{issue.severity}</span>
                  <h2>{issue.issue_type}</h2>
                  <p>{issue.message}</p>
                </article>
              ))}
              {dashboard?.open_review_issues?.length ? null : <article className="kanban-card"><h2>暂无开放风险</h2><p>后续反 AI 审校和连续性审校会把风险写入这里。</p></article>}
            </div>

            <div className="kanban-column">
              <div className="kanban-title">核心功能预留 <span className="tag purple">Agent</span></div>
              <article className="kanban-card">
                <span className="tag purple">预留</span>
                <h2>Creative Director</h2>
                <p>后续接入 LangGraph，负责章节规划、上下文组装、任务派发和结果回写。</p>
              </article>
              <article className="kanban-card">
                <span className="tag">预留</span>
                <h2>Memory Sync</h2>
                <p>生成后自动抽取人物、地点、道具、关系和时间线变更。</p>
              </article>
              <article className="kanban-card">
                <span className="tag yellow">预留</span>
                <h2>Style Auditor</h2>
                <p>清理 AI 常用句式、模板化段落和解释性独白。</p>
              </article>
            </div>
          </section>
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
