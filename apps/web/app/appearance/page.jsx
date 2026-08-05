"use client";

import { useEffect, useState } from "react";

import AppShell from "@/components/AppShell";
import { apiFetch, getStoredUser, setStoredUser } from "@/lib/api";
import { applyTheme, DEFAULT_THEME, getPreferredTheme, THEMES } from "@/lib/themes";

export default function AppearancePage() {
  const [selectedTheme, setSelectedTheme] = useState(DEFAULT_THEME);
  const [savingTheme, setSavingTheme] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    const localTheme = getPreferredTheme(getStoredUser());
    setSelectedTheme(localTheme);
    applyTheme(localTheme, { notify: false });
    apiFetch("/api/auth/me")
      .then((user) => {
        if (!active) return;
        setStoredUser(user);
        const theme = getPreferredTheme(user);
        setSelectedTheme(theme);
        applyTheme(theme, { notify: false });
      })
      .catch((err) => {
        if (active) setError(`账号外观偏好读取失败：${err.message}`);
      });
    return () => {
      active = false;
    };
  }, []);

  async function chooseTheme(theme) {
    if (savingTheme) return;
    const previousTheme = selectedTheme;
    setSelectedTheme(theme);
    setSavingTheme(theme);
    setMessage("");
    setError("");
    applyTheme(theme);

    try {
      const user = await apiFetch("/api/users/me/preferences", {
        method: "PATCH",
        body: JSON.stringify({
          preferences: {
            appearance: { theme }
          }
        })
      });
      setStoredUser(user);
      setMessage(`已切换为 ${THEMES.find((item) => item.id === theme)?.name} 主题，并同步到账号。`);
    } catch (err) {
      setSelectedTheme(previousTheme);
      applyTheme(previousTheme);
      setError(`主题保存失败，已恢复原外观：${err.message}`);
    } finally {
      setSavingTheme("");
    }
  }

  const currentTheme = THEMES.find((theme) => theme.id === selectedTheme) || THEMES[0];

  return (
    <AppShell
      title="外观设置"
      actions={<span className="appearance-current-pill"><i />正在使用 {currentTheme.name}</span>}
    >
      <section className="appearance-hero">
        <div className="appearance-hero-copy">
          <span className="appearance-eyebrow">YOUR WRITING ATMOSPHERE</span>
          <h2>{currentTheme.name} <small>（{currentTheme.chineseName}）</small></h2>
          <p>{currentTheme.tagline}</p>
          <div className="appearance-palette" aria-label={`${currentTheme.name} 色板`}>
            {currentTheme.palette.map((color) => <span key={color} style={{ background: color }} />)}
          </div>
        </div>
        <div className="appearance-hero-quote" aria-hidden="true">
          <span>第 07 章</span>
          <strong>窗外的光落在未写完的句子上。</strong>
          <p>光标安静地闪烁，像在等待故事自己找到下一句话。</p>
        </div>
      </section>

      {message || error ? (
        <div className={error ? "error-box appearance-feedback" : "success-box appearance-feedback"} role="status">
          {error || message}
        </div>
      ) : null}

      <div className="appearance-section-heading">
        <div>
          <span>BUILT-IN THEMES</span>
          <h2>选择你的创作空间</h2>
        </div>
        <p>选择后立即应用到整个系统，并自动保存。正文内容和业务数据不会受到影响。</p>
      </div>

      <section className="theme-grid" aria-label="内置外观主题">
        {THEMES.map((theme) => {
          const selected = selectedTheme === theme.id;
          const saving = savingTheme === theme.id;
          return (
            <article className={`theme-card ${selected ? "selected" : ""}`} key={theme.id}>
              <div className="theme-preview" data-preview-theme={theme.id} aria-hidden="true">
                <div className="theme-preview-sidebar">
                  <b>N</b>
                  <i className="active" />
                  <i />
                  <i />
                  <i />
                  <span />
                </div>
                <div className="theme-preview-workspace">
                  <div className="theme-preview-topbar"><i /><i /></div>
                  <div className="theme-preview-body">
                    <div className="theme-preview-paper">
                      <em>CHAPTER 07</em>
                      <strong>雨停在故事开始之前</strong>
                      <i />
                      <i />
                      <i className="short" />
                      <span>写作中</span>
                    </div>
                    <div className="theme-preview-notes">
                      <i />
                      <i />
                      <i />
                    </div>
                  </div>
                </div>
              </div>

              <div className="theme-card-content">
                <div className="theme-card-title">
                  <div>
                    <span>{theme.name}</span>
                    <h3>{theme.chineseName}</h3>
                  </div>
                  {selected ? <b className="theme-selected-mark" aria-label="当前使用">✓</b> : null}
                </div>
                <p>{theme.description}</p>
                <div className="theme-card-footer">
                  <span>{theme.suitableFor}</span>
                  <button
                    type="button"
                    className={selected ? "secondary-button" : "primary-button"}
                    disabled={Boolean(savingTheme)}
                    onClick={() => chooseTheme(theme.id)}
                  >
                    {saving ? "正在保存…" : selected ? "当前使用" : "应用主题"}
                  </button>
                </div>
              </div>
            </article>
          );
        })}
      </section>
    </AppShell>
  );
}
