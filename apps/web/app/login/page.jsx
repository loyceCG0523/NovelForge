"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { login, setSession } from "@/lib/api";

export default function LoginPage() {
  // 登录页成功后写入本地会话，并进入工作台。
  const router = useRouter();
  const [form, setForm] = useState({ email: "linchuan@example.com", password: "test123456" });
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const message = sessionStorage.getItem("novelforge_login_notice");
    if (!message) return;
    setNotice(message);
    sessionStorage.removeItem("novelforge_login_notice");
  }, []);

  async function handleSubmit(event) {
    // 表单提交期间锁定按钮，避免重复登录请求。
    event.preventDefault();
    setError("");
    setNotice("");
    setLoading(true);
    try {
      const data = await login(form);
      setSession(data.access_token, data.user);
      router.replace("/workbench");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="entry-shell">
      <aside className="entry-visual">
        <div className="brand-row">
          <div className="brand-mark">N</div>
          <div><strong>NovelForge</strong><span>欢迎回到创作现场</span></div>
        </div>
        <div className="visual-copy">
          <h1>继续管理你的小说项目、章节生产和结构化记忆。</h1>
          <p>登录后进入创作工作台，查看当前作品状态、系统审校记录和自动生产进度。</p>
          <div className="visual-board">
            <div className="visual-card"><strong>灰塔长夜</strong><span>第 24 章生成中</span></div>
            <div className="visual-card"><strong>系统审校</strong><span>自动修复记录</span></div>
            <div className="visual-card"><strong>本月生成</strong><span>182,400 字</span></div>
          </div>
        </div>
        <div className="brand-row"><span>系统会保留你的作品、偏好、记忆库和审校策略。</span></div>
      </aside>

      <main className="entry-main">
        <section className="auth-card">
          <div className="auth-head">
            <div><h2>登录账号</h2><p>进入作品管理和创作工作台。</p></div>
            <Link className="auth-switch" href="/register">创建账号</Link>
          </div>
          <form className="form-grid" onSubmit={handleSubmit}>
            <div className="field">
              <label>邮箱</label>
              <input value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} />
            </div>
            <div className="field">
              <label>密码</label>
              <input type="password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} />
            </div>
            {notice ? <div className="hint-panel">{notice}</div> : null}
            {error ? <div className="error-box">{error}</div> : null}
            <button className="primary-button" disabled={loading}>{loading ? "登录中..." : "登录并进入工作台"}</button>
          </form>
          <div className="hint-panel">如果还没有账号，先创建一个。当前后端已接入真实注册登录和 JWT 鉴权。</div>
        </section>
      </main>
    </div>
  );
}
