"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import AppShell from "@/components/AppShell";
import EmptyState from "@/components/EmptyState";
import { apiFetch } from "@/lib/api";


const EMPTY_MEME_FORM = {
  phrase: "",
  meaning: "",
  origin_event: "",
  suitable_scenes: "",
  popularity_period: "",
  source_urls: ""
};


export default function MemeLibraryPage() {
  const fileRef = useRef(null);
  const [entries, setEntries] = useState([]);
  const [query, setQuery] = useState("");
  const [sourceType, setSourceType] = useState("all");
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [editor, setEditor] = useState(null);
  const [form, setForm] = useState(EMPTY_MEME_FORM);

  async function loadEntries() {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (query.trim()) params.set("query", query.trim());
      params.set("source_type", sourceType);
      setEntries(await apiFetch(`/api/meme-library?${params.toString()}`));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadEntries().catch((err) => setError(err.message));
  }, [sourceType]);

  const stats = useMemo(() => ({
    builtin: entries.filter((item) => item.source_type === "builtin").length,
    user: entries.filter((item) => item.source_type === "user").length,
    indexed: entries.filter((item) => item.embedding_model).length
  }), [entries]);

  async function importFile(event) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setWorking("import");
    setMessage("");
    setError("");
    try {
      const formData = new FormData();
      formData.append("file", file);
      const result = await apiFetch("/api/meme-library/import", {
        method: "POST",
        body: formData
      });
      setMessage(
        `导入 ${result.imported} 条，更新 ${result.updated} 条，跳过 ${result.skipped} 条；`
        + (result.index_status === "completed" ? `已建立 ${result.indexed} 条向量。` : "向量将在配置 Qwen 后建立。")
      );
      if (result.errors?.length) setError(result.errors.join("；"));
      await loadEntries();
    } catch (err) {
      setError(err.message);
    } finally {
      setWorking("");
    }
  }

  async function rebuildIndex() {
    setWorking("index");
    setMessage("");
    setError("");
    try {
      const result = await apiFetch("/api/meme-library/rebuild-index", { method: "POST" });
      setMessage(result.message);
      await loadEntries();
    } catch (err) {
      setError(err.message);
    } finally {
      setWorking("");
    }
  }

  async function toggleEntry(entry) {
    setWorking(entry.id);
    setError("");
    try {
      const updated = await apiFetch(`/api/meme-library/${entry.id}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: !entry.enabled })
      });
      setEntries((current) => current.map((item) => item.id === entry.id ? updated : item));
    } catch (err) {
      setError(err.message);
    } finally {
      setWorking("");
    }
  }

  function openCreateEditor() {
    setEditor({ mode: "create", entryId: "" });
    setForm(EMPTY_MEME_FORM);
    setMessage("");
    setError("");
  }

  function openEditEditor(entry) {
    setEditor({ mode: "edit", entryId: entry.id });
    setForm({
      phrase: entry.phrase || "",
      meaning: entry.meaning || "",
      origin_event: entry.origin_event || "",
      suitable_scenes: entry.suitable_scenes || "",
      popularity_period: entry.popularity_period || "",
      source_urls: (entry.source_urls || []).join("\n")
    });
    setMessage("");
    setError("");
  }

  function updateForm(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function saveEntry(event) {
    event.preventDefault();
    if (!editor) return;
    setWorking("save");
    setMessage("");
    setError("");
    const payload = {
      ...form,
      source_urls: form.source_urls
        .split(/\r?\n|\|/)
        .map((item) => item.trim())
        .filter(Boolean)
    };
    try {
      const path = editor.mode === "create"
        ? "/api/meme-library"
        : `/api/meme-library/${editor.entryId}`;
      const saved = await apiFetch(path, {
        method: editor.mode === "create" ? "POST" : "PATCH",
        body: JSON.stringify(payload)
      });
      setEntries((current) => (
        editor.mode === "create"
          ? [saved, ...current]
          : current.map((item) => item.id === saved.id ? saved : item)
      ));
      setMessage(
        editor.mode === "create"
          ? "热梗已新增；向量会在下次检索或重建索引时生成。"
          : "热梗已更新；旧向量已作废，将在下次检索或重建索引时更新。"
      );
      setEditor(null);
      setForm(EMPTY_MEME_FORM);
    } catch (err) {
      setError(err.message);
    } finally {
      setWorking("");
    }
  }

  async function removeEntry(entry) {
    if (!window.confirm(`删除用户热梗“${entry.phrase}”？`)) return;
    setWorking(entry.id);
    setError("");
    try {
      await apiFetch(`/api/meme-library/${entry.id}`, { method: "DELETE" });
      setEntries((current) => current.filter((item) => item.id !== entry.id));
      if (editor?.entryId === entry.id) setEditor(null);
      setMessage(`已删除用户热梗“${entry.phrase}”。`);
    } catch (err) {
      setError(err.message);
    } finally {
      setWorking("");
    }
  }

  return (
    <AppShell
      title="热梗库"
      subtitle="系统内置审核库 + 用户扩展库；章节生成前使用 Qwen RAG 按人物关系和交流场景检索"
      actions={<span className="tag purple">只检索数据库，不联网搜梗</span>}
    >
      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">热梗知识库</div>
            <div className="panel-subtitle">内置库只读；你可以按同样的6列表头导入 XLSX 或 UTF-8 CSV 扩展库。</div>
          </div>
          <div className="inline-actions">
            <button className="secondary-button" disabled={Boolean(working)} onClick={openCreateEditor}>
              新增热梗
            </button>
            <input ref={fileRef} hidden type="file" accept=".xlsx,.csv" onChange={importFile} />
            <button className="secondary-button" disabled={Boolean(working)} onClick={() => fileRef.current?.click()}>
              {working === "import" ? "导入中…" : "导入扩展库"}
            </button>
            <button className="primary-button" disabled={Boolean(working)} onClick={rebuildIndex}>
              {working === "index" ? "索引中…" : "重建 Qwen 索引"}
            </button>
          </div>
        </div>
        <div className="panel-body settings-stack">
          {(message || error) ? <div className={error ? "error-box" : "success-box"}>{error || message}</div> : null}
          {editor ? (
            <form className="meme-library-editor" onSubmit={saveEntry}>
              <div className="meme-library-editor-header">
                <div>
                  <strong>{editor.mode === "create" ? "手动新增热梗" : "编辑用户热梗"}</strong>
                  <span>修改原词、真实含义或适用场景后，旧向量会自动失效。</span>
                </div>
                <button className="text-button" type="button" onClick={() => setEditor(null)}>取消</button>
              </div>
              <div className="meme-library-editor-grid">
                <div className="field">
                  <label>热梗原词</label>
                  <input required maxLength={120} value={form.phrase} onChange={(event) => updateForm("phrase", event.target.value)} />
                </div>
                <div className="field">
                  <label>流行时间</label>
                  <input required maxLength={80} placeholder="如：2025-11~2026-01" value={form.popularity_period} onChange={(event) => updateForm("popularity_period", event.target.value)} />
                </div>
                <div className="field meme-editor-wide">
                  <label>真实含义</label>
                  <textarea required maxLength={1000} rows={3} value={form.meaning} onChange={(event) => updateForm("meaning", event.target.value)} />
                </div>
                <div className="field meme-editor-wide">
                  <label>出处事件</label>
                  <textarea required maxLength={1600} rows={3} value={form.origin_event} onChange={(event) => updateForm("origin_event", event.target.value)} />
                </div>
                <div className="field meme-editor-wide">
                  <label>适用人物关系、情绪与场景</label>
                  <textarea required maxLength={1200} rows={4} value={form.suitable_scenes} onChange={(event) => updateForm("suitable_scenes", event.target.value)} />
                </div>
                <div className="field meme-editor-wide">
                  <label>来源链接</label>
                  <textarea required rows={2} placeholder="每行一个 http:// 或 https:// 链接，最多 5 个" value={form.source_urls} onChange={(event) => updateForm("source_urls", event.target.value)} />
                </div>
              </div>
              <div className="meme-library-editor-actions">
                <button className="secondary-button" type="button" disabled={working === "save"} onClick={() => setEditor(null)}>取消</button>
                <button className="primary-button" type="submit" disabled={working === "save"}>
                  {working === "save" ? "保存中…" : editor.mode === "create" ? "新增并保存" : "保存修改"}
                </button>
              </div>
            </form>
          ) : null}
          <div className="meme-library-stats">
            <div><span>系统内置</span><strong>{stats.builtin}</strong></div>
            <div><span>用户扩展</span><strong>{stats.user}</strong></div>
            <div><span>已建立向量</span><strong>{stats.indexed}</strong></div>
          </div>
          <div className="meme-library-toolbar">
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter") loadEntries().catch((err) => setError(err.message)); }}
              placeholder="搜索热梗、含义或适用场景"
            />
            <select value={sourceType} onChange={(event) => setSourceType(event.target.value)}>
              <option value="all">全部来源</option>
              <option value="builtin">系统内置</option>
              <option value="user">用户扩展</option>
            </select>
            <button className="secondary-button" onClick={() => loadEntries().catch((err) => setError(err.message))}>搜索</button>
          </div>
          {loading ? <div className="route-loading">正在加载热梗库…</div> : entries.length === 0 ? (
            <EmptyState title="没有匹配条目" description="调整筛选条件，或手动新增、导入符合6列表头的扩展库文件。" />
          ) : (
            <div className="meme-library-list">
              {entries.map((entry) => (
                <article className={`meme-library-row ${entry.enabled ? "" : "is-disabled"}`} key={entry.id}>
                  <div className="meme-library-row-main">
                    <div className="meme-library-row-title">
                      <h2>{entry.phrase}</h2>
                      <span className={`tag ${entry.source_type === "builtin" ? "green" : "purple"}`}>
                        {entry.source_type === "builtin" ? "系统内置" : "用户扩展"}
                      </span>
                      <span className="memory-range">{entry.popularity_period}</span>
                    </div>
                    <p>{entry.meaning}</p>
                    <small>{entry.suitable_scenes}</small>
                    <small className="meme-library-origin">出处：{entry.origin_event}</small>
                  </div>
                  <div className="meme-library-row-actions">
                    {entry.source_urls?.[0] ? <a className="secondary-button compact-button" href={entry.source_urls[0]} target="_blank" rel="noreferrer">来源</a> : null}
                    {entry.source_type === "user" ? (
                      <>
                        <button className="secondary-button compact-button" disabled={working === entry.id} onClick={() => openEditEditor(entry)}>
                          编辑
                        </button>
                        <button className="secondary-button compact-button" disabled={working === entry.id} onClick={() => toggleEntry(entry)}>
                          {entry.enabled ? "停用" : "启用"}
                        </button>
                        <button className="danger-button" disabled={working === entry.id} onClick={() => removeEntry(entry)}>删除</button>
                      </>
                    ) : null}
                  </div>
                </article>
              ))}
            </div>
          )}
        </div>
      </section>
    </AppShell>
  );
}
