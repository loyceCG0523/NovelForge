"use client";

import { useEffect, useState } from "react";

import AppShell from "@/components/AppShell";
import { apiFetch, setStoredUser } from "@/lib/api";

const defaultForm = {
  llm_base_url: "https://api.openai.com/v1",
  llm_model: "gpt-4.1-mini",
  llm_context_window_tokens: 128000,
  llm_api_key: "",
  review_llm_enabled: false,
  review_llm_base_url: "https://api.openai.com/v1",
  review_llm_model: "gpt-4.1-mini",
  review_llm_context_window_tokens: 128000,
  review_llm_api_key: "",
  embedding_enabled: false,
  embedding_endpoint: "",
  embedding_model: "qwen3.7-text-embedding",
  embedding_api_key: "",
  tavily_enabled: false,
  tavily_api_key: ""
};

const idleModelTest = { status: "idle", message: "", latencyMs: 0, model: "" };

export default function PersonalizationPage() {
  // 设置页只保留全局设置项；账号资料和退出登录由左下角用户菜单承载。
  const [form, setForm] = useState(defaultForm);
  const [savedForm, setSavedForm] = useState(defaultForm);
  const [apiKeyConfigured, setApiKeyConfigured] = useState(false);
  const [apiKeyMasked, setApiKeyMasked] = useState("");
  const [reviewApiKeyConfigured, setReviewApiKeyConfigured] = useState(false);
  const [reviewApiKeyMasked, setReviewApiKeyMasked] = useState("");
  const [embeddingApiKeyConfigured, setEmbeddingApiKeyConfigured] = useState(false);
  const [embeddingApiKeyMasked, setEmbeddingApiKeyMasked] = useState("");
  const [tavilyApiKeyConfigured, setTavilyApiKeyConfigured] = useState(false);
  const [tavilyApiKeyMasked, setTavilyApiKeyMasked] = useState("");
  const [editing, setEditing] = useState(false);
  const [editingKeys, setEditingKeys] = useState({
    writer: false,
    reviewer: false,
    embedding: false,
    tavily: false
  });
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [modelTests, setModelTests] = useState({
    writer: idleModelTest,
    reviewer: idleModelTest,
    embedding: idleModelTest
  });

  useEffect(() => {
    loadPreferences().catch((err) => setError(err.message));
  }, []);

  async function loadPreferences() {
    const user = await apiFetch("/api/auth/me");
    setStoredUser(user);
    const llm = user.preferences?.llm || {};
    const reviewLlm = user.preferences?.review_llm || {};
    const embedding = user.preferences?.embedding || {};
    const webSearch = user.preferences?.web_search || {};
    const nextForm = {
      ...defaultForm,
      llm_base_url: llm.base_url || defaultForm.llm_base_url,
      llm_model: llm.model || defaultForm.llm_model,
      llm_context_window_tokens: Number(llm.context_window_tokens) || defaultForm.llm_context_window_tokens,
      llm_api_key: "",
      review_llm_enabled: Boolean(reviewLlm.enabled),
      review_llm_base_url: reviewLlm.base_url || llm.base_url || defaultForm.review_llm_base_url,
      review_llm_model: reviewLlm.model || llm.model || defaultForm.review_llm_model,
      review_llm_context_window_tokens: Number(reviewLlm.context_window_tokens) || Number(llm.context_window_tokens) || defaultForm.review_llm_context_window_tokens,
      review_llm_api_key: "",
      embedding_enabled: Boolean(embedding.enabled),
      embedding_endpoint: embedding.endpoint || "",
      embedding_model: embedding.model || defaultForm.embedding_model,
      embedding_api_key: "",
      tavily_enabled: typeof webSearch.enabled === "boolean"
        ? webSearch.enabled
        : Boolean(webSearch.tavily_api_key_configured),
      tavily_api_key: ""
    };
    setForm(nextForm);
    setSavedForm(nextForm);
    setApiKeyConfigured(Boolean(llm.api_key_configured));
    setApiKeyMasked(llm.api_key_masked || "");
    setReviewApiKeyConfigured(Boolean(reviewLlm.api_key_configured));
    setReviewApiKeyMasked(reviewLlm.api_key_masked || "");
    setEmbeddingApiKeyConfigured(Boolean(embedding.api_key_configured));
    setEmbeddingApiKeyMasked(embedding.api_key_masked || "");
    setTavilyApiKeyConfigured(Boolean(webSearch.tavily_api_key_configured));
    setTavilyApiKeyMasked(webSearch.tavily_api_key_masked || "");
    setEditingKeys({ writer: false, reviewer: false, embedding: false, tavily: false });
  }

  function updateForm(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
    if (key.startsWith("llm_")) {
      setModelTests((current) => ({ ...current, writer: idleModelTest }));
    }
    if (key.startsWith("review_llm_")) {
      setModelTests((current) => ({ ...current, reviewer: idleModelTest }));
    }
    if (key.startsWith("embedding_")) {
      setModelTests((current) => ({ ...current, embedding: idleModelTest }));
    }
  }

  function cancelEdit() {
    setForm(savedForm);
    setEditing(false);
    setEditingKeys({ writer: false, reviewer: false, embedding: false, tavily: false });
    setMessage("");
    setError("");
    setModelTests({ writer: idleModelTest, reviewer: idleModelTest, embedding: idleModelTest });
  }

  async function testModelApi(target) {
    const reviewer = target === "reviewer";
    const baseUrl = reviewer ? form.review_llm_base_url : form.llm_base_url;
    const model = reviewer ? form.review_llm_model : form.llm_model;
    const contextWindowTokens = reviewer ? form.review_llm_context_window_tokens : form.llm_context_window_tokens;
    const apiKey = reviewer ? form.review_llm_api_key : form.llm_api_key;
    const hasSavedKey = reviewer ? reviewApiKeyConfigured : apiKeyConfigured;
    const targetLabel = reviewer ? "审校模型" : "正文模型";

    setModelTests((current) => ({
      ...current,
      [target]: { status: "testing", message: "正在发送最小测试请求…", latencyMs: 0, model }
    }));
    try {
      if (!baseUrl.trim()) throw new Error("请填写 Base URL。");
      if (!model.trim()) throw new Error("请填写模型名称。");
      if (Number(contextWindowTokens) < 4096) throw new Error("模型最大上下文不能小于 4096 Token。");
      if (!apiKey.trim() && !hasSavedKey) throw new Error("请填写 API Key，或先保存已有密钥。");

      const result = await apiFetch("/api/users/me/model-api/test", {
        method: "POST",
        body: JSON.stringify({
          target,
          base_url: baseUrl,
          model,
          api_key: apiKey,
          context_window_tokens: Number(contextWindowTokens)
        })
      });
      setModelTests((current) => ({
        ...current,
        [target]: {
          status: result.ok ? "success" : "error",
          message: result.ok
            ? `${targetLabel}连接成功，耗时 ${result.latency_ms} ms。`
            : result.message,
          latencyMs: result.latency_ms,
          model: result.model
        }
      }));
    } catch (err) {
      setModelTests((current) => ({
        ...current,
        [target]: { status: "error", message: err.message, latencyMs: 0, model }
      }));
    }
  }

  async function testEmbeddingApi() {
    const model = form.embedding_model;
    setModelTests((current) => ({
      ...current,
      embedding: {
        status: "testing",
        message: "正在请求测试向量…",
        latencyMs: 0,
        model
      }
    }));
    try {
      if (!form.embedding_endpoint.trim()) throw new Error("请填写 Embedding API Endpoint。");
      if (!model.trim()) throw new Error("请填写 Embedding 模型名称。");
      if (!form.embedding_api_key.trim() && !embeddingApiKeyConfigured) {
        throw new Error("请填写 Embedding API Key，或先保存已有密钥。");
      }
      const result = await apiFetch("/api/users/me/embedding-api/test", {
        method: "POST",
        body: JSON.stringify({
          endpoint: form.embedding_endpoint,
          model,
          api_key: form.embedding_api_key
        })
      });
      setModelTests((current) => ({
        ...current,
        embedding: {
          status: result.ok ? "success" : "error",
          message: result.ok
            ? `向量模型连接成功，${result.dimensions} 维，耗时 ${result.latency_ms} ms。`
            : result.message,
          latencyMs: result.latency_ms,
          model: result.model
        }
      }));
    } catch (err) {
      setModelTests((current) => ({
        ...current,
        embedding: { status: "error", message: err.message, latencyMs: 0, model }
      }));
    }
  }

  async function savePreferences() {
    setMessage("");
    setError("");
    try {
      if (form.review_llm_enabled && !reviewApiKeyConfigured && !form.review_llm_api_key.trim()) {
        throw new Error("开启独立审校模型后，请填写审校模型 API Key。");
      }
      if (Number(form.llm_context_window_tokens) < 4096) {
        throw new Error("正文模型最大上下文不能小于 4096 Token。");
      }
      if (form.review_llm_enabled && Number(form.review_llm_context_window_tokens) < 4096) {
        throw new Error("审校模型最大上下文不能小于 4096 Token。");
      }
      if (form.tavily_enabled && !tavilyApiKeyConfigured && !form.tavily_api_key.trim()) {
        throw new Error("开启 Tavily 网络检索后，请填写 Tavily API Key。");
      }
      if (form.embedding_enabled) {
        if (!form.embedding_endpoint.trim()) {
          throw new Error("开启 Qwen RAG 后，请填写 Embedding API Endpoint。");
        }
        if (!form.embedding_model.trim()) {
          throw new Error("开启 Qwen RAG 后，请填写 Embedding 模型名称。");
        }
        if (!embeddingApiKeyConfigured && !form.embedding_api_key.trim()) {
          throw new Error("开启 Qwen RAG 后，请填写 Embedding API Key。");
        }
      }
      const user = await apiFetch("/api/users/me/preferences", {
        method: "PATCH",
        body: JSON.stringify({
          preferences: {
            llm: {
              provider: "openai_compatible",
              base_url: form.llm_base_url,
              model: form.llm_model,
              context_window_tokens: Number(form.llm_context_window_tokens),
              api_key: editingKeys.writer ? form.llm_api_key : ""
            },
            review_llm: {
              enabled: form.review_llm_enabled,
              provider: "openai_compatible",
              base_url: form.review_llm_base_url,
              model: form.review_llm_model,
              context_window_tokens: Number(form.review_llm_context_window_tokens),
              api_key: editingKeys.reviewer ? form.review_llm_api_key : ""
            },
            embedding: {
              enabled: form.embedding_enabled,
              provider: "dashscope",
              endpoint: form.embedding_endpoint,
              model: form.embedding_model,
              api_key: editingKeys.embedding ? form.embedding_api_key : ""
            },
            web_search: {
              enabled: form.tavily_enabled,
              provider: "tavily",
              tavily_api_key: editingKeys.tavily ? form.tavily_api_key : ""
            }
          }
        })
      });
      setStoredUser(user);
      const nextForm = {
        ...form,
        llm_api_key: "",
        review_llm_api_key: "",
        embedding_api_key: "",
        tavily_api_key: ""
      };
      setForm(nextForm);
      setSavedForm(nextForm);
      setApiKeyConfigured(Boolean(user.preferences?.llm?.api_key_configured || (editingKeys.writer && form.llm_api_key)));
      setApiKeyMasked(user.preferences?.llm?.api_key_masked || "");
      setReviewApiKeyConfigured(Boolean(user.preferences?.review_llm?.api_key_configured || (editingKeys.reviewer && form.review_llm_api_key)));
      setReviewApiKeyMasked(user.preferences?.review_llm?.api_key_masked || "");
      setEmbeddingApiKeyConfigured(Boolean(user.preferences?.embedding?.api_key_configured || (editingKeys.embedding && form.embedding_api_key)));
      setEmbeddingApiKeyMasked(user.preferences?.embedding?.api_key_masked || "");
      setTavilyApiKeyConfigured(Boolean(user.preferences?.web_search?.tavily_api_key_configured || (editingKeys.tavily && form.tavily_api_key)));
      setTavilyApiKeyMasked(user.preferences?.web_search?.tavily_api_key_masked || "");
      setEditingKeys({ writer: false, reviewer: false, embedding: false, tavily: false });
      setEditing(false);
      setMessage("正文模型、审校模型、Qwen RAG 与 Tavily 设置已保存。");
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <AppShell
      title="设置"
      actions={editing ? (
        <div className="inline-actions">
          <button className="secondary-button" onClick={cancelEdit}>取消</button>
          <button className="primary-button" onClick={savePreferences}>保存</button>
        </div>
      ) : (
        <button className="primary-button" onClick={() => setEditing(true)}>编辑</button>
      )}
    >
      {(message || error) ? <div className={error ? "error-box" : "success-box"}>{error || message}</div> : null}

      <section className="panel llm-only-panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">模型 API 配置</div>
          </div>
          <span className={`tag ${apiKeyConfigured ? "green" : "yellow"}`}>{apiKeyConfigured ? "正文模型已配置" : "正文模型未配置"}</span>
        </div>
        <div className="panel-body settings-stack">
          <div className="settings-toggle-row">
            <div className="settings-toggle-copy">
              <strong>使用独立深度思考模型 API</strong>
              <span>{form.review_llm_enabled ? "事件规划、质量审校和样本经验总结共用这套配置；正文生成使用下方 Flash 配置。" : "未启用时，以上分析任务会兼容复用正文模型。"}</span>
            </div>
            <label className="settings-switch">
              <input
                type="checkbox"
                role="switch"
                disabled={!editing}
                checked={form.review_llm_enabled}
                onChange={(event) => updateForm("review_llm_enabled", event.target.checked)}
              />
              <span className="settings-switch-track" aria-hidden="true" />
            </label>
          </div>

          <div className="settings-subcard">
            <div className="settings-subhead">
              <div>
                <strong>正文生成模型 API（建议 Flash）</strong>
                <span>用于章节正文和审校后的局部补丁；优先选择速度快、成本低的模型。</span>
              </div>
              <div className="settings-subhead-actions">
                <span className={`tag ${apiKeyConfigured ? "green" : "yellow"}`}>{apiKeyConfigured ? "已配置" : "未配置"}</span>
                <button
                  className="secondary-button compact-button"
                  disabled={modelTests.writer.status === "testing"}
                  onClick={() => testModelApi("writer")}
                  type="button"
                >
                  {modelTests.writer.status === "testing" ? "测试中…" : "测试连接"}
                </button>
              </div>
            </div>
            <div className="form-grid">
              <div className="field">
                <label>Base URL</label>
                <input name="writer_base_url" autoComplete="off" disabled={!editing} value={form.llm_base_url} onChange={(event) => updateForm("llm_base_url", event.target.value)} placeholder="https://api.openai.com/v1" />
              </div>
              <div className="field">
                <label>模型</label>
                <input name="writer_model" autoComplete="off" disabled={!editing} value={form.llm_model} onChange={(event) => updateForm("llm_model", event.target.value)} placeholder="gpt-4.1-mini" />
              </div>
              <div className="field">
                <label>最大上下文 Token</label>
                <input
                  name="writer_context_window_tokens"
                  type="number"
                  min="4096"
                  step="1024"
                  disabled={!editing}
                  value={form.llm_context_window_tokens}
                  onChange={(event) => updateForm("llm_context_window_tokens", event.target.value)}
                />
              </div>
              <div className="field">
                <label>API Key</label>
                <input
                  disabled={!editing}
                  readOnly={editing && !editingKeys.writer}
                  name="novelforge_writer_api_key"
                  autoComplete="new-password"
                  data-lpignore="true"
                  data-1p-ignore="true"
                  type={editing && editingKeys.writer ? "password" : "text"}
                  value={editing && editingKeys.writer ? form.llm_api_key : apiKeyMasked}
                  onFocus={() => editing && setEditingKeys((current) => ({ ...current, writer: true }))}
                  onChange={(event) => updateForm("llm_api_key", event.target.value)}
                  placeholder={apiKeyConfigured ? "点击后可更换正文模型 API Key" : "点击后填入正文模型 API Key"}
                />
              </div>
            </div>
            {modelTests.writer.status !== "idle" ? (
              <div className={`model-test-result ${modelTests.writer.status}`}>
                <span className="model-test-dot" aria-hidden="true" />
                <span>{modelTests.writer.message}</span>
              </div>
            ) : null}
          </div>

          {form.review_llm_enabled ? (
            <div className="settings-subcard review-model-card">
              <div className="settings-subhead">
                <div>
                  <strong>事件规划、审校与样本分析模型 API（建议深度思考）</strong>
                  <span>同一模型负责事件规划、逐章审校、事件总审和最多十路并行的样本经验总结。</span>
                </div>
                <div className="settings-subhead-actions">
                  <span className={`tag ${reviewApiKeyConfigured ? "green" : "yellow"}`}>{reviewApiKeyConfigured ? "已配置" : "未配置"}</span>
                  <button
                    className="secondary-button compact-button"
                    disabled={modelTests.reviewer.status === "testing"}
                    onClick={() => testModelApi("reviewer")}
                    type="button"
                  >
                    {modelTests.reviewer.status === "testing" ? "测试中…" : "测试连接"}
                  </button>
                </div>
              </div>
              <div className="form-grid">
                <div className="field">
                  <label>Base URL</label>
                  <input name="reviewer_base_url" autoComplete="off" disabled={!editing} value={form.review_llm_base_url} onChange={(event) => updateForm("review_llm_base_url", event.target.value)} placeholder="https://api.openai.com/v1" />
                </div>
                <div className="field">
                  <label>模型</label>
                  <input name="reviewer_model" autoComplete="off" disabled={!editing} value={form.review_llm_model} onChange={(event) => updateForm("review_llm_model", event.target.value)} placeholder="填写深度思考模型名称" />
                </div>
                <div className="field">
                  <label>最大上下文 Token</label>
                  <input
                    name="reviewer_context_window_tokens"
                    type="number"
                    min="4096"
                    step="1024"
                    disabled={!editing}
                    value={form.review_llm_context_window_tokens}
                    onChange={(event) => updateForm("review_llm_context_window_tokens", event.target.value)}
                  />
                </div>
                <div className="field">
                  <label>API Key</label>
                  <input
                    disabled={!editing}
                    readOnly={editing && !editingKeys.reviewer}
                    name="novelforge_reviewer_api_key"
                    autoComplete="new-password"
                    data-lpignore="true"
                    data-1p-ignore="true"
                    type={editing && editingKeys.reviewer ? "password" : "text"}
                    value={editing && editingKeys.reviewer ? form.review_llm_api_key : reviewApiKeyMasked}
                    onFocus={() => editing && setEditingKeys((current) => ({ ...current, reviewer: true }))}
                    onChange={(event) => updateForm("review_llm_api_key", event.target.value)}
                    placeholder={reviewApiKeyConfigured ? "点击后可更换审校模型 API Key" : "点击后填入审校模型 API Key"}
                  />
                </div>
              </div>
              {modelTests.reviewer.status !== "idle" ? (
                <div className={`model-test-result ${modelTests.reviewer.status}`}>
                  <span className="model-test-dot" aria-hidden="true" />
                  <span>{modelTests.reviewer.message}</span>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </section>

      <section className="panel llm-only-panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">Qwen RAG（样本 + 热梗）</div>
          </div>
          <span className={`tag ${form.embedding_enabled && embeddingApiKeyConfigured ? "green" : "yellow"}`}>
            {!form.embedding_enabled ? "未启用" : (embeddingApiKeyConfigured ? "已启用" : "待配置")}
          </span>
        </div>
        <div className="panel-body settings-stack">
          <div className="settings-toggle-row">
            <div className="settings-toggle-copy">
              <strong>启用样本与热梗向量检索</strong>
              <span>
                {form.embedding_enabled
                  ? "样本剧情/表达经验、系统内置热梗和用户扩展热梗都会使用远程向量索引。"
                  : "关闭后不会调用 Embedding API，也不会消耗向量额度。"}
              </span>
            </div>
            <label className="settings-switch">
              <input
                type="checkbox"
                role="switch"
                disabled={!editing}
                checked={form.embedding_enabled}
                onChange={(event) => updateForm("embedding_enabled", event.target.checked)}
              />
              <span className="settings-switch-track" aria-hidden="true" />
              <span className="settings-switch-label">{form.embedding_enabled ? "已开启" : "已关闭"}</span>
            </label>
          </div>

          {form.embedding_enabled ? (
            <div className="settings-subcard">
              <div className="settings-subhead">
                <div>
                  <strong>阿里云百炼 Embedding API</strong>
                  <span>支持百炼原生 /api/v1、OpenAI 兼容 /compatible-mode/v1，或完整文本向量 Endpoint。</span>
                </div>
                <div className="settings-subhead-actions">
                  <span className={`tag ${embeddingApiKeyConfigured ? "green" : "yellow"}`}>
                    {embeddingApiKeyConfigured ? "已配置" : "未配置"}
                  </span>
                  <button
                    className="secondary-button compact-button"
                    disabled={modelTests.embedding.status === "testing"}
                    onClick={testEmbeddingApi}
                    type="button"
                  >
                    {modelTests.embedding.status === "testing" ? "测试中…" : "测试连接"}
                  </button>
                </div>
              </div>
              <div className="form-grid">
                <div className="field">
                  <label>API Endpoint</label>
                  <input
                    name="embedding_endpoint"
                    autoComplete="off"
                    disabled={!editing}
                    value={form.embedding_endpoint}
                    onChange={(event) => updateForm("embedding_endpoint", event.target.value)}
                    placeholder="https://你的WorkspaceId.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
                  />
                </div>
                <div className="field">
                  <label>模型</label>
                  <input
                    name="embedding_model"
                    autoComplete="off"
                    disabled={!editing}
                    value={form.embedding_model}
                    onChange={(event) => updateForm("embedding_model", event.target.value)}
                    placeholder="qwen3.7-text-embedding"
                  />
                </div>
                <div className="field">
                  <label>API Key</label>
                  <input
                    disabled={!editing}
                    readOnly={editing && !editingKeys.embedding}
                    name="novelforge_embedding_api_key"
                    autoComplete="new-password"
                    data-lpignore="true"
                    data-1p-ignore="true"
                    type={editing && editingKeys.embedding ? "password" : "text"}
                    value={editing && editingKeys.embedding ? form.embedding_api_key : embeddingApiKeyMasked}
                    onFocus={() => editing && setEditingKeys((current) => ({ ...current, embedding: true }))}
                    onChange={(event) => updateForm("embedding_api_key", event.target.value)}
                    placeholder={embeddingApiKeyConfigured ? "点击后可更换百炼 API Key" : "点击后填入百炼 API Key"}
                  />
                </div>
              </div>
              {modelTests.embedding.status !== "idle" ? (
                <div className={`model-test-result ${modelTests.embedding.status}`}>
                  <span className="model-test-dot" aria-hidden="true" />
                  <span>{modelTests.embedding.message}</span>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </section>

      <section className="panel llm-only-panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">Tavily 网络检索</div>
          </div>
          <div className="inline-actions">
            <a className="settings-external-link" href="https://www.tavily.com/" target="_blank" rel="noreferrer">官网 ↗</a>
            <span className={`tag ${form.tavily_enabled && tavilyApiKeyConfigured ? "green" : "yellow"}`}>
              {!form.tavily_enabled ? "未启用" : (tavilyApiKeyConfigured ? "已启用" : "待配置")}
            </span>
          </div>
        </div>
        <div className="panel-body settings-stack">
          <div className="settings-toggle-row">
            <div className="settings-toggle-copy">
              <strong>启用 Tavily 网络检索</strong>
              <span>{form.tavily_enabled ? "允许事件规划按需检索现实资料。" : "关闭后不会调用 Tavily，也不会消耗检索额度。"}</span>
            </div>
            <label className="settings-switch">
              <input
                type="checkbox"
                role="switch"
                disabled={!editing}
                checked={form.tavily_enabled}
                onChange={(event) => updateForm("tavily_enabled", event.target.checked)}
              />
              <span className="settings-switch-track" aria-hidden="true" />
              <span className="settings-switch-label">{form.tavily_enabled ? "已开启" : "已关闭"}</span>
            </label>
          </div>

          {form.tavily_enabled ? (
            <div className="settings-subcard">
              <div className="form-grid">
                <div className="field">
                  <label>Tavily API Key</label>
                  <input
                    disabled={!editing}
                    readOnly={editing && !editingKeys.tavily}
                    name="novelforge_tavily_api_key"
                    autoComplete="new-password"
                    data-lpignore="true"
                    data-1p-ignore="true"
                    type={editing && editingKeys.tavily ? "password" : "text"}
                    value={editing && editingKeys.tavily ? form.tavily_api_key : tavilyApiKeyMasked}
                    onFocus={() => editing && setEditingKeys((current) => ({ ...current, tavily: true }))}
                    onChange={(event) => updateForm("tavily_api_key", event.target.value)}
                    placeholder={tavilyApiKeyConfigured ? "点击后可更换 Tavily API Key" : "点击后填入 Tavily API Key"}
                  />
                </div>
              </div>
            </div>
          ) : null}
        </div>
      </section>
    </AppShell>
  );
}
