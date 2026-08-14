"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";

import AppShell from "@/components/AppShell";
import EmptyState from "@/components/EmptyState";
import { apiFetch, apiFetchCached, getCachedApiData } from "@/lib/api";

const providerLabels = {
  deepseek_web_search: "DeepSeek Web Search",
  tavily: "Tavily",
};

function providerLabel(provider) {
  return providerLabels[provider] || provider || "网络检索";
}

function ResearchContent() {
  const searchParams = useSearchParams();
  const novelIdFromUrl = searchParams.get("novel");
  const initialProjects = getCachedApiData("/api/novels");
  const [projects, setProjects] = useState(() => initialProjects || []);
  const [selectedId, setSelectedId] = useState(() => novelIdFromUrl || initialProjects?.[0]?.id || "");
  const [sources, setSources] = useState([]);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [deletingId, setDeletingId] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const selectedProject = useMemo(() => projects.find((item) => item.id === selectedId), [projects, selectedId]);

  async function loadProjects() {
    const data = await apiFetchCached("/api/novels", { ttlMs: 300_000 });
    setProjects(data);
    const nextId = novelIdFromUrl || selectedId || data[0]?.id || "";
    setSelectedId(nextId);
    return nextId;
  }

  async function loadSources(novelId, { force = false } = {}) {
    if (novelId) {
      setSources(await apiFetchCached(`/api/novels/${novelId}/research`, { ttlMs: 30_000, force }));
    }
  }

  useEffect(() => { loadProjects().catch((err) => setError(err.message)); }, [novelIdFromUrl]);
  useEffect(() => { if (selectedId) loadSources(selectedId).catch((err) => setError(err.message)); }, [selectedId]);

  async function search() {
    if (!selectedId || query.trim().length < 2) return;
    setSearching(true); setError(""); setMessage("");
    try {
      const results = await apiFetch(`/api/novels/${selectedId}/research/search`, {
        method: "POST",
        body: JSON.stringify({ query: query.trim(), max_results: 5, search_depth: "basic" })
      });
      setSources((current) => [...results, ...current]);
      setMessage(results.length ? `已保存 ${results.length} 条研究来源` : "未找到可保存的来源，请调整检索词。");
      setQuery("");
    } catch (err) { setError(err.message); } finally { setSearching(false); }
  }

  async function removeSource(sourceId) {
    setDeletingId(sourceId); setError("");
    try {
      await apiFetch(`/api/novels/${selectedId}/research/${sourceId}`, { method: "DELETE" });
      setSources((current) => current.filter((item) => item.id !== sourceId));
    } catch (err) { setError(err.message); } finally { setDeletingId(""); }
  }

  return <AppShell title="资料检索" actions={<select className="secondary-button" value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>{projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}</select>}>
    {projects.length === 0 ? <EmptyState title="还没有作品" description="先创建作品，再为它检索可引用的现实资料。" action={<a className="primary-button" href="/projects">去创建作品</a>} /> : <section className="panel memory-panel">
      <div className="panel-header"><div><div className="panel-title">{selectedProject?.title || "当前作品"}</div></div></div>
      <div className="panel-body stack-list">
        {(message || error) ? <div className={error ? "error-box" : "success-box"}>{error || message}</div> : null}
        <div className="inline-actions"><input className="research-query-input" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") search(); }} placeholder="例如：上海 2000 年代老小区租房生活细节" /><button className="primary-button" disabled={searching || query.trim().length < 2} onClick={search}>{searching ? "检索中" : "开始检索"}</button></div>
        {sources.length === 0 ? <EmptyState title="暂无研究资料" description="请先配置可用的网络检索，再输入问题开始检索。" /> : <div className="memory-grid">{sources.map((source) => <article className="memory-card" key={source.id}><div className="memory-card-head"><span className="tag purple">{providerLabel(source.provider)}</span><span className="memory-range">{source.domain || "网页来源"}</span></div><h2>{source.title}</h2><p>{source.snippet || "该来源未返回可展示摘要。"}</p><div className="memory-card-actions"><a className="secondary-button compact-button" href={source.url} target="_blank" rel="noreferrer">查看来源</a><button className="danger-button" disabled={deletingId === source.id} onClick={() => removeSource(source.id)}>{deletingId === source.id ? "删除中" : "删除"}</button></div></article>)}</div>}
      </div>
    </section>}
  </AppShell>;
}

export default function ResearchPage() { return <Suspense fallback={<div className="route-loading">正在加载资料检索…</div>}><ResearchContent /></Suspense>; }
