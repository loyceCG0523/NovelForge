"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import AppShell from "@/components/AppShell";
import EmptyState from "@/components/EmptyState";
import ReviewIssuePanel from "@/components/ReviewIssuePanel";
import { apiDownload, apiFetch, buildTimestampedDownloadFilename } from "@/lib/api";

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
  const router = useRouter();
  const searchParams = useSearchParams();
  const [projects, setProjects] = useState([]);
  const [selectedNovelId, setSelectedNovelId] = useState("");
  const [chapters, setChapters] = useState([]);
  const [selectedChapterId, setSelectedChapterId] = useState("");
  const [draft, setDraft] = useState(emptyChapter);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [contentExpanded, setContentExpanded] = useState(false);
  const [contextExpanded, setContextExpanded] = useState(false);
  const [chapterIssues, setChapterIssues] = useState([]);
  const [pendingDeleteChapter, setPendingDeleteChapter] = useState(null);
  const [deletingChapterId, setDeletingChapterId] = useState("");
  const [exportingFormat, setExportingFormat] = useState("");

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

  async function loadChapters(novelId, preferredChapterId = "") {
    // 加载章节后默认选中第一章；没有章节时保持空草稿状态。
    if (!novelId) return;
    const data = await apiFetch(`/api/novels/${novelId}/chapters`);
    setChapters(data);
    const nextChapter = data.find((chapter) => chapter.id === preferredChapterId) || data[0];
    setSelectedChapterId(nextChapter?.id || "");
    setDraft(nextChapter || { ...emptyChapter, chapter_index: data.length + 1 });
    if (nextChapter?.id) await loadChapterIssues(novelId, nextChapter.id);
  }

  async function loadChapterIssues(novelId, chapterId) {
    if (!novelId || !chapterId) {
      setChapterIssues([]);
      return;
    }
    setChapterIssues(await apiFetch(`/api/novels/${novelId}/reviews?chapter_id=${chapterId}`));
  }

  useEffect(() => {
    loadProjects().then(loadChapters).catch((err) => setError(err.message));
  }, [searchParams]);

  useEffect(() => {
    if (selectedNovelId) loadChapters(selectedNovelId).catch((err) => setError(err.message));
  }, [selectedNovelId]);

  useEffect(() => {
    if (selectedChapter) {
      setDraft(selectedChapter);
      setContentExpanded(false);
      setContextExpanded(false);
      if (selectedNovelId) loadChapterIssues(selectedNovelId, selectedChapter.id).catch((err) => setError(err.message));
    }
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
    setContentExpanded(false);
    setContextExpanded(false);
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
      await loadChapterIssues(selectedNovelId, saved.id);
    } catch (err) {
      setError(err.message);
    }
  }

  async function deleteChapter() {
    if (!pendingDeleteChapter) return;
    setError("");
    setMessage("");
    setDeletingChapterId(pendingDeleteChapter.id);
    try {
      await apiFetch(`/api/novels/${selectedNovelId}/chapters/${pendingDeleteChapter.id}`, { method: "DELETE" });
      setMessage("章节已删除");
      setPendingDeleteChapter(null);
      await loadChapters(selectedNovelId);
    } catch (err) {
      setError(err.message);
    } finally {
      setDeletingChapterId("");
    }
  }

  async function exportSelectedNovel(format) {
    if (!selectedNovelId) return;
    setError("");
    setMessage("");
    setExportingFormat(format);
    try {
      await apiDownload(
        `/api/novels/${selectedNovelId}/export?format=${format}`,
        buildTimestampedDownloadFilename(
          selectedProject?.title || "novel",
          format === "txt" ? "txt" : "md"
        )
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setExportingFormat("");
    }
  }

  return (
    <AppShell
      title="章节管理"
      actions={
        <>
          <select className="secondary-button" value={selectedNovelId} onChange={(event) => setSelectedNovelId(event.target.value)}>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}
          </select>
          <button className="secondary-button" disabled={!selectedNovelId || exportingFormat === "txt"} onClick={() => exportSelectedNovel("txt")}>导出 TXT</button>
          <button className="secondary-button" disabled={!selectedNovelId || exportingFormat === "markdown"} onClick={() => exportSelectedNovel("markdown")}>导出 MD</button>
          <button className="secondary-button" disabled={!selectedNovelId} onClick={() => router.push(`/chapter-canvas?novel=${selectedNovelId}`)}>章节画布</button>
          <button className="primary-button" disabled={!selectedNovelId} onClick={startNewChapter}>新建章节</button>
        </>
      }
    >
      {projects.length === 0 ? (
        <EmptyState title="还没有作品" description="请先在作品管理里创建作品，再维护章节。" action={<a className="primary-button" href="/projects">去创建作品</a>} />
      ) : (
        <>
          {(message || error) ? <div className={error ? "error-box" : "hint-panel"}>{error || message}</div> : null}

          <section className="chapter-layout">
            <aside className="panel chapter-list">
              <div className="panel-header">
                <div>
                  <div className="panel-title">章节列表</div>
                  <div className="panel-subtitle">{selectedProject?.title || "当前作品"} · {selectedProject?.genre || "未分类"}</div>
                </div>
                <div className="canvas-nav-stats">
                  <span>{chapters.length} 章</span>
                  <span>{totalWords.toLocaleString()} 字</span>
                </div>
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

            <form className={`panel chapter-editor ${contentExpanded ? "content-expanded" : ""} ${contextExpanded ? "context-expanded" : ""}`} onSubmit={saveChapter}>
              <div className="panel-header">
                <div><div className="panel-title">{selectedChapterId ? "编辑章节" : "新建章节"}</div></div>
                <div className="inline-actions">
                  {selectedChapterId ? <button type="button" className="danger-button" onClick={() => setPendingDeleteChapter(selectedChapter)}>删除</button> : null}
                  <button className="primary-button">保存章节</button>
                </div>
              </div>
              <div className="panel-body form-grid">
                <div className="grid-3 chapter-meta-fields">
                  <div className="field"><label>章节序号</label><input type="number" value={draft.chapter_index} onChange={(e) => updateDraft("chapter_index", e.target.value)} /></div>
                  <div className="field"><label>章节标题</label><input value={draft.title || ""} onChange={(e) => updateDraft("title", e.target.value)} /></div>
                  <div className="field"><label>状态</label><select value={draft.status || "draft"} onChange={(e) => updateDraft("status", e.target.value)}><option value="draft">draft</option><option value="generating">generating</option><option value="reviewing">reviewing</option><option value="done">done</option></select></div>
                </div>
                <div className="field chapter-summary-field"><label>章节摘要</label><textarea value={draft.summary || ""} onChange={(e) => updateDraft("summary", e.target.value)} /></div>
                {draft.event_plan ? (
                  <section className="event-plan-summary">
                    <div>
                      <span>所属剧情事件</span>
                      <strong>{draft.event_plan.story_event_title || "未命名事件"}</strong>
                    </div>
                    <div>
                      <span>本章功能</span>
                      <strong>{draft.event_plan.function || "推进"}</strong>
                    </div>
                    <p>{draft.event_plan.core_event || "暂无事件计划。"}</p>
                    {draft.event_plan.ending_hook ? <em>章末钩子：{draft.event_plan.ending_hook}</em> : null}
                  </section>
                ) : null}

                <section className="collapse-block chapter-content-block">
                  <div className="collapse-head">
                    <div>
                      <div className="panel-title">正文草稿</div>
                    </div>
                    <div className="inline-actions">
                      <span className="tag">{(draft.content || "").length.toLocaleString()} 字符</span>
                      <button type="button" className="secondary-button" onClick={() => router.push(`/chapter-canvas?novel=${selectedNovelId}`)}>去画布阅读</button>
                      <button type="button" className="ghost-button" onClick={() => setContentExpanded((value) => !value)}>
                        {contentExpanded ? "收起正文" : "展开正文"}
                      </button>
                    </div>
                  </div>
                  {contentExpanded ? (
                    <div className="field collapse-body">
                      <textarea className="chapter-content-input" value={draft.content || ""} onChange={(e) => updateDraft("content", e.target.value)} />
                    </div>
                  ) : (
                    <div className="chapter-content-summary">
                      {(draft.content || "").trim() ? `${draft.content.trim().slice(0, 160)}${draft.content.trim().length > 160 ? "..." : ""}` : "当前章节还没有正文内容。"}
                    </div>
                  )}
                </section>

                <section className="collapse-block context-block">
                  <div className="collapse-head">
                    <div>
                      <div className="panel-title">上下文快照</div>
                    </div>
                    <button type="button" className="ghost-button" onClick={() => setContextExpanded((value) => !value)}>
                      {contextExpanded ? "收起快照" : "展开快照"}
                    </button>
                  </div>
                  {draft.context_snapshot?.schema_version ? (
                    <>
                      <div className="grid-4">
                        <div className="mini-stat"><span>最近章节</span><strong>{draft.context_snapshot.stats?.recent_chapter_count ?? 0}</strong></div>
                        <div className="mini-stat"><span>结构化记忆</span><strong>{draft.context_snapshot.stats?.memory_count ?? 0}</strong></div>
                        <div className="mini-stat"><span>伏笔</span><strong>{draft.context_snapshot.stats?.foreshadowing_count ?? 0}</strong></div>
                        <div className="mini-stat"><span>审校记录</span><strong>{draft.context_snapshot.stats?.open_review_issue_count ?? 0}</strong></div>
                      </div>
                      {contextExpanded ? <pre className="json-preview">{JSON.stringify(draft.context_snapshot, null, 2)}</pre> : null}
                    </>
                  ) : (
                    <div className="hint-panel">当前章节还没有上下文快照。通过工作台开始自动生成后，Worker 会自动写入。</div>
                  )}
                </section>
              </div>
            </form>
          </section>

          {selectedChapterId ? (
            <ReviewIssuePanel
              title="本章审校记录"
              issues={chapterIssues}
              emptyTitle="本章暂无审校记录"
              emptyDescription="章节生成后的系统审校结果会显示在这里。"
            />
          ) : null}
          {pendingDeleteChapter ? (
            <div className="modal-backdrop" role="presentation" onClick={() => setPendingDeleteChapter(null)}>
              <div className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-chapter-title" onClick={(event) => event.stopPropagation()}>
                <div>
                  <div className="panel-title" id="delete-chapter-title">删除章节</div>
                  <div className="panel-subtitle">删除后，该章节相关记忆、审校记录和事件计划会同步清理。</div>
                </div>
                <div className="delete-preview">
                  <span>第 {pendingDeleteChapter.chapter_index} 章</span>
                  <strong>{pendingDeleteChapter.title || "未命名章节"}</strong>
                  <p>{pendingDeleteChapter.summary || "该章节暂无摘要。"}</p>
                </div>
                <div className="inline-actions dialog-actions">
                  <button className="secondary-button" disabled={deletingChapterId === pendingDeleteChapter.id} onClick={() => setPendingDeleteChapter(null)}>取消</button>
                  <button className="danger-button" disabled={deletingChapterId === pendingDeleteChapter.id} onClick={deleteChapter}>
                    {deletingChapterId === pendingDeleteChapter.id ? "删除中" : "确认删除"}
                  </button>
                </div>
              </div>
            </div>
          ) : null}
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
