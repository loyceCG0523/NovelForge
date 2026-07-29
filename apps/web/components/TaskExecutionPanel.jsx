"use client";

import { useEffect, useMemo, useRef, useState } from "react";

function eventStatusLabel(status) {
  return {
    pending: "等待",
    running: "进行中",
    paused: "已暂停",
    partial: "部分完成",
    completed: "完成",
    failed: "失败",
    info: "记录"
  }[status] || status || "记录";
}

function latestByStep(events) {
  const order = [];
  const values = new Map();
  for (const event of events || []) {
    const key = event.step_key || `event-${event.sequence_no}`;
    if (!values.has(key)) order.push(key);
    values.set(key, event);
  }
  return order.map((key) => values.get(key));
}

function combinedStatus(items) {
  if (items.some((item) => item.status === "failed")) return "failed";
  if (items.some((item) => item.status === "running")) return "running";
  if (items.some((item) => item.status === "paused")) return "paused";
  if (items.length && items.every((item) => item.status === "completed")) return "completed";
  if (items.some((item) => item.status === "completed")) return "running";
  return "pending";
}

function severityLabel(severity) {
  return {
    high: "高优先级",
    medium: "中优先级",
    low: "低优先级"
  }[severity] || "";
}

function eventResearchDetail(step) {
  const sourceIds = Array.isArray(step?.payload?.source_ids) ? step.payload.source_ids : [];
  if (step?.payload?.reason === "not_needed") return "本事件不需要额外现实资料";
  if (step?.payload?.enabled === false) return "现实资料检索未启用";
  return `已关联 ${sourceIds.length} 条现实资料`;
}

function buildFlowSteps(events, active = false, taskStatus = "", taskError = "") {
  const steps = latestByStep(
    (events || []).filter((event) => !["chapter_preview", "chapter_preview_reset"].includes(event.event_type))
  );
  if (!steps.length) {
    return [{ key: "waiting", title: "等待生成任务", status: "pending", detail: "尚未开始" }];
  }

  const flow = [];
  const eventPlan = steps.find((step) => step.step_key === "event_plan");
  const eventResearch = steps.find((step) => step.step_key === "event_research");
  if (eventPlan) flow.push({ ...eventPlan, key: "event_plan", title: "事件规划", detail: eventPlan.message });
  if (eventResearch) flow.push({
    ...eventResearch,
    key: "event_research",
    title: "资料检查",
    detail: eventResearchDetail(eventResearch)
  });
  const pauseStep = steps.find((step) => step.step_key === "pause" && step.status === "completed");

  const chapterGroups = new Map();
  for (const step of steps) {
    const match = String(step.step_key || "").match(/^chapter_(\d+)_(draft|review|revision|meme)$/);
    if (!match) continue;
    const chapterIndex = Number(match[1]);
    if (!chapterGroups.has(chapterIndex)) chapterGroups.set(chapterIndex, []);
    chapterGroups.get(chapterIndex).push(step);
  }
  const latestRunningChapterStep = [...chapterGroups.values()]
    .flat()
    .filter((step) => step.status === "running")
    .sort((a, b) => Number(b.sequence_no || 0) - Number(a.sequence_no || 0))[0];
  for (const [chapterIndex, chapterSteps] of [...chapterGroups.entries()].sort((a, b) => a[0] - b[0])) {
    const activeStep = chapterSteps.find((step) => step.status === "failed")
      || chapterSteps.find((step) => step.status === "running")
      || chapterSteps.find((step) => step.status === "paused")
      || [...chapterSteps].reverse().find((step) => step.status === "completed")
      || chapterSteps[0];
    const historicalPause = Boolean(
      !active
      && pauseStep
      && latestRunningChapterStep
      && chapterSteps.includes(latestRunningChapterStep)
    );
    const chapterStatus = historicalPause ? "paused" : combinedStatus(chapterSteps);
    flow.push({
      ...activeStep,
      key: `chapter_${chapterIndex}`,
      title: `第 ${chapterIndex} 章`,
      chapter_index: chapterIndex,
      status: chapterStatus,
      detail: historicalPause
        ? "已暂停，继续后将重新生成本章"
        : chapterStatus === "failed"
          ? activeStep?.message || activeStep?.title || "章节处理失败"
          : activeStep?.message || activeStep?.title || "等待生成"
    });
  }

  const eventReviewSteps = steps.filter(
    (step) => (
      ["event_review_initial", "event_review_final"].includes(step.step_key)
      || ["revision_plan", "revision_patch_stream", "revision_package"].includes(step.event_type)
    )
  );
  if (eventReviewSteps.length) {
    const initialReview = eventReviewSteps.find((step) => step.step_key === "event_review_initial");
    const finalReview = eventReviewSteps.find((step) => step.step_key === "event_review_final");
    const manualRetryStep = [...eventReviewSteps].reverse().find(
      (step) => step.payload?.manual_retry && ["pending", "running"].includes(step.status)
    );
    const partialPackage = [...eventReviewSteps].reverse().find(
      (step) => step.event_type === "revision_package" && step.payload?.status === "partial"
    );
    const inferredRetryableIndexes = Object.keys(partialPackage?.payload?.chapter_errors || {})
      .map(Number)
      .filter((index) => Number.isFinite(index));
    const hasExplicitRetryState = Array.isArray(finalReview?.payload?.retryable_chapter_indexes);
    const retryableChapterIndexes = hasExplicitRetryState
      ? finalReview.payload.retryable_chapter_indexes
      : inferredRetryableIndexes;
    const manualRetryFinished = Boolean(finalReview?.payload?.manual_retry);
    const failedChapterDetails = finalReview?.payload?.failed_chapters || {};
    const retryFailureReason = Object.values(failedChapterDetails)
      .map((item) => item?.error)
      .find(Boolean);
    const isPartial = Boolean(finalReview?.status === "completed" && retryableChapterIndexes.length);
    const activeStep = manualRetryStep
      || (isPartial ? finalReview : null)
      || eventReviewSteps.find((step) => step.status === "failed")
      || eventReviewSteps.find((step) => step.status === "running")
      || [...eventReviewSteps].reverse().find((step) => step.status === "completed")
      || eventReviewSteps[0];
    const reviewStatus = manualRetryStep
      ? manualRetryStep.status
      : isPartial
        ? "partial"
        : finalReview?.status === "completed" && hasExplicitRetryState
          ? "completed"
          : (
      !active
      && initialReview?.status === "completed"
      && finalReview?.status === "pending"
      && !eventReviewSteps.some((step) => step.status === "failed")
    )
      ? "completed"
      : combinedStatus(eventReviewSteps);
    flow.push({
      ...activeStep,
      key: "event_review",
      title: "事件复检",
      status: reviewStatus,
      detail: isPartial
        ? (
          manualRetryFinished
            ? `已重试第 ${retryableChapterIndexes.join("、")} 章，但补丁仍未通过${retryFailureReason ? `：${retryFailureReason}` : ""}`
            : `第 ${retryableChapterIndexes.join("、")} 章补丁失败，其余复检已完成`
        )
        : activeStep?.title,
      payload: {
        ...(activeStep?.payload || {}),
        retryable: isPartial,
        retryable_chapter_indexes: retryableChapterIndexes
      }
    });
  }

  const memoryStep = steps.find((step) => step.step_key === "memory_sync");
  const failedMemoryStep = steps.find(
    (step) => step.event_type === "memory_sync" && step.status === "failed" && step.payload?.retryable
  );
  if (memoryStep || failedMemoryStep) {
    const activeStep = failedMemoryStep || memoryStep;
    flow.push({
      ...activeStep,
      key: "memory_sync",
      title: "后台记忆",
      detail: activeStep?.message || activeStep?.title
    });
  }
  if (taskStatus === "failed") {
    const runningIndex = flow.findLastIndex((step) => step.status === "running");
    if (runningIndex >= 0) {
      flow[runningIndex] = {
        ...flow[runningIndex],
        status: "failed",
        detail: taskError || "任务执行失败，请查看错误提示后重试"
      };
    } else if (!flow.some((step) => step.status === "failed")) {
      flow.push({
        key: "task_failed",
        title: "任务执行失败",
        status: "failed",
        detail: taskError || "请查看错误提示后重试"
      });
    }
  }
  return flow;
}

function collectChapterActivity(events, patches) {
  const previewStates = new Map();
  for (const event of events || []) {
    if (!["chapter_preview", "chapter_preview_reset"].includes(event.event_type)) continue;
    const chapterIndex = Number(event.chapter_index);
    if (!Number.isFinite(chapterIndex) || chapterIndex <= 0) continue;
    const current = previewStates.get(chapterIndex) || {
      visible: null,
      latest: null,
      suppressRetryStream: false,
      retryAttempt: 0,
      retryChars: 0
    };
    const text = event.payload?.text || "";
    const attempt = Number(event.payload?.attempt || 1);
    current.latest = event;

    if (event.event_type === "chapter_preview_reset") {
      if (current.visible?.payload?.text) {
        // 已经展示过正文后发生重试：保留上一版，不让界面闪回空状态。
        current.suppressRetryStream = true;
        current.retryAttempt = attempt;
        current.retryChars = 0;
      } else {
        current.visible = event;
      }
    } else if (event.status === "completed") {
      // 最终版本确定后才替换冻结的上一版。
      current.visible = event;
      current.suppressRetryStream = false;
      current.retryAttempt = 0;
      current.retryChars = 0;
    } else if (current.suppressRetryStream) {
      current.retryAttempt = attempt;
      current.retryChars = Number(event.payload?.char_count || text.length);
    } else {
      current.visible = event;
    }
    previewStates.set(chapterIndex, current);
  }
  const patchChapterIndexes = [];
  for (const patch of patches || []) {
    const chapterIndex = Number(patch.chapter_index);
    if (!Number.isFinite(chapterIndex) || chapterIndex <= 0) continue;
    patchChapterIndexes.push(chapterIndex);
  }
  const chapterIndexes = [...new Set([...previewStates.keys(), ...patchChapterIndexes])].sort((a, b) => a - b);
  const latestPreviewState = [...previewStates.values()].sort(
    (a, b) => Number(b.latest?.sequence_no || 0) - Number(a.latest?.sequence_no || 0)
  )[0];
  const latestChapterIndex = Number(latestPreviewState?.latest?.chapter_index) || patchChapterIndexes.at(-1) || null;
  return { previewStates, chapterIndexes, latestChapterIndex };
}

function useTypewriterPreview(preview) {
  const targetText = preview?.payload?.text || "";
  const identity = preview
    ? `${preview.chapter_index || 0}-${preview.payload?.attempt || 0}`
    : "empty";
  // 首次打开页面或切换章节时，已有快照必须立即完整显示。
  // 只有同一生成尝试后续新收到的增量，才从当前长度继续逐字追加。
  const [display, setDisplay] = useState({ identity, text: targetText });
  const shouldAnimate = Boolean(
    display.identity === identity
    && targetText.startsWith(display.text)
    && display.text.length < targetText.length
  );

  useEffect(() => {
    setDisplay({ identity, text: targetText });
  }, [identity]);

  useEffect(() => {
    if (display.identity !== identity) return;
    if (!targetText.startsWith(display.text)) {
      // 模型重试、快照回滚或正文被替换时直接同步，不制造一次假的流式重放。
      setDisplay({ identity, text: targetText });
    }
  }, [display, identity, targetText]);

  useEffect(() => {
    if (!shouldAnimate || display.identity !== identity || display.text.length >= targetText.length) return undefined;
    const timer = window.setTimeout(() => {
      setDisplay((current) => {
        if (current.identity !== identity || !targetText.startsWith(current.text)) {
          return { identity, text: targetText };
        }
        return { identity, text: targetText.slice(0, current.text.length + 1) };
      });
    }, targetText.length - display.text.length > 400 ? 5 : 12);
    return () => window.clearTimeout(timer);
  }, [display, identity, shouldAnimate, targetText]);

  return {
    text: display.identity === identity ? display.text : "",
    animating: shouldAnimate && display.text.length < targetText.length
  };
}

export default function TaskExecutionPanel({
  events = [],
  patches = [],
  active = false,
  taskStatus = "",
  taskError = "",
  onRetryMemory = null,
  onRetryEventRevision = null
}) {
  const [retryingMemoryTaskId, setRetryingMemoryTaskId] = useState("");
  const [retryingEventRevision, setRetryingEventRevision] = useState(false);
  const [expandedFlowKey, setExpandedFlowKey] = useState("");
  const [chapterSelection, setChapterSelection] = useState("latest");
  const [followPreviewTail, setFollowPreviewTail] = useState(true);
  const [showInlineDiff, setShowInlineDiff] = useState(true);
  const flowScrollRef = useRef(null);
  const previewContentRef = useRef(null);
  const flowSteps = useMemo(
    () => buildFlowSteps(events, active, taskStatus, taskError),
    [active, events, taskError, taskStatus]
  );
  const currentFlowKey = (
    flowSteps.find((step) => step.status === "failed")
    || flowSteps.find((step) => step.status === "running")
    || flowSteps.find((step) => step.status === "paused")
    || flowSteps.find((step) => step.status === "partial")
    || flowSteps.find((step) => step.status === "pending")
    || flowSteps.at(-1)
  )?.key;
  const chapterActivity = useMemo(() => collectChapterActivity(events, patches), [events, patches]);
  const selectedChapterIndex = chapterSelection === "latest"
    ? chapterActivity.latestChapterIndex
    : Number(chapterSelection);
  const selectedPreviewState = selectedChapterIndex
    ? chapterActivity.previewStates.get(selectedChapterIndex)
    : null;
  const preview = selectedPreviewState?.visible || null;
  const retryPreviewActive = Boolean(selectedPreviewState?.suppressRetryStream);
  const chapterPatches = useMemo(
    () => (patches || [])
      .filter((patch) => Number(patch.chapter_index) === selectedChapterIndex)
      .sort((a, b) => Number(a.paragraph_index || 0) - Number(b.paragraph_index || 0)),
    [patches, selectedChapterIndex]
  );
  const paragraphPatchMap = useMemo(() => {
    const map = new Map();
    chapterPatches.forEach((patch) => {
      const paragraphIndex = Number(patch.paragraph_index);
      const current = map.get(paragraphIndex);
      if (!current || patch.status === "applied" || current.status !== "applied") {
        map.set(paragraphIndex, patch);
      }
    });
    return map;
  }, [chapterPatches]);
  const smoothPreview = useTypewriterPreview(preview);
  const latestEvent = events[events.length - 1];
  const lastUpdateAt = latestEvent?.created_at ? new Date(latestEvent.created_at) : null;
  const stalled = Boolean(active && lastUpdateAt && Date.now() - lastUpdateAt.getTime() > 120000);

  useEffect(() => {
    if (
      chapterSelection !== "latest"
      && !chapterActivity.chapterIndexes.includes(Number(chapterSelection))
    ) {
      setChapterSelection("latest");
    }
  }, [chapterActivity.chapterIndexes, chapterSelection]);

  useEffect(() => {
    setFollowPreviewTail(true);
    setShowInlineDiff(true);
  }, [selectedChapterIndex]);

  useEffect(() => {
    const container = flowScrollRef.current;
    const currentNode = container?.querySelector(`[data-flow-key="${currentFlowKey}"]`);
    if (!container || !currentNode) return;
    const left = currentNode.offsetLeft - (container.clientWidth - currentNode.clientWidth) / 2;
    container.scrollTo({ left: Math.max(0, left), behavior: "smooth" });
  }, [currentFlowKey]);

  useEffect(() => {
    const container = previewContentRef.current;
    if (!container || chapterSelection !== "latest" || !followPreviewTail) return;
    if (smoothPreview.animating && smoothPreview.text.length % 12 !== 0) return;
    const frame = window.requestAnimationFrame(() => {
      container.scrollTop = container.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [
    chapterSelection,
    followPreviewTail,
    smoothPreview.animating,
    smoothPreview.text.length
  ]);

  async function retryMemory(memoryTaskId) {
    if (!onRetryMemory || !memoryTaskId || retryingMemoryTaskId) return;
    setRetryingMemoryTaskId(memoryTaskId);
    try {
      await onRetryMemory(memoryTaskId);
    } finally {
      setRetryingMemoryTaskId("");
    }
  }

  async function retryEventRevision() {
    if (!onRetryEventRevision || retryingEventRevision) return;
    setRetryingEventRevision(true);
    try {
      await onRetryEventRevision();
    } finally {
      setRetryingEventRevision(false);
    }
  }

  return (
    <section className="panel execution-console">
      <div className="panel-header execution-console-header">
        <div>
          <div className="panel-title">实时执行控制台</div>
          <div className="panel-subtitle">只显示当前流程、正文和对应章节的局部修改。</div>
        </div>
        <div className="execution-live-status">
          {lastUpdateAt ? <small>更新于 {lastUpdateAt.toLocaleTimeString("zh-CN", { hour12: false })}</small> : null}
          <span className={`tag ${stalled ? "red" : active ? "yellow" : "green"}`}>
            {taskStatus === "failed"
              ? "执行失败"
              : stalled
                ? "等待响应"
                : active
                  ? "实时更新"
                  : events.length
                    ? "本次记录"
                    : "待开始"}
          </span>
        </div>
      </div>

      <div className="execution-flow">
        <div className="execution-flow-heading">
          <strong>To-do 执行流程</strong>
          <span>从左到右推进</span>
        </div>
        <div className="execution-flow-scroll" ref={flowScrollRef}>
          {flowSteps.map((step, index) => {
            const flowKey = step.key || `${step.step_key}-${index}`;
            const detail = step.detail || eventStatusLabel(step.status);
            const detailExpandable = String(detail).length > 22;
            const detailExpanded = expandedFlowKey === flowKey;
            return (
            <div
              className="execution-flow-segment"
              data-flow-key={step.key}
              key={flowKey}
            >
              <article className={`execution-flow-step ${step.status || "pending"} ${detailExpanded ? "detail-expanded" : ""}`}>
                <span className="execution-flow-index">{step.status === "completed" ? "✓" : index + 1}</span>
                <div>
                  <strong>{step.title || "执行步骤"}</strong>
                  {detailExpandable ? (
                    <button
                      type="button"
                      className="execution-flow-detail-toggle"
                      aria-expanded={detailExpanded}
                      onClick={() => setExpandedFlowKey(detailExpanded ? "" : flowKey)}
                    >
                      <small>{detail}</small>
                      <span>{detailExpanded ? "收起" : "展开"}</span>
                    </button>
                  ) : <small>{detail}</small>}
                  <em>
                    {eventStatusLabel(step.status)}
                    {Number.isFinite(step.progress) && step.status === "running" ? ` · ${step.progress}%` : ""}
                  </em>
                  {step.key === "memory_sync" && step.status === "failed" && step.payload?.retryable && onRetryMemory ? (
                    <button
                      type="button"
                      className="execution-flow-retry"
                      disabled={Boolean(retryingMemoryTaskId)}
                      onClick={() => retryMemory(step.payload.memory_task_id)}
                    >
                      {retryingMemoryTaskId ? "排队中…" : "重试"}
                    </button>
                  ) : null}
                  {step.key === "event_review" && step.status === "partial" && step.payload?.retryable && onRetryEventRevision ? (
                    <button
                      type="button"
                      className="execution-flow-retry"
                      disabled={retryingEventRevision}
                      onClick={retryEventRevision}
                    >
                      {retryingEventRevision ? "排队中…" : "重试失败章节"}
                    </button>
                  ) : null}
                </div>
              </article>
              {index < flowSteps.length - 1 ? <span className="execution-flow-arrow" aria-hidden="true">→</span> : null}
            </div>
            );
          })}
        </div>
      </div>

      <div className="panel-body execution-grid">
        <div className="execution-preview">
          <div className="execution-column-title execution-content-title">
            <span>
              <strong>章节实时预览</strong>
              <small>
                {preview?.status === "completed"
                  ? "正文已生成"
                  : retryPreviewActive
                    ? `正在生成修订版${selectedPreviewState.retryChars ? ` · ${selectedPreviewState.retryChars} 字符` : ""}`
                  : smoothPreview.animating
                    ? "正在接收新内容"
                    : preview
                      ? (active ? "等待模型增量" : "已有草稿")
                      : "等待正文"}
              </small>
            </span>
            <select
              className="execution-chapter-select"
              value={chapterSelection}
              onChange={(event) => setChapterSelection(event.target.value)}
              disabled={!chapterActivity.chapterIndexes.length}
              aria-label="选择预览章节"
            >
              <option value="latest">跟随最新章节</option>
              {chapterActivity.chapterIndexes.map((chapterIndex) => (
                <option value={String(chapterIndex)} key={chapterIndex}>第 {chapterIndex} 章</option>
              ))}
            </select>
          </div>
          {preview?.payload?.text ? (
            <div className="live-chapter-surface">
              <div className="live-chapter-meta">
                <span>
                  第 {selectedChapterIndex} 章
                  {retryPreviewActive ? ` · 当前保留上一版，修订尝试 ${selectedPreviewState.retryAttempt}` : ""}
                </span>
                <span className="live-chapter-meta-actions">
                  <span>{smoothPreview.text.length} / {preview.payload?.char_count || preview.payload.text.length} 字符</span>
                  {chapterPatches.length ? (
                    <button
                      type="button"
                      className={`inline-diff-toggle ${showInlineDiff ? "active" : ""}`}
                      onClick={() => setShowInlineDiff((current) => !current)}
                    >
                      Diff View · {chapterPatches.length}
                    </button>
                  ) : null}
                </span>
              </div>
              <div
                className="live-chapter-content"
                ref={previewContentRef}
                onScroll={(event) => {
                  const node = event.currentTarget;
                  setFollowPreviewTail(node.scrollHeight - node.scrollTop - node.clientHeight < 72);
                }}
              >
                {smoothPreview.text
                  .split(/\n\s*\n/)
                  .filter((paragraph) => paragraph.trim())
                  .map((paragraph, index) => {
                    const paragraphIndex = index + 1;
                    const patch = paragraphPatchMap.get(paragraphIndex);
                    const appliedText = patch?.status === "applied" ? patch.new_text : paragraph;
                    return (
                      <div
                        className={`live-paragraph-row ${patch ? "has-revision" : ""}`}
                        key={`paragraph-${paragraphIndex}`}
                      >
                        <span className="live-paragraph-index">P{paragraphIndex}</span>
                        <div className="live-paragraph-body">
                          {patch && showInlineDiff ? (
                            <div className="inline-paragraph-diff">
                              <div className="inline-diff-block removed">
                                <span>{patch.old_text}</span>
                              </div>
                              <div className="inline-diff-block added">
                                <span>{patch.new_text}</span>
                              </div>
                            </div>
                          ) : (
                            <p>{appliedText}</p>
                          )}
                        </div>
                      </div>
                    );
                  })}
                {(smoothPreview.animating || (
                  active
                  && chapterSelection === "latest"
                  && preview.status !== "completed"
                  && !retryPreviewActive
                ))
                  ? <i className="stream-caret" aria-label="正在生成" />
                  : null}
              </div>
            </div>
          ) : (
            <div className="execution-empty">
              {selectedChapterIndex ? `第 ${selectedChapterIndex} 章正文尚未开始输出。` : "章节开始生成后，正文会在这里逐字显示。"}
            </div>
          )}
        </div>

        <div className="execution-patches">
          <div className="execution-column-title execution-content-title">
            <span>
              <strong>局部修订</strong>
              <small>{selectedChapterIndex ? `第 ${selectedChapterIndex} 章` : "等待选择章节"}</small>
            </span>
            {chapterPatches.length ? <em>{chapterPatches.length} 处</em> : null}
          </div>
          {chapterPatches.length ? (
            <div className="patch-list">
              {chapterPatches.map((patch) => (
                <article className={`patch-card ${patch.status}`} key={patch.id}>
                  <div className="patch-head">
                    <strong>第 {patch.paragraph_index} 段</strong>
                    <span>
                      {severityLabel(patch.severity)}
                      {patch.severity ? " · " : ""}
                      {patch.status === "applied" ? "已修复" : patch.status === "rejected" ? "未采用" : "校验中"}
                    </span>
                  </div>
                  <div className="patch-guidance">
                    <strong>问题</strong>
                    <p>{patch.problem || "该段落存在需要局部调整的问题。"}</p>
                  </div>
                  <div className="patch-guidance suggestion">
                    <strong>修复建议</strong>
                    <p>{patch.suggestion || patch.reason || "按审校要求进行最小范围修改。"}</p>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="execution-empty">
              {selectedChapterIndex ? `第 ${selectedChapterIndex} 章暂无局部修改。` : "选择章节后，这里只显示同一章的问题与修复建议。"}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
