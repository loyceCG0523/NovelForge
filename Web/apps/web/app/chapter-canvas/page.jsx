"use client";

import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import AppShell from "@/components/AppShell";
import EmptyState from "@/components/EmptyState";
import { apiFetch } from "@/lib/api";

function splitParagraphs(content) {
  return (content || "")
    // 兼容旧章节的单换行与新章节的空行分段，避免浏览器把换行折叠成普通空白。
    .split(/\r?\n+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function ChapterCanvasContent() {
  // 章节画布只负责阅读和检查生成结果，不承担编辑职责。
  const router = useRouter();
  const searchParams = useSearchParams();
  const [projects, setProjects] = useState([]);
  const [selectedNovelId, setSelectedNovelId] = useState("");
  const [chapters, setChapters] = useState([]);
  const [selectedChapterId, setSelectedChapterId] = useState("");
  const [error, setError] = useState("");
  const readerTopRef = useRef(null);

  const selectedProject = useMemo(
    () => projects.find((project) => project.id === selectedNovelId),
    [projects, selectedNovelId]
  );
  const selectedChapter = useMemo(
    () => chapters.find((chapter) => chapter.id === selectedChapterId),
    [chapters, selectedChapterId]
  );
  const paragraphs = useMemo(
    () => splitParagraphs(selectedChapter?.content),
    [selectedChapter]
  );
  const totalWords = chapters.reduce((sum, chapter) => sum + chapter.word_count, 0);

  useEffect(() => {
    if (!selectedChapterId) return;

    requestAnimationFrame(() => {
      readerTopRef.current?.scrollIntoView({
        block: "start",
        inline: "nearest",
        behavior: "auto"
      });
    });
  }, [selectedChapterId]);

  async function loadProjects() {
    const data = await apiFetch("/api/novels");
    setProjects(data);
    const nextNovelId = searchParams.get("novel") || selectedNovelId || data[0]?.id || "";
    setSelectedNovelId(nextNovelId);
    return nextNovelId;
  }

  async function loadChapters(novelId) {
    if (!novelId) return;
    const data = await apiFetch(`/api/novels/${novelId}/chapters`);
    setChapters(data);
    setSelectedChapterId((current) => {
      if (data.some((chapter) => chapter.id === current)) return current;
      return data[0]?.id || "";
    });
  }

  useEffect(() => {
    loadProjects().then(loadChapters).catch((err) => setError(err.message));
  }, [searchParams]);

  useEffect(() => {
    if (selectedNovelId) loadChapters(selectedNovelId).catch((err) => setError(err.message));
  }, [selectedNovelId]);

  return (
    <AppShell
      title="章节画布"
      actions={
        <>
          <select className="secondary-button" value={selectedNovelId} onChange={(event) => setSelectedNovelId(event.target.value)}>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}
          </select>
          <button className="secondary-button" disabled={!selectedNovelId} onClick={() => router.push(`/chapters?novel=${selectedNovelId}`)}>编辑章节</button>
          <button className="primary-button" disabled={!selectedNovelId} onClick={() => router.push(`/workbench?novel=${selectedNovelId}`)}>返回工作台</button>
        </>
      }
    >
      {projects.length === 0 ? (
        <EmptyState title="还没有作品" description="请先创建作品，再查看章节画布。" action={<a className="primary-button" href="/projects">去创建作品</a>} />
      ) : (
        <>
          {error ? <div className="error-box">{error}</div> : null}

          <section className="canvas-layout">
            <aside className="panel canvas-sidebar">
              <div className="panel-header">
                <div>
                  <div className="panel-title">章节导航</div>
                  <div className="panel-subtitle">{selectedProject?.title || "当前作品"} · {selectedProject?.genre || "未分类"}</div>
                </div>
                <div className="canvas-nav-stats">
                  <span>{chapters.length} 章</span>
                  <span>{totalWords.toLocaleString()} 字</span>
                </div>
              </div>
              <div className="panel-body stack-list">
                {chapters.length ? chapters.map((chapter) => (
                  <button
                    className={`chapter-row ${chapter.id === selectedChapterId ? "active" : ""}`}
                    key={chapter.id}
                    onClick={() => setSelectedChapterId(chapter.id)}
                  >
                    <span>第 {chapter.chapter_index} 章 · {chapter.status}</span>
                    <strong>{chapter.title || "未命名章节"}</strong>
                    <small>{chapter.word_count} 字</small>
                  </button>
                )) : (
                  <EmptyState title="暂无章节" description="从工作台开始自动生成，或先在章节管理中新建章节。" />
                )}
              </div>
            </aside>

            <article className="chapter-canvas" ref={readerTopRef}>
              {selectedChapter ? (
                <>
                  <header className="canvas-head">
                    <span>第 {selectedChapter.chapter_index} 章 · {selectedChapter.status}</span>
                    <h2>{selectedChapter.title || "未命名章节"}</h2>
                    <p>{selectedChapter.summary || "暂无章节摘要。"}</p>
                    <div className="canvas-meta">
                      <span>{selectedChapter.word_count} 字</span>
                      <span>上下文：{selectedChapter.context_snapshot?.schema_version ? "已保存" : "暂无"}</span>
                    </div>
                  </header>

                  <section className="reader-surface">
                    {paragraphs.length ? paragraphs.map((paragraph, index) => (
                      <p key={`${selectedChapter.id}-${index}`}>{paragraph}</p>
                    )) : (
                      <div className="hint-panel">当前章节还没有正文内容。</div>
                    )}
                  </section>

                  <footer className="canvas-inspector">
                    <div className="mini-stat"><span>最近章节</span><strong>{selectedChapter.context_snapshot?.stats?.recent_chapter_count ?? 0}</strong></div>
                    <div className="mini-stat"><span>结构化记忆</span><strong>{selectedChapter.context_snapshot?.stats?.memory_count ?? 0}</strong></div>
                    <div className="mini-stat"><span>伏笔</span><strong>{selectedChapter.context_snapshot?.stats?.foreshadowing_count ?? 0}</strong></div>
                    <div className="mini-stat"><span>审校记录</span><strong>{selectedChapter.context_snapshot?.stats?.open_review_issue_count ?? 0}</strong></div>
                  </footer>
                </>
              ) : (
                <EmptyState title="请选择章节" description="左侧选择章节后，会在这里显示完整正文。" />
              )}
            </article>
          </section>
        </>
      )}
    </AppShell>
  );
}

export default function ChapterCanvasPage() {
  return (
    <Suspense fallback={<main className="route-loading">正在载入章节画布...</main>}>
      <ChapterCanvasContent />
    </Suspense>
  );
}
