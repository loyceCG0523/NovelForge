"use client";

import { useEffect, useState } from "react";

import AppShell from "@/components/AppShell";
import { apiFetch, setStoredUser } from "@/lib/api";

const defaultForm = {
  genre: "奇幻悬疑、群像、长篇",
  narrative_taste: "balanced",
  avoid_style: "避免模板化比喻、解释性独白、过度总结、AI 常用转折句。",
  chapter_strategy: "draft_and_review",
  memory_strategy: "auto_after_chapter",
  risk_strategy: "auto_low_risk",
  llm_base_url: "https://api.openai.com/v1",
  llm_model: "gpt-4.1-mini",
  llm_api_key: ""
};

export default function PersonalizationPage() {
  // 设置页集中承载账号信息、创作偏好和个人 LLM API Key，避免拆出多个低价值页面。
  const [form, setForm] = useState(defaultForm);
  const [apiKeyConfigured, setApiKeyConfigured] = useState(false);
  const [user, setUser] = useState(null);
  const [projectCount, setProjectCount] = useState(0);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    loadPreferences().catch((err) => setError(err.message));
  }, []);

  async function loadPreferences() {
    const user = await apiFetch("/api/auth/me");
    setUser(user);
    apiFetch("/api/novels").then((items) => setProjectCount(items.length)).catch(() => setProjectCount(0));
    const preferences = user.preferences || {};
    const writing = preferences.writing || {};
    const automation = preferences.automation || {};
    const llm = preferences.llm || {};
    setForm({
      ...defaultForm,
      genre: writing.genre || defaultForm.genre,
      narrative_taste: writing.narrative_taste || defaultForm.narrative_taste,
      avoid_style: writing.avoid_style || defaultForm.avoid_style,
      chapter_strategy: automation.chapter_strategy || defaultForm.chapter_strategy,
      memory_strategy: automation.memory_strategy || defaultForm.memory_strategy,
      risk_strategy: automation.risk_strategy || defaultForm.risk_strategy,
      llm_base_url: llm.base_url || defaultForm.llm_base_url,
      llm_model: llm.model || defaultForm.llm_model,
      llm_api_key: ""
    });
    setApiKeyConfigured(Boolean(llm.api_key_configured));
  }

  function updateForm(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function savePreferences() {
    setMessage("");
    setError("");
    try {
      const user = await apiFetch("/api/users/me/preferences", {
        method: "PATCH",
        body: JSON.stringify({
          preferences: {
            writing: {
              genre: form.genre,
              narrative_taste: form.narrative_taste,
              avoid_style: form.avoid_style
            },
            automation: {
              chapter_strategy: form.chapter_strategy,
              memory_strategy: form.memory_strategy,
              risk_strategy: form.risk_strategy
            },
            llm: {
              provider: "openai_compatible",
              base_url: form.llm_base_url,
              model: form.llm_model,
              api_key: form.llm_api_key
            }
          }
        })
      });
      setStoredUser(user);
      setApiKeyConfigured(Boolean(user.preferences?.llm?.api_key_configured || form.llm_api_key));
      setForm((current) => ({ ...current, llm_api_key: "" }));
      setMessage("设置已保存。下次 Worker 执行章节生成任务时会优先使用个人 LLM 配置。");
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <AppShell
      title="设置"
      subtitle="管理账号信息、创作偏好、自动化策略和个人 LLM API Key"
      actions={<button className="primary-button" onClick={savePreferences}>保存设置</button>}
    >
      {(message || error) ? <div className={error ? "error-box" : "hint-panel"}>{error || message}</div> : null}

      <section className="panel">
        <div className="panel-header">
          <div><div className="panel-title">账号概况</div><div className="panel-subtitle">只保留和创作工作流有关的账号状态。</div></div>
        </div>
        <div className="panel-body grid-4">
          <div className="mini-stat"><span>显示名称</span><strong>{user?.display_name || "-"}</strong></div>
          <div className="mini-stat"><span>登录邮箱</span><strong>{user?.email || "-"}</strong></div>
          <div className="mini-stat"><span>套餐</span><strong>{user?.plan || "free"}</strong></div>
          <div className="mini-stat"><span>作品数量</span><strong>{projectCount} 部</strong></div>
        </div>
      </section>

      <section className="grid-2">
        <div className="panel">
          <div className="panel-header">
            <div><div className="panel-title">创作偏好</div><div className="panel-subtitle">作为后续 NovelBrief 和 Agent 规划的默认约束</div></div>
          </div>
          <div className="panel-body form-grid">
            <div className="field">
              <label>默认题材</label>
              <input value={form.genre} onChange={(event) => updateForm("genre", event.target.value)} />
            </div>
            <div className="field">
              <label>叙事口味</label>
              <select value={form.narrative_taste} onChange={(event) => updateForm("narrative_taste", event.target.value)}>
                <option value="balanced">剧情推进与人物刻画均衡</option>
                <option value="plot_first">强剧情推进</option>
                <option value="character_first">强人物内心</option>
              </select>
            </div>
            <div className="field">
              <label>规避风格</label>
              <textarea value={form.avoid_style} onChange={(event) => updateForm("avoid_style", event.target.value)} />
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel-header">
            <div><div className="panel-title">自动化策略</div><div className="panel-subtitle">目标是用户只填写初始需求，系统自动推进生产</div></div>
          </div>
          <div className="panel-body form-grid">
            <div className="field">
              <label>章节生成策略</label>
              <select value={form.chapter_strategy} onChange={(event) => updateForm("chapter_strategy", event.target.value)}>
                <option value="draft_and_review">自动生成草稿并审校</option>
                <option value="outline_only">只生成大纲</option>
              </select>
            </div>
            <div className="field">
              <label>记忆同步</label>
              <select value={form.memory_strategy} onChange={(event) => updateForm("memory_strategy", event.target.value)}>
                <option value="auto_after_chapter">每章后自动同步</option>
                <option value="manual_confirm">自动同步并保留审计记录</option>
              </select>
            </div>
            <div className="field">
              <label>质量处理</label>
              <select value={form.risk_strategy} onChange={(event) => updateForm("risk_strategy", event.target.value)}>
                <option value="auto_low_risk">自动修复并记录高风险</option>
                <option value="notify_all">全部转入系统审校记录</option>
              </select>
            </div>
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">个人 LLM API Key</div>
            <div className="panel-subtitle">当前按 OpenAI-compatible 接口调用，Worker 生成章节时读取该配置</div>
          </div>
          <span className={`tag ${apiKeyConfigured ? "green" : "yellow"}`}>{apiKeyConfigured ? "已配置" : "未配置"}</span>
        </div>
        <div className="panel-body form-grid">
          <div className="field">
            <label>Base URL</label>
            <input value={form.llm_base_url} onChange={(event) => updateForm("llm_base_url", event.target.value)} placeholder="https://api.openai.com/v1" />
          </div>
          <div className="field">
            <label>模型</label>
            <input value={form.llm_model} onChange={(event) => updateForm("llm_model", event.target.value)} placeholder="gpt-4.1-mini" />
          </div>
          <div className="field">
            <label>API Key</label>
            <input
              type="password"
              value={form.llm_api_key}
              onChange={(event) => updateForm("llm_api_key", event.target.value)}
              placeholder={apiKeyConfigured ? "已保存，留空则不修改" : "填入你的个人 API Key"}
            />
          </div>
        </div>
      </section>

    </AppShell>
  );
}
