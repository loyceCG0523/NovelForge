"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import AppShell from "@/components/AppShell";
import EmptyState from "@/components/EmptyState";
import MetricCard from "@/components/MetricCard";
import { apiFetch } from "@/lib/api";

const defaultBrief = {
  // 新用户只需要填写起始需求文档，后续系统会把这些字段转成作品 brief。
  work_type: "长篇小说",
  selling_points: "高概念悬疑、强反转、人物关系张力",
  protagonist: "背负旧案的调查者，理性但对记忆不完全可信",
  worldview: "灰塔统治下的边境城市，记忆可以被交易和伪造",
  plot_direction: "从一场长夜谋杀案切入，逐步揭开灰塔与主角过去的关系",
  style_reference: "克制、冷峻、细节密集，避免解释性独白",
  forbidden_content: "避免套路升级、过度金手指、模板化情绪描写",
  automation_strategy: "低风险自动推进，高风险只提醒用户确认"
};

export default function ProjectsPage() {
  // 作品管理页负责“一名用户多部小说”的入口，也承担起始需求文档创建流程。
  const router = useRouter();
  const [projects, setProjects] = useState([]);
  const [form, setForm] = useState({
    title: "灰塔长夜",
    genre: "奇幻悬疑",
    target_words: 300000,
    premise: "一座灰塔、一场长夜，以及不断失真的记忆。",
    ...defaultBrief
  });
  const [error, setError] = useState("");

  async function loadProjects() {
    // 列出当前账号下的所有作品项目。
    try {
      setProjects(await apiFetch("/api/novels"));
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    loadProjects();
  }, []);

  function updateForm(key, value) {
    // 表单字段较多，统一用 key-value 方式更新，减少重复事件处理函数。
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function createProject(event) {
    // title/genre/target_words/premise 是 Novel 字段，其余内容打包进 brief JSON。
    event.preventDefault();
    setError("");
    const { title, genre, target_words, premise, ...brief } = form;
    try {
      const created = await apiFetch("/api/novels", {
        method: "POST",
        body: JSON.stringify({
          title,
          genre,
          target_words: Number(target_words),
          premise,
          brief
        })
      });
      setProjects([created, ...projects]);
      router.push(`/workbench?novel=${created.id}`);
    } catch (err) {
      setError(err.message);
    }
  }

  const totalTargetWords = useMemo(
    () => projects.reduce((sum, item) => sum + item.target_words, 0),
    [projects]
  );

  return (
    <AppShell
      title="作品管理"
      subtitle="用起始需求文档创建作品，后续由工作台和 Agent 任务持续推进"
      actions={<button className="primary-button" onClick={() => router.push("/workbench")}>进入工作台</button>}
    >
      <section className="grid-4">
        <MetricCard label="作品数量" value={`${projects.length} 部`} note="当前账号下的小说项目" />
        <MetricCard label="规划中" value={`${projects.filter((item) => item.status === "draft").length} 部`} note="已保存起始需求" tone="purple" />
        <MetricCard label="目标字数" value={`${totalTargetWords.toLocaleString()} 字`} note="跨作品累计目标" tone="green" />
        <MetricCard label="自动化策略" value="已预留" note="后续驱动 Agent 执行" tone="yellow" />
      </section>

      <section className="grid-2">
        <form className="panel pad form-grid" onSubmit={createProject}>
          <div>
            <div className="panel-title">起始需求文档</div>
            <div className="panel-subtitle">用户只填写一次，系统后续基于这些结构化信息自动推进创作。</div>
          </div>
          <div className="grid-2">
            <div className="field"><label>作品名</label><input value={form.title} onChange={(e) => updateForm("title", e.target.value)} /></div>
            <div className="field"><label>题材</label><input value={form.genre} onChange={(e) => updateForm("genre", e.target.value)} /></div>
          </div>
          <div className="grid-2">
            <div className="field"><label>作品类型</label><input value={form.work_type} onChange={(e) => updateForm("work_type", e.target.value)} /></div>
            <div className="field"><label>目标字数</label><input type="number" value={form.target_words} onChange={(e) => updateForm("target_words", e.target.value)} /></div>
          </div>
          <div className="field"><label>一句话需求</label><textarea value={form.premise} onChange={(e) => updateForm("premise", e.target.value)} /></div>
          <div className="field"><label>卖点与读者期待</label><textarea value={form.selling_points} onChange={(e) => updateForm("selling_points", e.target.value)} /></div>
          <div className="field"><label>主角设定</label><textarea value={form.protagonist} onChange={(e) => updateForm("protagonist", e.target.value)} /></div>
          <div className="field"><label>世界观与核心规则</label><textarea value={form.worldview} onChange={(e) => updateForm("worldview", e.target.value)} /></div>
          <div className="field"><label>剧情方向</label><textarea value={form.plot_direction} onChange={(e) => updateForm("plot_direction", e.target.value)} /></div>
          <div className="field"><label>风格参考</label><textarea value={form.style_reference} onChange={(e) => updateForm("style_reference", e.target.value)} /></div>
          <div className="field"><label>禁忌内容</label><textarea value={form.forbidden_content} onChange={(e) => updateForm("forbidden_content", e.target.value)} /></div>
          <div className="field"><label>自动化策略</label><textarea value={form.automation_strategy} onChange={(e) => updateForm("automation_strategy", e.target.value)} /></div>
          {error ? <div className="error-box">{error}</div> : null}
          <button className="primary-button">创建作品并进入工作台</button>
        </form>

        <section className="panel">
          <div className="panel-header">
            <div><div className="panel-title">作品列表</div><div className="panel-subtitle">选择作品后进入工作台或章节管理。</div></div>
          </div>
          <div className="panel-body">
            {projects.length === 0 ? (
              <EmptyState title="还没有作品" description="先填写起始需求文档，创建第一部小说项目。" />
            ) : (
              <div className="stack-list">
                {projects.map((project) => (
                  <article className="project-card" key={project.id}>
                    <div className="project-card-head">
                      <div>
                        <div className="project-type">{project.genre || "未分类"} · {project.brief?.work_type || "小说"}</div>
                        <h2>{project.title}</h2>
                      </div>
                      <span className="tag green">{project.status}</span>
                    </div>
                    <p>{project.premise || "暂无起始需求。"}</p>
                    <div className="project-meta-grid">
                      <div><span>当前章节</span><strong>{project.current_chapter_index}</strong></div>
                      <div><span>目标字数</span><strong>{project.target_words.toLocaleString()}</strong></div>
                      <div><span>自动化</span><strong>{project.brief?.automation_strategy ? "已配置" : "待补充"}</strong></div>
                    </div>
                    <div className="inline-actions">
                      <button className="secondary-button" onClick={() => router.push(`/workbench?novel=${project.id}`)}>工作台</button>
                      <button className="secondary-button" onClick={() => router.push(`/chapters?novel=${project.id}`)}>章节管理</button>
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
