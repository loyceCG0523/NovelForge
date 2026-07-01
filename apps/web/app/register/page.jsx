"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { register, setSession } from "@/lib/api";

export default function RegisterPage() {
  // 注册成功后直接进入作品管理，符合“先填起始需求文档”的产品流程。
  const router = useRouter();
  const [form, setForm] = useState({ email: "", display_name: "", password: "" });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(event) {
    // 注册接口会同时返回 token，因此无需再额外登录一次。
    event.preventDefault();
    setError("");
    setLoading(true);
    try {
      const data = await register(form);
      setSession(data.access_token, data.user);
      router.replace("/projects");
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
          <div><strong>NovelForge</strong><span>新作品从这里开始</span></div>
        </div>
        <div className="visual-copy">
          <h1>填写最少信息，先拥有一个可持续生产小说的工作空间。</h1>
          <p>注册后可以创建作品，后续由系统接管章节生产、记忆同步、伏笔追踪和反 AI 审校。</p>
          <div className="visual-board">
            <div className="visual-card"><strong>作品管理</strong><span>多小说项目</span></div>
            <div className="visual-card"><strong>创作工作台</strong><span>状态与风险</span></div>
            <div className="visual-card"><strong>Agent 预留</strong><span>任务队列已接通</span></div>
          </div>
        </div>
        <div className="brand-row"><span>核心生成智能体会接入任务队列，不影响当前产品框架推进。</span></div>
      </aside>

      <main className="entry-main">
        <section className="auth-card">
          <div className="auth-head">
            <div><h2>创建账号</h2><p>创建你的 NovelForge 工作空间。</p></div>
            <Link className="auth-switch" href="/login">已有账号</Link>
          </div>
          <form className="form-grid" onSubmit={handleSubmit}>
            <div className="field">
              <label>邮箱</label>
              <input value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} placeholder="you@example.com" />
            </div>
            <div className="field">
              <label>显示名称</label>
              <input value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} placeholder="林深不知处" />
            </div>
            <div className="field">
              <label>密码</label>
              <input type="password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} placeholder="至少 8 位" />
            </div>
            {error ? <div className="error-box">{error}</div> : null}
            <button className="primary-button" disabled={loading}>{loading ? "创建中..." : "创建账号"}</button>
          </form>
        </section>
      </main>
    </div>
  );
}
