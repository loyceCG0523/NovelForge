"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";

import AppShell from "@/components/AppShell";
import EmptyState from "@/components/EmptyState";
import { apiFetch, apiFetchCached, getCachedApiData } from "@/lib/api";

const memoryTypeLabels = {
  all: "全部",
  character: "人物",
  relationship: "关系",
  location: "地点",
  item: "道具",
  event: "事件",
  timeline: "时间线",
  world_rule: "世界规则",
  chapter_summary: "章节摘要"
};

function getPayloadSummary(payload) {
  return payload?.summary || payload?.status || payload?.evidence || "暂无摘要。";
}

function getProfileRows(payload) {
  const profile = payload?.profile || {};
  return [
    ["身份", profile.identity],
    ["年级", profile.grade],
    ["年龄", profile.age],
    ["外貌", profile.appearance],
    ["性格", profile.personality],
    ["家庭", profile.family],
    ["能力", profile.ability],
    ["目标", profile.goal],
    ["关系", profile.relationship],
    ["状态", profile.stable_state]
  ].filter(([, value]) => Array.isArray(value) ? value.length > 0 : Boolean(value));
}

function formatProfileValue(value) {
  return Array.isArray(value) ? value.join("；") : value;
}

function getSourceCount(payload) {
  return payload?.source_memory_count || 1;
}

function MemoryContent() {
  // 结构化记忆页用于查看自动抽取和手动维护的长期事实，后续章节生成会读取这些内容。
  const searchParams = useSearchParams();
  const novelIdFromUrl = searchParams.get("novel");
  const initialProjects = getCachedApiData("/api/novels");
  const [projects, setProjects] = useState(() => initialProjects || []);
  const [selectedId, setSelectedId] = useState(() => novelIdFromUrl || initialProjects?.[0]?.id || "");
  const [memories, setMemories] = useState([]);
  const [activeType, setActiveType] = useState("all");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [deletingId, setDeletingId] = useState("");
  const [pendingDelete, setPendingDelete] = useState(null);
  const [confirmClearAll, setConfirmClearAll] = useState(false);
  const [clearingAll, setClearingAll] = useState(false);

  const selectedProject = useMemo(
    () => projects.find((item) => item.id === selectedId),
    [projects, selectedId]
  );

  const visibleMemories = useMemo(() => {
    if (activeType === "all") return memories;
    return memories.filter((item) => item.memory_type === activeType);
  }, [activeType, memories]);

  const typeCounts = useMemo(() => {
    const counts = { all: memories.length };
    for (const item of memories) counts[item.memory_type] = (counts[item.memory_type] || 0) + 1;
    return counts;
  }, [memories]);

  async function loadProjects() {
    const data = await apiFetchCached("/api/novels", { ttlMs: 300_000 });
    setProjects(data);
    const nextId = novelIdFromUrl || selectedId || data[0]?.id || "";
    setSelectedId(nextId);
    return nextId;
  }

  async function loadMemories(id, { force = false } = {}) {
    if (!id) return;
    setMemories(await apiFetchCached(`/api/novels/${id}/memory`, { ttlMs: 30_000, force }));
  }

  async function confirmDeleteMemory() {
    if (!selectedId || !pendingDelete) return;
    setMessage("");
    setError("");
    setDeletingId(pendingDelete.id);
    try {
      await apiFetch(`/api/novels/${selectedId}/memory/${pendingDelete.id}`, { method: "DELETE" });
      const sourceIds = pendingDelete.payload?.source_memory_ids || [pendingDelete.id];
      setMemories((current) => current.filter((memory) => !sourceIds.includes(memory.id)));
      setMessage("结构化记忆已删除");
      setPendingDelete(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setDeletingId("");
    }
  }

  async function clearAllMemories() {
    if (!selectedId) return;
    setMessage("");
    setError("");
    setClearingAll(true);
    try {
      await apiFetch(`/api/novels/${selectedId}/memory`, { method: "DELETE" });
      setMemories([]);
      setActiveType("all");
      setMessage("已清空当前作品的全部结构化记忆");
      setConfirmClearAll(false);
    } catch (err) {
      setError(err.message);
    } finally {
      setClearingAll(false);
    }
  }

  useEffect(() => {
    loadProjects().catch((err) => setError(err.message));
  }, [novelIdFromUrl]);

  useEffect(() => {
    if (selectedId) loadMemories(selectedId).catch((err) => setError(err.message));
  }, [selectedId]);

  return (
    <AppShell
      title="结构化记忆"
      actions={
        <>
          <select className="secondary-button" value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}
          </select>
          <button className="danger-button" disabled={!selectedId || memories.length === 0} onClick={() => setConfirmClearAll(true)}>清空全部</button>
        </>
      }
    >
      {projects.length === 0 ? (
        <EmptyState title="还没有作品" description="先创建作品并生成章节，系统会在章节完成后同步结构化记忆。" action={<a className="primary-button" href="/projects">去创建作品</a>} />
      ) : (
        <section className="panel memory-panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">{selectedProject?.title || "当前作品"}</div>
              <div className="panel-subtitle">共 {memories.length} 条记忆，来自自动抽取和手动维护。</div>
            </div>
          </div>
          <div className="panel-body">
            {(message || error) ? <div className={error ? "error-box" : "hint-panel"}>{error || message}</div> : null}
            <div className="filter-row">
              {Object.entries(memoryTypeLabels).map(([type, label]) => (
                <button
                  key={type}
                  className={`chip-button ${activeType === type ? "active" : ""}`}
                  onClick={() => setActiveType(type)}
                >
                  {label} {typeCounts[type] || 0}
                </button>
              ))}
            </div>

            {visibleMemories.length === 0 ? (
              <EmptyState title="暂无结构化记忆" description="生成章节后，Worker 会自动从正文中抽取记忆写入这里。" />
            ) : (
              <div className="memory-grid">
                {visibleMemories.map((item) => (
                  <article className="memory-card" key={item.id}>
                    <div className="memory-card-head">
                      <span className="tag green">{memoryTypeLabels[item.memory_type] || item.memory_type}</span>
                      <span className="memory-range">
                        {item.chapter_index_start ? `第 ${item.chapter_index_start} 章` : "全局"}
                        {item.chapter_index_end && item.chapter_index_end !== item.chapter_index_start ? ` - 第 ${item.chapter_index_end} 章` : ""}
                      </span>
                    </div>
                    <h2>{item.entity_name}</h2>
                    {item.memory_type === "character" && getProfileRows(item.payload).length > 0 ? (
                      <div className="profile-list">
                        {getProfileRows(item.payload).map(([label, value]) => (
                          <div key={label}><span>{label}</span><strong>{formatProfileValue(value)}</strong></div>
                        ))}
                      </div>
                    ) : (
                      <p>{getPayloadSummary(item.payload)}</p>
                    )}
                    <div className="memory-card-actions">
                      <div className="memory-tags">
                        {item.payload?.importance ? <span className="tag purple">重要性：{item.payload.importance}</span> : null}
                        {getSourceCount(item.payload) > 1 ? <span className="tag">合并 {getSourceCount(item.payload)} 条</span> : null}
                      </div>
                      <button
                        className="danger-button"
                        disabled={deletingId === item.id}
                        onClick={() => setPendingDelete(item)}
                      >
                        {deletingId === item.id ? "删除中" : "删除"}
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </div>
        </section>
      )}
      {pendingDelete ? (
        <div className="modal-backdrop" role="presentation" onClick={() => setPendingDelete(null)}>
          <div className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-memory-title" onClick={(event) => event.stopPropagation()}>
            <div>
              <div className="panel-title" id="delete-memory-title">删除结构化记忆</div>
              <div className="panel-subtitle">删除后，这条记忆不会再进入后续章节生成上下文。</div>
            </div>
            <div className="delete-preview">
              <span>{memoryTypeLabels[pendingDelete.memory_type] || pendingDelete.memory_type}</span>
              <strong>{pendingDelete.entity_name}</strong>
              <p>{getPayloadSummary(pendingDelete.payload)}</p>
              {getSourceCount(pendingDelete.payload) > 1 ? <p>将同时删除该规范实体下的 {getSourceCount(pendingDelete.payload)} 条来源记录。</p> : null}
            </div>
            <div className="inline-actions dialog-actions">
              <button className="secondary-button" disabled={deletingId === pendingDelete.id} onClick={() => setPendingDelete(null)}>取消</button>
              <button className="danger-button" disabled={deletingId === pendingDelete.id} onClick={confirmDeleteMemory}>
                {deletingId === pendingDelete.id ? "删除中" : "确认删除"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {confirmClearAll ? (
        <div className="modal-backdrop" role="presentation" onClick={() => setConfirmClearAll(false)}>
          <div className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="clear-memory-title" onClick={(event) => event.stopPropagation()}>
            <div>
              <div className="panel-title" id="clear-memory-title">清空全部结构化记忆</div>
              <div className="panel-subtitle">该操作会删除当前作品下所有人物、关系、地点、道具、事件、时间线和世界规则记忆。</div>
            </div>
            <div className="delete-preview">
              <span>{selectedProject?.title || "当前作品"}</span>
              <strong>将删除 {memories.length} 条合并记忆</strong>
              <p>清空后，后续章节生成将不再读取这些结构化记忆；已经生成的章节正文不会被删除。</p>
            </div>
            <div className="inline-actions dialog-actions">
              <button className="secondary-button" disabled={clearingAll} onClick={() => setConfirmClearAll(false)}>取消</button>
              <button className="danger-button" disabled={clearingAll} onClick={clearAllMemories}>
                {clearingAll ? "清空中" : "确认清空"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </AppShell>
  );
}

export default function MemoryPage() {
  return (
    <Suspense fallback={<div className="route-loading">正在载入结构化记忆...</div>}>
      <MemoryContent />
    </Suspense>
  );
}
