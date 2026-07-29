"use client";

import { useEffect, useRef, useState } from "react";

import AppShell from "@/components/AppShell";
import EmptyState from "@/components/EmptyState";
import { apiDownload, apiFetch } from "@/lib/api";

const defaultForm = {
  sample_title: "",
  source_author: "",
  source_genre: ""
};

const statusLabels = {
  queued: "排队中",
  running: "分析中",
  completed: "已完成",
  active: "已完成",
  failed: "失败"
};

function formatNumber(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (Number.isNaN(number)) return String(value);
  return `${Number.isInteger(number) ? number.toLocaleString() : number.toLocaleString(undefined, { maximumFractionDigits: 2 })}${suffix}`;
}

function formatBytes(value) {
  const number = Number(value);
  if (Number.isNaN(number) || number <= 0) return "-";
  if (number >= 1024 * 1024) return `${(number / 1024 / 1024).toFixed(2)} MB`;
  if (number >= 1024) return `${(number / 1024).toFixed(1)} KB`;
  return `${number} B`;
}

function filenameToTitle(filename) {
  return filename.replace(/\.[^.]+$/, "").trim();
}

function statusTone(status) {
  if (status === "failed") return "red";
  if (status === "completed" || status === "active") return "green";
  return "yellow";
}

function progressPercent(analysis) {
  if (!analysis.chunk_count) return analysis.status === "running" ? 18 : 0;
  return Math.min(96, Math.max(12, Math.round((analysis.analyzed_chunk_count / analysis.chunk_count) * 100)));
}

function splitGenreTags(value) {
  const text = String(value || "").trim();
  if (!text) return ["未标注题材"];
  const bracketed = [...text.matchAll(/(?:【|\[)\s*([^】\]]+)\s*(?:】|\])/g)]
    .map((match) => match[1].trim())
    .filter(Boolean);
  if (bracketed.length > 1) return bracketed.slice(0, 10);
  return text.split(/[、,，/|]+/).map((item) => item.trim()).filter(Boolean).slice(0, 10);
}

function MetricTile({ label, value }) {
  return (
    <div className="sample-metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function GuidelineList({ title, items }) {
  if (!items?.length) return null;
  return (
    <section className="sample-section-card compact">
      <h2>{title}</h2>
      <ul className="sample-guideline-list">
        {items.map((item) => <li key={item}>{item}</li>)}
      </ul>
    </section>
  );
}

function SampleReportDetail({ analysis }) {
  const report = analysis.report || {};
  const profile = report.reference_profile || {};
  const experience = report.experience_summary || {};
  const ragIndex = report.rag_index || {};

  if (!["completed", "active"].includes(analysis.status)) {
    return (
      <div className="sample-pending-box">
        <strong>{analysis.summary}</strong>
        <span>已分析分片：{analysis.analyzed_chunk_count || 0}，当前文件：{analysis.source_file_name}</span>
      </div>
    );
  }

  return (
    <div className="sample-detail">
      <div className="sample-metric-grid">
        <MetricTile label="表达经验" value={formatNumber(experience.expression_experience_count ?? ragIndex.expression_experience_count)} />
        <MetricTile label="剧情经验" value={formatNumber(experience.plot_experience_count ?? ragIndex.plot_experience_count)} />
        <MetricTile label="Embedding 模型" value={ragIndex.embedding_model || "-"} />
        <MetricTile label="并行分析" value={experience.part_count ? `${experience.part_count} 份` : "-"} />
      </div>

      <div className="sample-section-grid">
        <section className="sample-section-card sample-reference-summary">
          <h2>语言表达参考</h2>
          {profile.available ? (
            <div className="sample-strategy-summary">
              <span className="tag purple">{profile.model || "LLM"}</span>
              <p>{profile.summary || "已生成少量可执行的语言表达规则。"}</p>
            </div>
          ) : (
            <div className="sample-pending-box">
              <strong>经验文档尚未生成</strong>
              <span>{profile.reason || "请重新分析样本，生成剧情与表达经验文档。"}</span>
            </div>
          )}
        </section>

        <section className="sample-section-card sample-reference-summary">
          <h2>剧情设计参考</h2>
          <p>事件规划时从大模型总结的剧情经验中检索完整机制，不再直接检索固定字符原文窗口。</p>
          {ragIndex.reason ? <p>{ragIndex.reason}</p> : null}
        </section>

        <GuidelineList title="语言规则" items={profile.language_rules} />
        <GuidelineList title="反 AI 味约束" items={profile.anti_ai_rules} />
      </div>
    </div>
  );
}

function SampleAnalysisContent() {
  const fileInputRef = useRef(null);
  const [analyses, setAnalyses] = useState([]);
  const [expandedAnalysisIds, setExpandedAnalysisIds] = useState([]);
  const [form, setForm] = useState(defaultForm);
  const [selectedFile, setSelectedFile] = useState(null);
  const [loading, setLoading] = useState(false);
  const [deletingId, setDeletingId] = useState("");
  const [reindexingId, setReindexingId] = useState("");
  const [downloadingId, setDownloadingId] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  async function loadAnalyses() {
    const data = await apiFetch("/api/sample-analyses");
    setAnalyses(data);
    setExpandedAnalysisIds((current) => {
      const validIds = new Set(data.map((item) => item.id));
      return current.filter((id) => validIds.has(id));
    });
  }

  function updateForm(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function handleFile(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    setSelectedFile(file);
    setForm((current) => ({
      ...current,
      sample_title: current.sample_title || filenameToTitle(file.name)
    }));
  }

  function toggleExpanded(analysisId) {
    setExpandedAnalysisIds((current) => (
      current.includes(analysisId)
        ? current.filter((id) => id !== analysisId)
        : [analysisId, ...current]
    ));
  }

  async function createAnalysis(event) {
    event.preventDefault();
    if (!selectedFile) {
      setError("请先选择 TXT 或 MD 样本文本文件。");
      return;
    }
    setLoading(true);
    setMessage("");
    setError("");
    try {
      const formData = new FormData();
      formData.append("sample_title", form.sample_title);
      formData.append("source_author", form.source_author);
      formData.append("source_genre", form.source_genre);
      formData.append("file", selectedFile);
      const created = await apiFetch("/api/sample-analyses", {
        method: "POST",
        body: formData
      });
      setAnalyses((current) => [created, ...current]);
      setForm(defaultForm);
      setSelectedFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      setMessage("样本已保存，Worker 将最多十路并行生成创作经验文档。");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function deleteAnalysis(analysisId) {
    if (!analysisId) return;
    setDeletingId(analysisId);
    setMessage("");
    setError("");
    try {
      await apiFetch(`/api/sample-analyses/${analysisId}`, { method: "DELETE" });
      setAnalyses((current) => current.filter((item) => item.id !== analysisId));
      setExpandedAnalysisIds((current) => current.filter((id) => id !== analysisId));
      setMessage("样本分析报告已删除。");
    } catch (err) {
      setError(err.message);
    } finally {
      setDeletingId("");
    }
  }

  async function reindexAnalysis(analysisId) {
    setReindexingId(analysisId);
    setMessage("");
    setError("");
    try {
      const updated = await apiFetch(`/api/sample-analyses/${analysisId}/reindex`, {
        method: "POST"
      });
      setAnalyses((current) => current.map((item) => (
        item.id === analysisId ? { ...item, ...updated } : item
      )));
      setMessage("样本已进入队列，将重新分析并补建剧情与语言 RAG 索引。");
    } catch (err) {
      setError(err.message);
    } finally {
      setReindexingId("");
    }
  }

  async function downloadExperience(analysis) {
    setDownloadingId(analysis.id);
    setError("");
    try {
      await apiDownload(
        `/api/sample-analyses/${analysis.id}/experience-document`,
        `${analysis.sample_title || "样本"}-创作经验文档.md`
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setDownloadingId("");
    }
  }

  useEffect(() => {
    loadAnalyses().catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    if (analyses.every((item) => !["queued", "running"].includes(item.status))) return undefined;
    const timer = window.setInterval(async () => {
      try {
        await loadAnalyses();
      } catch (err) {
        setError(err.message);
      }
    }, 2500);
    return () => window.clearInterval(timer);
  }, [analyses]);

  return (
    <AppShell
      title="样本分析"
      subtitle="最多十路并行拆解优秀作品，生成剧情设计与精彩表达经验文档"
      actions={<span className="tag purple">独立样本库</span>}
    >
      <section className="sample-layout">
          <section className="panel sample-form-panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">上传优秀样本</div>
                <div className="panel-subtitle">TXT/MD 原文最多拆成 10 份，由事件规划/审校模型并行总结后建立 Qwen 索引。</div>
              </div>
            </div>
            <form className="panel-body sample-form" onSubmit={createAnalysis}>
              {(message || error) ? <div className={error ? "error-box" : "hint-panel"}>{error || message}</div> : null}
              <label className="field">
                <span>样本作品名</span>
                <input value={form.sample_title} onChange={(event) => updateForm("sample_title", event.target.value)} placeholder="例如：某部同题材爆款小说" required />
              </label>
              <div className="form-grid">
                <label className="field">
                  <span>作者/来源</span>
                  <input value={form.source_author} onChange={(event) => updateForm("source_author", event.target.value)} placeholder="可选" />
                </label>
                <label className="field">
                  <span>题材</span>
                  <input value={form.source_genre} onChange={(event) => updateForm("source_genre", event.target.value)} placeholder="例如：男频校园爱情" />
                </label>
              </div>
              <div className="field">
                <span>上传文本文件</span>
                <input ref={fileInputRef} className="sample-file-input" type="file" accept=".txt,.md,.text" onChange={handleFile} />
                <button className="sample-file-picker" type="button" onClick={() => fileInputRef.current?.click()}>
                  <span>选择 TXT/MD 文件</span>
                  <strong>{selectedFile ? selectedFile.name : "未选择文件"}</strong>
                </button>
              </div>
              {selectedFile ? (
                <div className="sample-file-note">
                  {selectedFile.name} · {formatBytes(selectedFile.size)} · 上传后自动保存并进入异步分析
                </div>
              ) : null}
              <button className="primary-button" disabled={loading || !selectedFile || !form.sample_title.trim()}>
                {loading ? "上传中..." : "保存样本并开始分析"}
              </button>
            </form>
          </section>

          <section className="panel sample-library-panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">样本报告库</div>
                <div className="panel-subtitle">用户级样本资产 · 已保存 {analyses.length} 份报告</div>
              </div>
              <span className="tag purple">SampleAnalysisAgent</span>
            </div>
            <div className="panel-body">
              {analyses.length === 0 ? (
                <EmptyState title="暂无样本分析报告" description="上传优秀作品后，系统会生成剧情设计与精彩表达经验文档。" />
              ) : (
                <div className="sample-list">
                  {analyses.map((analysis) => {
                    const expanded = expandedAnalysisIds.includes(analysis.id);
                    const profileSummary = analysis.report?.reference_profile?.summary;
                    const genreTags = splitGenreTags(analysis.source_genre);
                    return (
                      <article className={`sample-row ${expanded ? "active" : ""}`} key={analysis.id}>
                        <div className="sample-row-main">
                          <div className="sample-row-heading">
                            <div className="sample-row-copy">
                              <div className="sample-row-title-line">
                                <div className="sample-row-title">{analysis.sample_title}</div>
                                <span className={`tag ${statusTone(analysis.status)}`}>{statusLabels[analysis.status] || analysis.status}</span>
                              </div>
                              <p className="sample-row-summary">{profileSummary || analysis.summary}</p>
                            </div>
                            <div className="sample-row-actions">
                              <button className="secondary-button" type="button" onClick={() => toggleExpanded(analysis.id)}>
                                {expanded ? "收起" : "展开"}
                              </button>
                              {["completed", "active", "failed"].includes(analysis.status) ? (
                                <button
                                  className="secondary-button"
                                  type="button"
                                  disabled={reindexingId === analysis.id}
                                  onClick={() => reindexAnalysis(analysis.id)}
                                  title="重新并行总结样本并更新经验索引"
                                >
                                  {reindexingId === analysis.id ? "入队中..." : "重新分析"}
                                </button>
                              ) : null}
                              {analysis.report?.schema_version === "sample_experience.v1" ? (
                                <button
                                  className="secondary-button"
                                  type="button"
                                  disabled={downloadingId === analysis.id}
                                  onClick={() => downloadExperience(analysis)}
                                >
                                  {downloadingId === analysis.id ? "下载中..." : "下载经验文档"}
                                </button>
                              ) : null}
                              <button
                                className="danger-button"
                                type="button"
                                disabled={deletingId === analysis.id}
                                onClick={() => deleteAnalysis(analysis.id)}
                              >
                                {deletingId === analysis.id ? "删除中..." : "删除"}
                              </button>
                            </div>
                          </div>
                          <div className="sample-row-meta">
                            <div className="sample-genre-tags">
                              {genreTags.map((tag) => <span className="tag green" key={tag}>{tag}</span>)}
                            </div>
                            <div className="sample-file-tags">
                              <span>已保存</span>
                              <span>{formatBytes(analysis.source_file_size)}</span>
                              {analysis.source_word_count ? <span>{formatNumber(analysis.source_word_count, " 字")}</span> : null}
                              {analysis.chunk_count ? <span>分片 {analysis.analyzed_chunk_count}/{analysis.chunk_count}</span> : null}
                            </div>
                          </div>
                          {analysis.status === "running" ? (
                            <div className="progress sample-progress"><span style={{ width: `${progressPercent(analysis)}%` }} /></div>
                          ) : null}
                          {analysis.error_message ? <div className="error-box compact-error">{analysis.error_message}</div> : null}
                        </div>
                        {expanded ? <div className="sample-expand-area"><SampleReportDetail analysis={analysis} /></div> : null}
                      </article>
                    );
                  })}
                </div>
              )}
            </div>
          </section>
        </section>
    </AppShell>
  );
}

export default function SampleAnalysisPage() {
  return <SampleAnalysisContent />;
}
