"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import AppShell from "@/components/AppShell";
import EmptyState from "@/components/EmptyState";
import MetricCard from "@/components/MetricCard";
import { apiFetch } from "@/lib/api";

export default function ProjectsPage() {
  const router = useRouter();
  const [projects, setProjects] = useState([]);
  const [form, setForm] = useState({ title: "灰塔长夜", genre: "奇幻悬疑", target_words: 300000, premise: "一座灰塔、一场长夜，以及不断失真的记忆。" });
  const [error, setError] = useState("");

  async function loadProjects() {
    try {
      setProjects(await apiFetch("/api/novels"));
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    loadProjects();
  }, []);

  async function createProject(event) {
    event.preventDefault();
    setError("");
    try {
      const created = await apiFetch("/api/novels", {
        method: "POST",
        body: JSON.stringify({ ...form, target_words: Number(form.target_words) })
      });
      setProjects([created, ...projects]);
    } catch (err) {
      setError(err.message);
    }
  }

  const activeCount = projects.filter((item) => item.status !== "archived").length;

  return (
    <AppShell
      title="作品管理"
      subtitle="管理全部小说项目，快速进入当前作品的创作工作台"
      actions={<button className="primary-button" onClick={() => router.push("/workbench")}>进入工作台</button>}
    >
      <section className="grid-4">
        <MetricCard label="活跃作品" value={`${activeCount} 部`} note="当前账号下的小说项目" />
        <MetricCard label="规划中" value={`${projects.filter((item) => item.status === "draft").length} 部`} note="可继续完善设定" tone="purple" />
        <MetricCard label="总目标字数" value={`${projects.reduce((sum, item) => sum + item.target_words, 0).toLocaleString()} 字`} note="跨作品统计" tone="green" />
        <MetricCard label="核心能力" value="预留" note="Agent 任务入口已接通" tone="yellow" />
      </section>

      <section className="grid-2">
        <form className="panel pad form-grid" onSubmit={createProject}>
          <div className="panel-title">新建作品</div>
          <div className="field"><label>作品名</label><input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></div>
          <div className="field"><label>题材</label><input value={form.genre} onChange={(e) => setForm({ ...form, genre: e.target.value })} /></div>
          <div className="field"><label>目标字数</label><input type="number" value={form.target_words} onChange={(e) => setForm({ ...form, target_words: e.target.value })} /></div>
          <div className="field"><label>初始需求</label><textarea value={form.premise} onChange={(e) => setForm({ ...form, premise: e.target.value })} /></div>
          {error ? <div className="error-box">{error}</div> : null}
          <button className="primary-button">创建作品</button>
        </form>

        <section className="panel">
          <div className="panel-header">
            <div><div className="panel-title">作品列表</div><div className="panel-subtitle">点击作品进入创作工作台</div></div>
          </div>
          <div className="panel-body">
            {projects.length === 0 ? (
              <EmptyState title="还没有作品" description="先创建一部小说，工作台和 Agent 预留任务才有上下文。" />
            ) : (
              <div className="grid-1">
                {projects.map((project) => (
                  <article className="project-card" key={project.id} onClick={() => router.push(`/workbench?novel=${project.id}`)}>
                    <div className="project-card-head">
                      <div>
                        <div className="project-type">{project.genre || "未分类"} · 长篇</div>
                        <h2>{project.title}</h2>
                      </div>
                      <span className="tag green">{project.status}</span>
                    </div>
                    <p>{project.premise || "暂无初始需求描述。"}</p>
                    <div className="project-meta-grid">
                      <div><span>当前章节</span><strong>{project.current_chapter_index}</strong></div>
                      <div><span>目标字数</span><strong>{project.target_words.toLocaleString()}</strong></div>
                      <div><span>入口</span><strong>工作台</strong></div>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </div>
        </section>
      </section>
    </AppShell>
  );
}
