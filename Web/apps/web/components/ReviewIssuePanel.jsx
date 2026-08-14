"use client";

const issueTypeLabels = {
  continuity_character: "人物连续性",
  continuity_item: "道具连续性",
  continuity_timeline: "时间线连续性",
  continuity_foreshadowing: "伏笔连续性",
  continuity_world_rule: "世界规则",
  continuity_event: "事件连续性",
  event_closure: "事件闭环",
  event_pacing: "事件节奏",
  event_conflict: "冲突推进",
  event_character_arc: "人物推进",
  event_foreshadowing: "伏笔推进",
  event_repetition: "重复空转",
  event_plan_consistency: "事件计划一致性",
  anti_ai_style: "AI 风格",
  punctuation_fragmentation: "标点节奏 / AI 风格",
  resource_state_conflict: "道具 / 资源状态",
  paragraph_overlength: "段落长度 / 阅读节奏",
};

const statusLabels = {
  open: "系统处理中",
  system_deferred: "后续自动处理",
  quality_note: "质量建议",
  resolved: "已解决",
  ignored: "已忽略",
};

export default function ReviewIssuePanel({
  title = "系统审校记录",
  issues = [],
  emptyTitle = "暂无审校记录",
  emptyDescription = "生成后的连续性审校、质量建议和自动修复历史会出现在这里。",
  onStatusChange,
  onRevise,
  updatingId = "",
  revisingId = "",
  showActions = false,
  showResolvedActions = false,
}) {
  return (
    <section className="panel review-panel">
      <div className="panel-header">
        <div>
          <div className="panel-title">{title}</div>
        </div>
        <span className="tag red">{issues.length}</span>
      </div>
      <div className="panel-body stack-list">
        {issues.length === 0 ? (
          <article className="empty-inline">
            <h2>{emptyTitle}</h2>
            <p>{emptyDescription}</p>
          </article>
        ) : issues.map((issue) => (
          <article className="review-card" key={issue.id}>
            <div className="review-card-head">
              <div>
                <span className={`tag ${issue.severity === "high" ? "red" : issue.severity === "low" ? "green" : "yellow"}`}>
                  {issue.severity}
                </span>
                <span className="tag">{statusLabels[issue.status] || issue.status || "系统记录"}</span>
              </div>
              <strong>{issueTypeLabels[issue.issue_type] || issue.issue_type}</strong>
            </div>
            <p>{issue.message}</p>
            {issue.payload?.evidence ? <div className="review-detail"><span>依据</span><p>{issue.payload.evidence}</p></div> : null}
            {issue.payload?.expected ? <div className="review-detail"><span>应保持</span><p>{issue.payload.expected}</p></div> : null}
            {issue.payload?.suggestion ? <div className="review-detail"><span>建议</span><p>{issue.payload.suggestion}</p></div> : null}
            {issue.payload?.auto_repair ? (
              <div className={`review-detail repair-detail ${issue.payload.auto_repair.status === "failed" ? "failed" : ""}`}>
                <span>自动处理</span>
                <p>
                  {issue.payload.auto_repair.status === "resolved" ? "已自动修复" : issue.payload.auto_repair.status === "deferred" || issue.payload.auto_repair.status === "skipped" ? "已转入后续自动处理" : "自动修复失败"}
                  {issue.payload.auto_repair.revision_note ? `：${issue.payload.auto_repair.revision_note}` : ""}
                  {issue.payload.auto_repair.error ? `：${issue.payload.auto_repair.error}` : ""}
                  {issue.payload.auto_repair.message ? `：${issue.payload.auto_repair.message}` : ""}
                </p>
              </div>
            ) : null}
            {Array.isArray(issue.payload?.repair_history) && issue.payload.repair_history.length > 1 ? (
              <div className="repair-history">
                {issue.payload.repair_history.map((item, index) => (
                  <span key={`${issue.id}-repair-${index}`}>
                    第 {index + 1} 次 · {item.status === "resolved" ? "已修复" : item.status === "deferred" || item.status === "skipped" ? "后续处理" : "失败"}
                  </span>
                ))}
              </div>
            ) : null}
            {showActions ? <div className="inline-actions review-actions">
              {onRevise && issue.status === "open" && issue.chapter_id ? (
                <button
                  type="button"
                  className="primary-button compact-button"
                  disabled={revisingId === issue.id || updatingId === issue.id}
                  onClick={() => onRevise(issue)}
                >
                  {revisingId === issue.id ? "修订中" : "按建议修正"}
                </button>
              ) : null}
              {issue.status !== "resolved" ? (
                <button type="button" className="secondary-button" disabled={updatingId === issue.id || revisingId === issue.id} onClick={() => onStatusChange?.(issue, "resolved")}>
                  标记已解决
                </button>
              ) : null}
              {issue.status !== "ignored" ? (
                <button type="button" className="ghost-button" disabled={updatingId === issue.id || revisingId === issue.id} onClick={() => onStatusChange?.(issue, "ignored")}>
                  忽略
                </button>
              ) : null}
              {showResolvedActions && issue.status !== "open" ? (
                <button type="button" className="secondary-button" disabled={updatingId === issue.id || revisingId === issue.id} onClick={() => onStatusChange?.(issue, "open")}>
                  重新打开
                </button>
              ) : null}
            </div> : null}
          </article>
        ))}
      </div>
    </section>
  );
}
