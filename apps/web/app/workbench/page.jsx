"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import AppShell from "@/components/AppShell";
import EmptyState from "@/components/EmptyState";
import MetricCard from "@/components/MetricCard";
import { apiFetch } from "@/lib/api";

function WorkbenchContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [projects, setProjects] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [dashboard, setDashboard] = useState(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const novelIdFromUrl = searchParams.get("novel");
  const selectedProject = useMemo(
    () => projects.find((item) => item.id === selectedId),
    [projects, selectedId]
  );
  const brief = dashboard?.novel?.brief || selectedProject?.brief || {};

  async function loadProjects() {
    const data = await apiFetch("/api/novels");
    setProjects(data);
    const nextId = novelIdFromUrl || selectedId || data[0]?.id || "";
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

  async function runAgentTask(taskType = "generate_chapter") {
    setMessage("");
    setError("");
    try {
      const task = await apiFetch(`/api/novels/${selectedId}/tasks/agent-runs`, {
        method: "POST",
        body: JSON.stringify({
          task_type: taskType,
          input_payload: {
            source: "web-workbench",
            current_chapter_index: dashboard?.novel?.current_chapter_index || 0
          }
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
      subtitle="基于真实作品数据展示章节、起始需求、风险和 Agent 任务"
      actions={
        <>
          <select className="secondary-button" value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}
          </select>
          <button className="secondary-button" disabled={!selectedId} onClick={() => router.push(`/chapters?novel=${selectedId}`)}>章节管理</button>
          <button className="primary-button" disabled={!selectedId} onClick={() => runAgentTask("generate_chapter")}>启动章节 Agent</button>
        </>
      }
    >
      {projects.length === 0 ? (
        <EmptyState
          title="还没有可创作的作品"
          description="先填写起始需求文档创建作品，工作台会自动读取章节、风险、任务和结构化数据。"
          action={<a className="primary-button" href="/projects">去创建作品</a>}
        />
      ) : (
        <>
          <section className="grid-5">
            <MetricCard label="当前作品" value={dashboard?.novel?.title || selectedProject?.title || "-"} note={dashboard?.novel?.genre || selectedProject?.genre || "未分类"} />
            <MetricCard label="章节数量" value={dashboard?.counts?.chapters ?? 0} note={`当前第 ${dashboard?.novel?.current_chapter_index ?? 0} 章`} tone="green" />
            <MetricCard label="正文总字数" value={`${(dashboard?.counts?.words ?? 0).toLocaleString()} 字`} note={`目标 ${(dashboard?.novel?.target_words ?? 0).toLocaleString()} 字`} tone="purple" />
            <MetricCard label="伏笔记录" value={dashboard?.counts?.foreshadowing ?? 0} note="埋设与回收追踪" tone="yellow" />
            <MetricCard label="开放风险" value={dashboard?.counts?.open_review_issues ?? 0} note="待处理审校项" tone="red" />
          </section>

          {(message || error) ? <div className={error ? "error-box" : "hint-panel"}>{error || message}</div> : null}

          <section className="grid-2">
            <section className="panel">
              <div className="panel-header">
                <div><div className="panel-title">起始需求文档</div><div className="panel-subtitle">后续规划、章节生成和审校都会读取这些结构化约束。</div></div>
                <button className="secondary-button" onClick={() => router.push("/projects")}>查看作品</button>
              </div>
              <div className="panel-body brief-grid">
                <div><span>一句话需求</span><strong>{dashboard?.novel?.premise || "暂无"}</strong></div>
                <div><span>作品类型</span><strong>{brief.work_type || "未设置"}</strong></div>
                <div><span>卖点</span><strong>{brief.selling_points || "未设置"}</strong></div>
                <div><span>主角</span><strong>{brief.protagonist || "未设置"}</strong></div>
                <div><span>世界观</span><strong>{brief.worldview || "未设置"}</strong></div>
                <div><span>自动化策略</span><strong>{brief.automation_strategy || "未设置"}</strong></div>
              </div>
            </section>

            <section className="panel">
              <div className="panel-header">
                <div><div className="panel-title">最新章节</div><div className="panel-subtitle">来自章节管理页面的真实数据。</div></div>
                <button className="primary-button" onClick={() => router.push(`/chapters?novel=${selectedId}`)}>管理章节</button>
              </div>
              <div className="panel-body stack-list">
                {dashboard?.latest_chapters?.length ? dashboard.latest_chapters.map((chapter) => (
                  <article className="compact-card" key={chapter.id}>
                    <div>
                      <span>第 {chapter.chapter_index} 章 · {chapter.status}</span>
                      <strong>{chapter.title || "未命名章节"}</strong>
                      <p>{chapter.summary || "暂无章节摘要。"}</p>
                    </div>
                    <em>{chapter.word_count} 字</em>
                  </article>
                )) : (
                  <EmptyState title="暂无章节" description="先创建章节，工作台会同步展示最新章节状态。" action={<button className="primary-button" onClick={() => router.push(`/chapters?novel=${selectedId}`)}>新建章节</button>} />
                )}
              </div>
            </section>
          </section>

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
              {dashboard?.latest_tasks?.length ? null : <article className="kanban-card"><h2>等待任务</h2><p>点击“启动章节 Agent”创建占位任务。</p></article>}
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
                <p>负责章节规划、上下文组装、任务派发和结果回写。</p>
                <button className="secondary-button" onClick={() => runAgentTask("plan_novel")}>创建规划任务</button>
              </article>
              <article className="kanban-card">
                <span className="tag">预留</span>
                <h2>Memory Sync</h2>
                <p>每章后抽取人物、地点、道具、关系和时间线变更。</p>
                <button className="secondary-button" onClick={() => runAgentTask("sync_memory")}>创建记忆任务</button>
              </article>
              <article className="kanban-card">
                <span className="tag yellow">预留</span>
                <h2>Style Auditor</h2>
                <p>清理 AI 常用句式、模板化段落和解释性独白。</p>
                <button className="secondary-button" onClick={() => runAgentTask("anti_ai_review")}>创建审校任务</button>
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
