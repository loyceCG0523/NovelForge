"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import useSWR from "swr";

import AppShellRegion from "@/components/AppShellRegion";
import EmptyState from "@/components/EmptyState";
import ReviewIssuePanel from "@/components/ReviewIssuePanel";
import { apiFetch, isTaskInFlight, isTaskSettled } from "@/lib/api";
import { useLiveRefresh } from "@/lib/useLiveRefresh";

function statusText(status) {
  return {
    planned: "待生成",
    generating: "生成中",
    generated: "已生成",
    revised: "已修订",
    completed: "已完成",
    failed: "失败",
    done: "已完成",
    open: "系统处理中",
    system_deferred: "后续自动处理",
    quality_note: "质量建议",
    resolved: "已解决",
    ignored: "已忽略"
  }[status] || status || "未知";
}

function scoreTone(score) {
  if (score >= 85) return "green";
  if (score >= 70) return "yellow";
  return "red";
}

function StoryEventsContent() {
  // 剧情事件页只处理事件级信息：查看与编辑计划、事件审校、重跑单章或从某章继续。
  const router = useRouter();
  const searchParams = useSearchParams();
  const [selectedNovelId, setSelectedNovelId] = useState("");
  const [selectedEventId, setSelectedEventId] = useState("");
  // 作品/事件列表/事件详情走 SWR 缓存：切回页面秒显缓存，后台静默刷新。
  const { data: projects = [] } = useSWR("/api/novels");
  const { data: events = [], mutate: mutateEvents } = useSWR(
    selectedNovelId ? `/api/novels/${selectedNovelId}/story-events` : null
  );
  const { data: eventDetail = null, mutate: mutateEventDetail } = useSWR(
    selectedNovelId && selectedEventId
      ? `/api/novels/${selectedNovelId}/story-events/${selectedEventId}`
      : null
  );
  const [trackingTaskId, setTrackingTaskId] = useState("");
  const [busyPlanId, setBusyPlanId] = useState("");
  const [planDraft, setPlanDraft] = useState(null);
  const [savingPlan, setSavingPlan] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const selectedProject = useMemo(
    () => projects.find((project) => project.id === selectedNovelId),
    [projects, selectedNovelId]
  );

  useEffect(() => {
    // 作品列表由 SWR 供给；这里只负责按 URL 参数或当前选择挑定作品。
    if (!projects.length) return;
    const nextNovelId = searchParams.get("novel") || selectedNovelId || projects[0]?.id || "";
    if (nextNovelId && nextNovelId !== selectedNovelId) setSelectedNovelId(nextNovelId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, projects]);

  useEffect(() => {
    // 事件列表变化时校正选中事件：URL 指定 > 保持当前（需仍存在）> 默认第一个。
    if (!selectedNovelId || !events.length) {
      if (!events.length && selectedEventId) setSelectedEventId("");
      return;
    }
    const eventFromUrl = searchParams.get("event");
    const nextEventId =
      (eventFromUrl && events.some((item) => item.id === eventFromUrl) && eventFromUrl)
      || (events.some((item) => item.id === selectedEventId) && selectedEventId)
      || events[0]?.id
      || "";
    if (nextEventId !== selectedEventId) setSelectedEventId(nextEventId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events, selectedNovelId]);

  useEffect(() => {
    if (!eventDetail) {
      setPlanDraft(null);
      return;
    }
    setPlanDraft({
      title: eventDetail.title || "",
      goal: eventDetail.goal || "",
      core_conflict: eventDetail.core_conflict || "",
      next_event_hook: eventDetail.next_event_hook || "",
      completion_criteria: eventDetail.completion_criteria || [],
      chapter_plans: (eventDetail.plans || []).map((plan) => ({
        id: plan.id,
        title: plan.title || "",
        function: plan.function || "",
        core_event: plan.core_event || "",
        ending_hook: plan.ending_hook || ""
      }))
    });
  }, [eventDetail]);

  useEffect(() => {
    if (
      !trackingTaskId
      && eventDetail?.task_id
      && isTaskInFlight(eventDetail.task_status)
    ) {
      setTrackingTaskId(eventDetail.task_id);
    }
  }, [eventDetail?.task_id, eventDetail?.task_status, trackingTaskId]);

  useLiveRefresh({
    enabled: Boolean(selectedNovelId && selectedEventId && trackingTaskId),
    intervalMs: 2500,
    refresh: async () => {
      const task = await apiFetch(`/api/novels/${selectedNovelId}/tasks/${trackingTaskId}`);
      if (isTaskSettled(task.status)) {
        await mutateEvents();
        await mutateEventDetail();
      } else {
        await mutateEventDetail();
      }
      setError("");
      if (task.status === "completed") {
        setMessage("剧情事件任务已完成，事件列表和详情已刷新");
        setTrackingTaskId("");
        setBusyPlanId("");
      } else if (task.status === "waiting") {
        setMessage(task.result_payload?.graph_status || "剧情事件已进入待处理状态，详情已刷新。");
        setTrackingTaskId("");
        setBusyPlanId("");
      } else if (task.status === "cancelled") {
        setMessage("剧情事件任务已取消，当前详情已刷新。");
        setTrackingTaskId("");
        setBusyPlanId("");
      } else if (task.status === "failed") {
        setError(`剧情事件任务失败：${task.error_message || "请查看 Worker 日志"}`);
        setTrackingTaskId("");
        setBusyPlanId("");
      } else {
        setMessage(`${task.result_payload?.graph_status || "剧情事件任务"}：${task.progress || 0}%`);
      }
    },
    onError: (err) => setError(err.message)
  });

  async function rerunPlan(plan) {
    setMessage("");
    setError("");
    setBusyPlanId(plan.id);
    try {
      const task = await apiFetch(`/api/novels/${selectedNovelId}/story-events/${selectedEventId}/plans/${plan.id}/rerun`, {
        method: "POST"
      });
      setTrackingTaskId(task.id);
      setMessage(`已创建第 ${plan.chapter_index} 章重跑任务`);
    } catch (err) {
      setError(err.message);
      setBusyPlanId("");
    }
  }

  async function continueFrom(plan) {
    setMessage("");
    setError("");
    setBusyPlanId(plan.id);
    try {
      const task = await apiFetch(`/api/novels/${selectedNovelId}/story-events/${selectedEventId}/continue?from_chapter_index=${plan.chapter_index}`, {
        method: "POST"
      });
      setTrackingTaskId(task.id);
      setMessage(`已创建从第 ${plan.chapter_index} 章继续生成的任务`);
    } catch (err) {
      setError(err.message);
      setBusyPlanId("");
    }
  }

  async function checkQuality() {
    if (!selectedNovelId || !selectedEventId) return;
    setMessage("");
    setError("");
    try {
      const task = await apiFetch(`/api/novels/${selectedNovelId}/story-events/${selectedEventId}/quality-check`, {
        method: "POST"
      });
      setTrackingTaskId(task.id);
      setMessage("已创建事件级质量审校任务");
    } catch (err) {
      setError(err.message);
    }
  }

  function updatePlanDraft(field, value) {
    setPlanDraft((current) => ({ ...(current || {}), [field]: value }));
  }

  function updateChapterPlanDraft(planId, field, value) {
    setPlanDraft((current) => ({
      ...(current || {}),
      chapter_plans: (current?.chapter_plans || []).map((plan) => (
        plan.id === planId ? { ...plan, [field]: value } : plan
      ))
    }));
  }

  async function savePlanDraft() {
    if (!selectedNovelId || !selectedEventId || !planDraft) return null;
    setMessage("");
    setError("");
    setSavingPlan(true);
    try {
      const updated = await apiFetch(`/api/novels/${selectedNovelId}/story-events/${selectedEventId}/plan`, {
        method: "PATCH",
        body: JSON.stringify(planDraft)
      });
      mutateEventDetail(updated, { revalidate: false });
      setMessage("章节计划已保存");
      return updated;
    } catch (err) {
      setError(err.message);
      return null;
    } finally {
      setSavingPlan(false);
    }
  }

  async function confirmPlanAndGenerate() {
    if (!selectedNovelId || !selectedEventId) return;
    const saved = await savePlanDraft();
    if (!saved) return;
    setMessage("");
    setError("");
    try {
      const task = await apiFetch(`/api/novels/${selectedNovelId}/story-events/${selectedEventId}/confirm`, {
        method: "POST"
      });
      setTrackingTaskId(task.id);
      setMessage("已确认章节计划，开始批量生成本事件章节");
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <>
    <AppShellRegion
      title="剧情事件"
      actions={
        <>
          <select className="secondary-button" value={selectedNovelId} onChange={(event) => setSelectedNovelId(event.target.value)}>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}
          </select>
          <button className="secondary-button" disabled={!selectedNovelId} onClick={() => router.push(`/workbench?novel=${selectedNovelId}`)}>返回工作台</button>
          <button className="secondary-button" disabled={!selectedEventId || Boolean(trackingTaskId)} onClick={checkQuality}>重新审校事件</button>
          <button className="primary-button" disabled={!selectedNovelId} onClick={() => router.push(`/chapters?novel=${selectedNovelId}`)}>章节管理</button>
        </>
      }
    />
      {projects.length === 0 ? (
        <EmptyState title="还没有作品" description="请先创建作品，再生成剧情事件。" action={<a className="primary-button" href="/projects">去创建作品</a>} />
      ) : events.length === 0 ? (
        <EmptyState title="暂无剧情事件" description="先在工作台点击“开始自动生成”，系统会按作品设置规划并生成 4-12 章闭环剧情。" action={<button className="primary-button" onClick={() => router.push(`/workbench?novel=${selectedNovelId}`)}>去工作台生成</button>} />
      ) : (
        <section className="event-console">
          <aside className="panel event-sidebar">
            <div className="panel-header">
              <div>
                <div className="panel-title">{selectedProject?.title || "当前作品"}</div>
                <div className="panel-subtitle">共 {events.length} 个剧情事件。</div>
              </div>
            </div>
            <div className="panel-body stack-list">
              {events.map((item) => (
                <button
                  className={`event-row ${item.id === selectedEventId ? "active" : ""}`}
                  key={item.id}
                  onClick={() => setSelectedEventId(item.id)}
                >
                  <span>{statusText(item.status)} · 第 {item.start_chapter_index || "-"}-{item.end_chapter_index || "-"} 章</span>
                  <strong>{item.title || "未命名事件"}</strong>
                  <small>{item.generated_chapter_count || 0}/{item.planned_chapter_count || 0} 章 · 后续处理 {item.remaining_open_risks || 0} 条</small>
                </button>
              ))}
            </div>
          </aside>

          <div className="event-detail-stack">
            {(message || error) ? <div className={error ? "error-box" : "hint-panel"}>{error || message}</div> : null}
            {eventDetail ? (
              <>
                <section className="panel event-hero-panel">
                  <div className="panel-body event-hero">
                    <div>
                      <span className="tag purple">StoryPlanningAgent</span>
                      {eventDetail.status === "planned" && planDraft ? (
                        <div className="field-stack">
                          <div className="field"><label>事件标题</label><input value={planDraft.title} onChange={(event) => updatePlanDraft("title", event.target.value)} /></div>
                          <div className="field"><label>事件目标</label><textarea value={planDraft.goal} onChange={(event) => updatePlanDraft("goal", event.target.value)} rows={3} /></div>
                          <div className="field"><label>核心冲突</label><textarea value={planDraft.core_conflict} onChange={(event) => updatePlanDraft("core_conflict", event.target.value)} rows={2} /></div>
                        </div>
                      ) : (
                        <>
                          <h2>{eventDetail.title || "未命名事件"}</h2>
                          <p>{eventDetail.goal || "暂无事件目标。"}</p>
                        </>
                      )}
                    </div>
                    <div className="story-event-progress">
                      <strong>{eventDetail.generated_chapter_count || 0} / {eventDetail.planned_chapter_count || 0}</strong>
                      <span>已生成章节</span>
                      <div className="progress">
                        <span style={{ width: `${eventDetail.planned_chapter_count ? Math.round((eventDetail.generated_chapter_count / eventDetail.planned_chapter_count) * 100) : 4}%` }} />
                      </div>
                      <em>{eventDetail.graph_status || statusText(eventDetail.status)}</em>
                    </div>
                  </div>
                  {eventDetail.status === "planned" ? (
                    <div className="inline-actions event-hero-actions">
                      <button className="secondary-button" disabled={savingPlan || Boolean(trackingTaskId)} onClick={savePlanDraft}>
                        {savingPlan ? "保存中" : "保存计划"}
                      </button>
                      <button className="primary-button" disabled={savingPlan || Boolean(trackingTaskId)} onClick={confirmPlanAndGenerate}>
                        确认并生成本事件章节
                      </button>
                    </div>
                  ) : null}
                  <div className="event-facts">
                    <div><span>核心冲突</span><strong>{eventDetail.core_conflict || "未生成"}</strong></div>
                    <div><span>章节范围</span><strong>第 {eventDetail.start_chapter_index || "-"}-{eventDetail.end_chapter_index || "-"} 章</strong></div>
                    <div><span>自动修复</span><strong>{eventDetail.auto_repair_count || 0} 次</strong></div>
                    <div><span>后续处理</span><strong>{eventDetail.remaining_open_risks || 0} 条</strong></div>
                  </div>
                  {eventDetail.completion_criteria?.length ? (
                    <div className="event-criteria">
                      {eventDetail.completion_criteria.map((item, index) => <span key={`${item}-${index}`}>{item}</span>)}
                    </div>
                  ) : null}
                  {eventDetail.next_event_hook ? <div className="hint-panel">下一事件钩子：{eventDetail.next_event_hook}</div> : null}
                </section>

                <section className="panel event-quality-panel">
                  <div className="panel-header">
                    <div>
                      <div className="panel-title">事件级质量审校</div>
                    </div>
                    <span className={`tag ${scoreTone(eventDetail.quality_report?.scores?.overall || 0)}`}>
                      总分 {eventDetail.quality_report?.scores?.overall ?? "-"}
                    </span>
                  </div>
                  <div className="panel-body">
                    {eventDetail.quality_report?.scores ? (
                      <>
                        <div className="quality-score-grid">
                          {[
                            ["闭环", "closure"],
                            ["节奏", "pacing"],
                            ["人物推进", "character_arc"],
                            ["伏笔推进", "foreshadowing"],
                            ["综合", "overall"]
                          ].map(([label, key]) => {
                            const value = eventDetail.quality_report.scores[key] ?? 0;
                            return (
                              <div className="quality-score-card" key={key}>
                                <span>{label}</span>
                                <strong>{value}</strong>
                                <div className="progress"><span className={scoreTone(value)} style={{ width: `${value}%` }} /></div>
                              </div>
                            );
                          })}
                        </div>
                        <div className="quality-summary">
                          <strong>{eventDetail.quality_report.summary || "事件级审校已完成。"}</strong>
                          {eventDetail.quality_report.repair_strategy ? <p>修复策略：{eventDetail.quality_report.repair_strategy}</p> : null}
                        </div>
                        {eventDetail.quality_report.strengths?.length ? (
                          <div className="event-criteria">
                            {eventDetail.quality_report.strengths.map((item, index) => <span key={`${item}-${index}`}>{item}</span>)}
                          </div>
                        ) : null}
                      </>
                    ) : (
                      <EmptyState title="尚未执行事件级审校" description="事件生成完成后会自动审校，也可以点击右上角“重新审校事件”。" />
                    )}
                  </div>
                </section>

                <ReviewIssuePanel
                  title="事件级质量建议"
                  issues={eventDetail.event_issues || []}
                  emptyTitle="暂无事件级质量建议"
                  emptyDescription="事件级审校完成后，系统质量建议会显示在这里。"
                />

                <section className="panel">
                  <div className="panel-header">
                    <div>
                      <div className="panel-title">章节计划看板</div>
                    </div>
                  </div>
                  <div className="panel-body event-board">
                    {eventDetail.plans.map((plan) => (
                      <article className={`event-board-card ${plan.status === "planned" ? "" : "done"}`} key={plan.id}>
                        <div className="event-board-card-head">
                          <span className="tag green">第 {plan.chapter_index} 章</span>
                          <span className={`tag ${plan.open_issue_count ? "yellow" : "green"}`}>{plan.open_issue_count ? `${plan.open_issue_count} 条后续处理` : "系统正常"}</span>
                        </div>
                        <h3>{plan.title || "未命名章节"}</h3>
                        <div className="event-card-meta">
                          <span>{plan.function || "推进"}</span>
                          <span>{statusText(plan.status)}</span>
                        </div>
                        {eventDetail.status === "planned" && planDraft ? (
                          <div className="field-stack">
                            <div className="field"><label>章节标题</label><input value={(planDraft.chapter_plans || []).find((item) => item.id === plan.id)?.title || ""} onChange={(event) => updateChapterPlanDraft(plan.id, "title", event.target.value)} /></div>
                            <div className="field"><label>章节功能</label><input value={(planDraft.chapter_plans || []).find((item) => item.id === plan.id)?.function || ""} onChange={(event) => updateChapterPlanDraft(plan.id, "function", event.target.value)} /></div>
                            <div className="field"><label>本章核心事件</label><textarea value={(planDraft.chapter_plans || []).find((item) => item.id === plan.id)?.core_event || ""} onChange={(event) => updateChapterPlanDraft(plan.id, "core_event", event.target.value)} rows={3} /></div>
                            <div className="field"><label>章末钩子</label><input value={(planDraft.chapter_plans || []).find((item) => item.id === plan.id)?.ending_hook || ""} onChange={(event) => updateChapterPlanDraft(plan.id, "ending_hook", event.target.value)} /></div>
                          </div>
                        ) : (
                          <>
                            <p>{plan.core_event || "暂无本章核心事件。"}</p>
                            {plan.ending_hook ? <em>章末钩子：{plan.ending_hook}</em> : null}
                          </>
                        )}
                        {plan.issues?.length ? (
                          <div className="event-card-issues">
                            {plan.issues.slice(0, 2).map((issue) => (
                              <span key={issue.id}>{statusText(issue.status)} · {issue.message}</span>
                            ))}
                          </div>
                        ) : null}
                        <div className="inline-actions">
                          <button className="secondary-button" disabled={Boolean(trackingTaskId) || busyPlanId === plan.id} onClick={() => rerunPlan(plan)}>
                            {busyPlanId === plan.id && trackingTaskId ? "处理中" : "重跑本章"}
                          </button>
                          <button className="ghost-button" disabled={Boolean(trackingTaskId) || busyPlanId === plan.id} onClick={() => continueFrom(plan)}>
                            从本章继续
                          </button>
                          {plan.chapter_id ? <button className="ghost-button" onClick={() => router.push(`/chapter-canvas?novel=${selectedNovelId}`)}>阅读</button> : null}
                        </div>
                      </article>
                    ))}
                  </div>
                </section>

                <ReviewIssuePanel
                  title="后续自动处理"
                  issues={eventDetail.open_issues || []}
                  emptyTitle="当前事件暂无后续处理项"
                  emptyDescription="连续性审校和事件级审校都已进入系统自动闭环。"
                />
              </>
            ) : (
              <EmptyState title="未选择剧情事件" description="请选择左侧事件查看详情。" />
            )}
          </div>
        </section>
      )}
    </>
  );
}

export default function StoryEventsPage() {
  return (
    <Suspense fallback={<main className="route-loading">正在载入剧情事件...</main>}>
      <StoryEventsContent />
    </Suspense>
  );
}
