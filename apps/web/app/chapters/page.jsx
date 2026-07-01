"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";

import AppShell from "@/components/AppShell";
import EmptyState from "@/components/EmptyState";
import MetricCard from "@/components/MetricCard";
import { apiFetch } from "@/lib/api";

const emptyChapter = {
  // 手动新建章节时使用的默认草稿，Worker 生成章节也会落到同一张表。
  chapter_index: 1,
  title: "第一章 灰塔之下",
  status: "draft",
  summary: "",
  content: "",
  context_snapshot: {}
};

function ChaptersContent() {
  // 章节页用于查看/编辑正文，同时展示 Worker 写入的上下文快照。
  const searchParams = useSearchParams();
  const [projects, setProjects] = useState([]);
  const [selectedNovelId, setSelectedNovelId] = useState("");
  const [chapters, setChapters] = useState([]);
  const [selectedChapterId, setSelectedChapterId] = useState("");
  const [draft, setDraft] = useState(emptyChapter);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const selectedProject = useMemo(
    () => projects.find((project) => project.id === selectedNovelId),
    [projects, selectedNovelId]
  );
  const selectedChapter = useMemo(
    () => chapters.find((chapter) => chapter.id === selectedChapterId),
    [chapters, selectedChapterId]
  );
  const totalWords = chapters.reduce((sum, chapter) => sum + chapter.word_count, 0);

  async function loadProjects() {
    // 章节页也支持 URL 携带 novel 参数，方便从工作台跳转到当前作品。
    const data = await apiFetch("/api/novels");
    setProjects(data);
    const nextNovelId = searchParams.get("novel") || data[0]?.id || "";
    setSelectedNovelId(nextNovelId);
    return nextNovelId;
  }

  async function loadChapters(novelId) {
    // 加载章节后默认选中第一章；没有章节时保持空草稿状态。
    if (!novelId) return;
    const data = await apiFetch(`/api/novels/${novelId}/chapters`);
    setChapters(data);
    const nextChapter = data[0];
    setSelectedChapterId(nextChapter?.id || "");
    setDraft(nextChapter || { ...emptyChapter, chapter_index: data.length + 1 });
  }

  useEffect(() => {
    loadProjects().then(loadChapters).catch((err) => setError(err.message));
  }, [searchParams]);

  useEffect(() => {
    if (selectedNovelId) loadChapters(selectedNovelId).catch((err) => setError(err.message));
  }, [selectedNovelId]);

  useEffect(() => {
    if (selectedChapter) setDraft(selectedChapter);
  }, [selectedChapter]);

  function updateDraft(key, value) {
    // 草稿编辑统一入口，便于后续增加脏状态提示或自动保存。
    setDraft((current) => ({ ...current, [key]: value }));
  }

  function startNewChapter() {
    // 新章序号从当前最大章节号递增，避免删除章节后序号回退。
    const nextIndex = chapters.length ? Math.max(...chapters.map((chapter) => chapter.chapter_index)) + 1 : 1;
    setSelectedChapterId("");
    setDraft({ ...emptyChapter, chapter_index: nextIndex, title: `第 ${nextIndex} 章` });
  }

  async function saveChapter(event) {
    // 选中已有章节时 PATCH；没有选中章节时 POST 创建。
    event.preventDefault();
    setError("");
    setMessage("");
    const payload = {
      chapter_index: Number(draft.chapter_index),
      title: draft.title,
      status: draft.status,
      summary: draft.summary,
      content: draft.content,
      context_snapshot: draft.context_snapshot || {}
    };
    try {
      const saved = selectedChapterId
        ? await apiFetch(`/api/novels/${selectedNovelId}/chapters/${selectedChapterId}`, {
            method: "PATCH",
            body: JSON.stringify(payload)
          })
        : await apiFetch(`/api/novels/${selectedNovelId}/chapters`, {
            method: "POST",
            body: JSON.stringify(payload)
          });
      setMessage(`已保存：第 ${saved.chapter_index} 章`);
      await loadChapters(selectedNovelId);
      setSelectedChapterId(saved.id);
    } catch (err) {
      setError(err.message);
    }
  }

  async function deleteChapter() {
    if (!selectedChapterId) return;
    setError("");
    setMessage("");
    try {
      await apiFetch(`/api/novels/${selectedNovelId}/chapters/${selectedChapterId}`, { method: "DELETE" });
      setMessage("章节已删除");
      await loadChapters(selectedNovelId);
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <AppShell
      title="章节管理"
      subtitle="维护章节列表、正文草稿、摘要和上下文快照"
      actions={
        <>
          <select className="secondary-button" value={selectedNovelId} onChange={(event) => setSelectedNovelId(event.target.value)}>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}
          </select>
          <button className="primary-button" disabled={!selectedNovelId} onClick={startNewChapter}>新建章节</button>
        </>
      }
    >
      {projects.length === 0 ? (
        <EmptyState title="还没有作品" description="请先在作品管理里创建作品，再维护章节。" action={<a className="primary-button" href="/projects">去创建作品</a>} />
      ) : (
        <>
          <section className="grid-4">
            <MetricCard label="当前作品" value={selectedProject?.title || "-"} note={selectedProject?.genre || "未分类"} />
            <MetricCard label="章节数" value={`${chapters.length} 章`} note="当前作品已保存章节" tone="green" />
            <MetricCard label="正文总字数" value={`${totalWords.toLocaleString()} 字`} note="按章节正文统计" tone="purple" />
            <MetricCard label="当前状态" value={draft.status || "-"} note="正在编辑的章节状态" tone="yellow" />
          </section>

          {(message || error) ? <div className={error ? "error-box" : "hint-panel"}>{error || message}</div> : null}

          <section className="chapter-layout">
            <aside className="panel chapter-list">
              <div className="panel-header">
                <div><div className="panel-title">章节列表</div><div className="panel-subtitle">按章节序号排序。</div></div>
              </div>
              <div className="panel-body stack-list">
                {chapters.length === 0 ? (
                  <EmptyState title="暂无章节" description="点击右上角新建章节，先建立手动编辑闭环。" />
                ) : chapters.map((chapter) => (
                  <button
                    className={`chapter-row ${chapter.id === selectedChapterId ? "active" : ""}`}
                    key={chapter.id}
                    onClick={() => setSelectedChapterId(chapter.id)}
                  >
                    <span>第 {chapter.chapter_index} 章</span>
                    <strong>{chapter.title || "未命名章节"}</strong>
                    <small>{chapter.status} · {chapter.word_count} 字</small>
                  </button>
                ))}
              </div>
            </aside>

            <form className="panel chapter-editor" onSubmit={saveChapter}>
              <div className="panel-header">
                <div><div className="panel-title">{selectedChapterId ? "编辑章节" : "新建章节"}</div><div className="panel-subtitle">后续 Agent 生成的草稿也会回写到这里。</div></div>
                <div className="inline-actions">
                  {selectedChapterId ? <button type="button" className="danger-button" onClick={deleteChapter}>删除</button> : null}
                  <button className="primary-button">保存章节</button>
                </div>
              </div>
              <div className="panel-body form-grid">
                <div className="grid-3">
                  <div className="field"><label>章节序号</label><input type="number" value={draft.chapter_index} onChange={(e) => updateDraft("chapter_index", e.target.value)} /></div>
                  <div className="field"><label>章节标题</label><input value={draft.title || ""} onChange={(e) => updateDraft("title", e.target.value)} /></div>
                  <div className="field"><label>状态</label><select value={draft.status || "draft"} onChange={(e) => updateDraft("status", e.target.value)}><option value="draft">draft</option><option value="generating">generating</option><option value="reviewing">reviewing</option><option value="done">done</option></select></div>
                </div>
                <div className="field"><label>章节摘要</label><textarea value={draft.summary || ""} onChange={(e) => updateDraft("summary", e.target.value)} /></div>
                <div className="field"><label>正文草稿</label><textarea className="chapter-content-input" value={draft.content || ""} onChange={(e) => updateDraft("content", e.target.value)} /></div>
                <section className="context-preview">
                  <div className="panel-title">上下文快照</div>
                  <div className="panel-subtitle">Worker 生成本章时使用的 ChapterContext，会随章节一起保存。</div>
                  {draft.context_snapshot?.schema_version ? (
                    <>
                      <div className="grid-4">
                        <div className="mini-stat"><span>最近章节</span><strong>{draft.context_snapshot.stats?.recent_chapter_count ?? 0}</strong></div>
                        <div className="mini-stat"><span>结构化记忆</span><strong>{draft.context_snapshot.stats?.memory_count ?? 0}</strong></div>
                        <div className="mini-stat"><span>伏笔</span><strong>{draft.context_snapshot.stats?.foreshadowing_count ?? 0}</strong></div>
                        <div className="mini-stat"><span>开放风险</span><strong>{draft.context_snapshot.stats?.open_review_issue_count ?? 0}</strong></div>
                      </div>
                      <pre className="json-preview">{JSON.stringify(draft.context_snapshot, null, 2)}</pre>
                    </>
                  ) : (
                    <div className="hint-panel">当前章节还没有上下文快照。通过工作台启动章节 Agent 后，Worker 会自动写入。</div>
                  )}
                </section>
              </div>
            </form>
          </section>
        </>
      )}
    </AppShell>
  );
}

export default function ChaptersPage() {
  return (
    <Suspense fallback={<main className="route-loading">正在载入章节管理...</main>}>
      <ChaptersContent />
    </Suspense>
  );
}
