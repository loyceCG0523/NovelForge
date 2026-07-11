"use client";

import { useEffect, useRef, useState } from "react";

import AppShell from "@/components/AppShell";
import EmptyState from "@/components/EmptyState";
import { apiFetch } from "@/lib/api";

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

const vectorLabels = {
  target_sentence_avg: "目标句长均值",
  short_sentence_ratio: "短句占比",
  dialogue_ratio: "对白占比",
  subtext_ratio: "潜台词比例",
  metaphor_density_per_1k_chars: "比喻密度/千字",
  sensory_covered_dimension_count: "感官覆盖维度",
  emotion_volatility: "情绪波动",
  foreshadowing_density_per_1k_chars: "伏笔密度/千字",
  payoff_cycle_chapters: "兑现周期/章",
  conflict_interval_chars: "冲突间隔/字",
  info_release_density_per_1k_chars: "信息释放密度/千字",
  ending_hook_strength: "章尾钩子强度"
};

const hookLabels = {
  action: "动作钩子",
  emotion: "情绪钩子",
  question: "悬念提问",
  reveal: "信息揭示",
  soft_pause: "自然停顿"
};

const emotionLabels = {
  positive: "正向",
  negative: "低谷",
  tension: "紧张",
  romance: "暧昧",
  neutral: "平稳"
};

function formatNumber(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (Number.isNaN(number)) return String(value);
  return `${Number.isInteger(number) ? number.toLocaleString() : number.toLocaleString(undefined, { maximumFractionDigits: 2 })}${suffix}`;
}

function formatRatio(value) {
  const number = Number(value);
  if (Number.isNaN(number)) return "-";
  return `${Math.round(number * 100)}%`;
}

function formatBytes(value) {
  const number = Number(value);
  if (Number.isNaN(number) || number <= 0) return "-";
  if (number >= 1024 * 1024) return `${(number / 1024 / 1024).toFixed(2)} MB`;
  if (number >= 1024) return `${(number / 1024).toFixed(1)} KB`;
  return `${number} B`;
}

function metricValue(key, value) {
  if (["short_sentence_ratio", "dialogue_ratio", "subtext_ratio", "ending_hook_strength"].includes(key)) {
    return formatRatio(value);
  }
  if (key === "target_sentence_avg" || key === "conflict_interval_chars") return formatNumber(value, " 字");
  if (key === "payoff_cycle_chapters") return formatNumber(value, " 章");
  return formatNumber(value);
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
  const style = report.style_fingerprint || {};
  const sentence = style.sentence_length || {};
  const sensory = style.sensory_coverage || {};
  const rhythm = style.paragraph_rhythm || {};
  const emotion = report.emotion_curve || {};
  const foreshadowing = report.foreshadowing_pattern || {};
  const dialogue = report.dialogue_style || {};
  const pacing = report.pacing_model || {};
  const vector = analysis.metrics || {};
  const strategy = report.llm_style_strategy || {};

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
        {Object.entries(vectorLabels).map(([key, label]) => (
          <MetricTile key={key} label={label} value={metricValue(key, vector[key])} />
        ))}
      </div>

      <div className="sample-section-grid">
        <section className="sample-section-card">
          <h2>文风指纹</h2>
          <dl className="sample-kv">
            <div><dt>句长均值</dt><dd>{formatNumber(sentence.avg, " 字")}</dd></div>
            <div><dt>句长 P90</dt><dd>{formatNumber(sentence.p90, " 字")}</dd></div>
            <div><dt>比喻密度</dt><dd>{formatNumber(style.metaphor_density_per_1k_chars, "/千字")}</dd></div>
            <div><dt>主感官维度</dt><dd>{sensory.dominant_dimension || "-"}</dd></div>
            <div><dt>短段落占比</dt><dd>{formatRatio(rhythm.short_paragraph_ratio)}</dd></div>
          </dl>
        </section>

        <section className="sample-section-card">
          <h2>情绪曲线</h2>
          <dl className="sample-kv">
            <div><dt>情绪波动</dt><dd>{formatNumber(emotion.volatility_avg_delta)}</dd></div>
            <div><dt>高潮章节</dt><dd>第 {emotion.peak_chapter || "-"} 章</dd></div>
            <div><dt>低谷章节</dt><dd>第 {emotion.valley_chapter || "-"} 章</dd></div>
          </dl>
          <div className="sample-chip-line">
            {(emotion.chapter_points || []).slice(0, 10).map((point) => (
              <span className="tag" key={point.chapter}>第 {point.chapter} 章 · {emotionLabels[point.label] || point.label}</span>
            ))}
          </div>
        </section>

        <section className="sample-section-card">
          <h2>伏笔模式</h2>
          <dl className="sample-kv">
            <div><dt>埋设密度</dt><dd>{formatNumber(foreshadowing.planting_density_per_1k_chars, "/千字")}</dd></div>
            <div><dt>兑现密度</dt><dd>{formatNumber(foreshadowing.payoff_density_per_1k_chars, "/千字")}</dd></div>
            <div><dt>平均兑现周期</dt><dd>{formatNumber(foreshadowing.estimated_payoff_cycle_chapters_avg, " 章")}</dd></div>
            <div><dt>误导密度</dt><dd>{formatNumber(foreshadowing.misdirection_strategy?.density_per_1k_chars, "/千字")}</dd></div>
          </dl>
        </section>

        <section className="sample-section-card">
          <h2>对白风格</h2>
          <dl className="sample-kv">
            <div><dt>对白占比</dt><dd>{formatRatio(dialogue.dialogue_ratio)}</dd></div>
            <div><dt>话语均长</dt><dd>{formatNumber(dialogue.utterance_length?.avg, " 字")}</dd></div>
            <div><dt>潜台词比例</dt><dd>{formatRatio(dialogue.subtext_ratio)}</dd></div>
            <div><dt>解释性对白</dt><dd>{formatRatio(dialogue.explanatory_dialogue_ratio)}</dd></div>
          </dl>
        </section>

        <section className="sample-section-card wide">
          <h2>节奏模型</h2>
          <dl className="sample-kv">
            <div><dt>冲突间隔</dt><dd>{formatNumber(pacing.conflict_interval_chars_est, " 字")}</dd></div>
            <div><dt>信息释放密度</dt><dd>{formatNumber(pacing.information_release_density_per_1k_chars, "/千字")}</dd></div>
            <div><dt>章尾钩子强度</dt><dd>{formatRatio(pacing.ending_hook_strength_avg)}</dd></div>
          </dl>
          <div className="sample-chip-line">
            {Object.entries(pacing.ending_hook_type_distribution || {}).map(([hook, count]) => (
              <span className="tag green" key={hook}>{hookLabels[hook] || hook} · {count}</span>
            ))}
          </div>
        </section>

        <section className="sample-section-card wide">
          <h2>LLM 风格策略</h2>
          {strategy.available ? (
            <div className="sample-strategy-summary">
              <span className="tag purple">{strategy.model || "LLM"}</span>
              <p>{strategy.style_summary || "已根据量化指标生成可迁移风格策略。"}</p>
            </div>
          ) : (
            <div className="sample-pending-box">
              <strong>当前仅保存量化指标</strong>
              <span>{strategy.reason || "未配置 LLM API Key，暂未生成高阶风格策略。"}</span>
            </div>
          )}
        </section>

        <GuidelineList title="生成约束" items={strategy.generation_guidelines} />
        <GuidelineList title="反 AI 味策略" items={strategy.anti_ai_guidelines} />
        <GuidelineList title="对白策略" items={strategy.dialogue_guidelines} />
        <GuidelineList title="节奏策略" items={strategy.pacing_guidelines} />
        <GuidelineList title="风险提醒" items={strategy.risk_notes} />
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
      setMessage("样本已保存为分析记录，Worker 会异步分片分析并沉淀报告。");
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
      subtitle="上传优秀小说样本，沉淀可复用的量化风格特征和 LLM 写作策略"
      actions={<span className="tag purple">独立样本库</span>}
    >
      <section className="sample-layout">
          <section className="panel sample-form-panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">上传优秀样本</div>
                <div className="panel-subtitle">TXT/MD 原文进入对象存储，报告保存为用户级样本库资产。</div>
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
                <EmptyState title="暂无样本分析报告" description="上传一份优秀作品文本后，系统会生成可迁移的工程化风格向量。" />
              ) : (
                <div className="sample-list">
                  {analyses.map((analysis) => {
                    const expanded = expandedAnalysisIds.includes(analysis.id);
                    return (
                      <article className={`sample-row ${expanded ? "active" : ""}`} key={analysis.id}>
                        <div className="sample-row-main">
                          <div className="sample-row-heading">
                            <div>
                              <div className="sample-row-title">{analysis.sample_title}</div>
                              <p>{analysis.summary}</p>
                            </div>
                            <div className="sample-row-actions">
                              <button className="secondary-button" type="button" onClick={() => toggleExpanded(analysis.id)}>
                                {expanded ? "收起" : "展开"}
                              </button>
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
                          <div className="memory-tags">
                            <span className={`tag ${statusTone(analysis.status)}`}>{statusLabels[analysis.status] || analysis.status}</span>
                            <span className="tag green">{analysis.source_genre || "未标注题材"}</span>
                            <span className="tag">已保存</span>
                            <span className="tag">{formatBytes(analysis.source_file_size)}</span>
                            {analysis.source_word_count ? <span className="tag">{formatNumber(analysis.source_word_count, " 字")}</span> : null}
                            {analysis.chunk_count ? <span className="tag">分片 {analysis.analyzed_chunk_count}/{analysis.chunk_count}</span> : null}
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
