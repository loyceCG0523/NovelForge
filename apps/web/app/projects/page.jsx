"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import AppShell from "@/components/AppShell";
import EmptyState from "@/components/EmptyState";
import { apiDownload, apiFetch } from "@/lib/api";

const defaultBrief = {
  work_type: "长篇小说",
  selling_points: "高概念悬疑、强反转、人物关系张力",
  protagonist: "背负旧案的调查者，理性但对记忆不完全可信",
  worldview: "灰塔统治下的边境城市，记忆可以被交易和伪造",
  plot_direction: "从一场长夜谋杀案切入，逐步揭开灰塔与主角过去的关系",
  chapter_word_min: 2000,
  chapter_word_max: 3000,
  style_reference: "克制、冷峻、细节密集，避免解释性独白",
  forbidden_content: "避免套路升级、过度金手指、模板化情绪描写",
  automation_strategy: "低风险自动推进，高风险只提醒用户确认",
  sample_reference_ids: []
};

const emptyProjectForm = {
  title: "灰塔长夜",
  genre: "奇幻悬疑",
  target_words: 300000,
  premise: "一座灰塔、一场长夜，以及不断失真的记忆。",
  ...defaultBrief
};

function projectToForm(project) {
  const brief = project?.brief || {};
  return {
    title: project?.title || "",
    genre: project?.genre || "",
    target_words: project?.target_words || 300000,
    premise: project?.premise || "",
    work_type: brief.work_type || defaultBrief.work_type,
    selling_points: brief.selling_points || "",
    protagonist: brief.protagonist || "",
    worldview: brief.worldview || "",
    plot_direction: brief.plot_direction || "",
    chapter_word_min: brief.chapter_word_min || defaultBrief.chapter_word_min,
    chapter_word_max: brief.chapter_word_max || defaultBrief.chapter_word_max,
    style_reference: brief.style_reference || "",
    forbidden_content: brief.forbidden_content || "",
    automation_strategy: brief.automation_strategy || "",
    sample_reference_ids: Array.isArray(brief.sample_reference_ids) ? brief.sample_reference_ids : []
  };
}

function formToNovelPayload(form) {
  const { title, genre, target_words, premise, ...brief } = form;
  return {
    title,
    genre,
    target_words: Number(target_words),
    premise,
    brief: {
      ...brief,
      chapter_word_min: Number(brief.chapter_word_min),
      chapter_word_max: Number(brief.chapter_word_max)
    }
  };
}

function createDeleteCode() {
  return String(Math.floor(10000000 + Math.random() * 90000000));
}

export default function ProjectsPage() {
  const router = useRouter();
  const [projects, setProjects] = useState([]);
  const [sampleLibrary, setSampleLibrary] = useState([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [briefForm, setBriefForm] = useState(projectToForm(null));
  const [createForm, setCreateForm] = useState(emptyProjectForm);
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [storyBible, setStoryBible] = useState(null);
  const [storyBibleTask, setStoryBibleTask] = useState(null);
  const [pendingDeleteProject, setPendingDeleteProject] = useState(null);
  const [deleteCode, setDeleteCode] = useState("");
  const [deleteCodeInput, setDeleteCodeInput] = useState("");
  const [deleteCodeError, setDeleteCodeError] = useState("");
  const [deletingProjectId, setDeletingProjectId] = useState("");
  const [exportingProjectId, setExportingProjectId] = useState("");
  const [projectError, setProjectError] = useState("");
  const [createError, setCreateError] = useState("");
  const [storyBibleError, setStoryBibleError] = useState("");

  const selectedProject = useMemo(
    () => projects.find((project) => project.id === selectedProjectId),
    [projects, selectedProjectId]
  );
  const deleteCodeMismatch = deleteCodeInput.length === 8 && deleteCodeInput !== deleteCode;

  useEffect(() => {
    loadProjects();
    loadSampleLibrary();
  }, []);

  useEffect(() => {
    setBriefForm(projectToForm(selectedProject));
    setProjectError("");
    setStoryBibleError("");
    setStoryBibleTask(null);
    if (selectedProject?.id) {
      loadStoryBible(selectedProject.id).catch((err) => setStoryBibleError(err.message));
    } else {
      setStoryBible(null);
    }
  }, [selectedProject?.id]);

  useEffect(() => {
    if (!storyBibleTask || !selectedProjectId) return undefined;
    if (!["queued", "running"].includes(storyBibleTask.status)) return undefined;

    const timer = setInterval(async () => {
      try {
        const task = await apiFetch(`/api/novels/${selectedProjectId}/tasks/${storyBibleTask.id}`);
        setStoryBibleTask(task);
        if (task.status === "completed") {
          await loadStoryBible(selectedProjectId);
        }
      } catch (err) {
        setStoryBibleError(err.message);
      }
    }, 1800);
    return () => clearInterval(timer);
  }, [storyBibleTask, selectedProjectId]);

  async function loadProjects() {
    try {
      const data = await apiFetch("/api/novels");
      setProjects(data);
      setSelectedProjectId((current) => current || data[0]?.id || "");
    } catch (err) {
      setProjectError(err.message);
    }
  }

  async function loadSampleLibrary() {
    try {
      const data = await apiFetch("/api/sample-analyses/library");
      setSampleLibrary(data);
    } catch (err) {
      setProjectError(err.message);
    }
  }

  function updateBriefForm(key, value) {
    setBriefForm((current) => ({ ...current, [key]: value }));
  }

  function updateCreateForm(key, value) {
    setCreateForm((current) => ({ ...current, [key]: value }));
  }

  async function createProject(event) {
    event.preventDefault();
    setCreateError("");
    try {
      const created = await apiFetch("/api/novels", {
        method: "POST",
        body: JSON.stringify(formToNovelPayload(createForm))
      });
      setProjects((current) => [created, ...current]);
      setSelectedProjectId(created.id);
      setCreateForm(emptyProjectForm);
      setShowCreateDialog(false);
    } catch (err) {
      setCreateError(err.message);
    }
  }

  async function saveBrief(regenerateBible = false) {
    if (!selectedProject) return;
    setProjectError("");
    try {
      const saved = await apiFetch(`/api/novels/${selectedProject.id}`, {
        method: "PATCH",
        body: JSON.stringify(formToNovelPayload(briefForm))
      });
      setProjects((current) => current.map((project) => (project.id === saved.id ? saved : project)));
      if (regenerateBible) {
        await generateStoryBible(saved.id, "brief_saved");
      }
    } catch (err) {
      setProjectError(err.message);
    }
  }

  async function loadStoryBible(projectId) {
    setStoryBibleError("");
    const data = await apiFetch(`/api/novels/${projectId}/story-bible`);
    setStoryBible(data);
  }

  async function generateStoryBible(projectId = selectedProjectId, source = "projects_page") {
    if (!projectId) return;
    setStoryBibleError("");
    try {
      const task = await apiFetch(`/api/novels/${projectId}/story-bible/generate`, {
        method: "POST",
        body: JSON.stringify({ input_payload: { source } })
      });
      setStoryBibleTask(task);
    } catch (err) {
      setStoryBibleError(err.message);
    }
  }

  function openDeleteProject(project) {
    setPendingDeleteProject(project);
    setDeleteCode(createDeleteCode());
    setDeleteCodeInput("");
    setDeleteCodeError("");
    setProjectError("");
  }

  async function confirmDeleteProject() {
    if (!pendingDeleteProject) return;
    if (deleteCodeInput !== deleteCode) {
      setDeleteCodeError(
        deleteCodeInput.length === 8
          ? "确认码不一致，请重新核对上方 8 位数字。"
          : "请完整输入上方 8 位确认码。"
      );
      return;
    }
    setDeletingProjectId(pendingDeleteProject.id);
    setProjectError("");
    try {
      await apiFetch(`/api/novels/${pendingDeleteProject.id}`, {
        method: "DELETE",
        body: JSON.stringify({
          confirmation_code: deleteCodeInput,
          expected_code: deleteCode
        })
      });
      const remainingProjects = projects.filter((project) => project.id !== pendingDeleteProject.id);
      setProjects(remainingProjects);
      setSelectedProjectId((current) => (
        current === pendingDeleteProject.id ? remainingProjects[0]?.id || "" : current
      ));
      setPendingDeleteProject(null);
      setDeleteCode("");
      setDeleteCodeInput("");
      setDeleteCodeError("");
    } catch (err) {
      setProjectError(err.message);
    } finally {
      setDeletingProjectId("");
    }
  }

  async function exportProject(project, format) {
    if (!project?.id) return;
    setProjectError("");
    setExportingProjectId(`${project.id}:${format}`);
    try {
      await apiDownload(
        `/api/novels/${project.id}/export?format=${format}`,
        `${project.title || "novel"}.${format === "txt" ? "txt" : "md"}`
      );
    } catch (err) {
      setProjectError(err.message);
    } finally {
      setExportingProjectId("");
    }
  }

  return (
    <AppShell
      title="作品管理"
      subtitle="管理多部作品、起始需求文档和作品圣经"
      actions={(
        <>
          <button className="secondary-button" onClick={() => router.push(selectedProjectId ? `/workbench?novel=${selectedProjectId}` : "/workbench")}>进入工作台</button>
          <button className="primary-button" onClick={() => setShowCreateDialog(true)}>新建作品</button>
        </>
      )}
    >
      <section className="projects-workspace">
        <section className="panel project-list-panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">作品列表</div>
              <div className="panel-subtitle">选择作品后编辑起始需求和作品圣经。</div>
            </div>
          </div>
          <div className="panel-body">
            {projects.length === 0 ? (
              <EmptyState title="还没有作品" description="点击右上角新建作品，填写第一份起始需求文档。" />
            ) : (
              <div className="stack-list">
                {projects.map((project) => (
                  <article
                    className={`project-card compact ${project.id === selectedProjectId ? "active" : ""}`}
                    key={project.id}
                    onClick={() => setSelectedProjectId(project.id)}
                  >
                    <div className="project-card-head">
                      <div>
                        <div className="project-type">{project.genre || "未分类"} · {project.brief?.work_type || "小说"}</div>
                        <h2>{project.title}</h2>
                      </div>
                      <span className="tag green">{project.status}</span>
                    </div>
                    <p>{project.premise || "暂无起始需求。"}</p>
                    <div className="project-meta-grid">
                      <div><span>章节</span><strong>{project.current_chapter_index}</strong></div>
                      <div><span>目标</span><strong>{project.target_words.toLocaleString()}</strong></div>
                      <div><span>单章</span><strong>{project.brief?.chapter_word_min || 2000}-{project.brief?.chapter_word_max || 3000}</strong></div>
                    </div>
                    <div className="inline-actions" onClick={(event) => event.stopPropagation()}>
                      <button className="secondary-button" onClick={() => router.push(`/workbench?novel=${project.id}`)}>工作台</button>
                      <button className="secondary-button" onClick={() => router.push(`/chapters?novel=${project.id}`)}>章节管理</button>
                      <button className="secondary-button" disabled={exportingProjectId === `${project.id}:txt`} onClick={() => exportProject(project, "txt")}>导出 TXT</button>
                      <button className="secondary-button" disabled={exportingProjectId === `${project.id}:markdown`} onClick={() => exportProject(project, "markdown")}>导出 MD</button>
                      <button className="danger-button" onClick={() => openDeleteProject(project)}>删除</button>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </div>
        </section>

        <section className="project-detail-stack">
          <section className="panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">起始需求文档</div>
                <div className="panel-subtitle">
                  {selectedProject ? `当前作品：${selectedProject.title}` : "选择作品后可维护其起始需求文档。"}
                </div>
              </div>
              <div className="inline-actions">
                <button className="secondary-button" disabled={!selectedProject} onClick={() => saveBrief(false)}>保存</button>
                <button className="primary-button" disabled={!selectedProject} onClick={() => saveBrief(true)}>保存并刷新作品圣经</button>
              </div>
            </div>
            <div className="panel-body">
              {!selectedProject ? (
                <EmptyState title="尚未选择作品" description="从左侧选择作品，或点击右上角新建作品。" />
              ) : (
                <BriefForm form={briefForm} onChange={updateBriefForm} sampleLibrary={sampleLibrary} />
              )}
              {projectError ? <div className="error-box">{projectError}</div> : null}
            </div>
          </section>

          <section className="panel story-bible-panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">创作设定状态</div>
                <div className="panel-subtitle">系统会基于起始需求文档自动维护内部创作设定。</div>
              </div>
              <div className="inline-actions">
                <button className="secondary-button" disabled={!selectedProjectId} onClick={() => generateStoryBible()}>
                  {storyBibleTask && ["queued", "running"].includes(storyBibleTask.status) ? "同步中..." : "同步创作设定"}
                </button>
              </div>
            </div>
            <div className="panel-body">
              {!selectedProject ? (
                <EmptyState title="尚未选择作品" description="选择作品后可查看创作设定同步状态。" />
              ) : (
                <div className="setting-status-grid">
                  <div className="setting-status-main">
                    <span className={`status-pill ${storyBibleTask?.status === "failed" ? "red" : storyBible ? "green" : "yellow"}`}>
                      {storyBibleTask && ["queued", "running"].includes(storyBibleTask.status)
                        ? `同步中 ${storyBibleTask.progress}%`
                        : storyBible
                          ? "已同步"
                          : "待同步"}
                    </span>
                    <h2>{selectedProject.title}</h2>
                    <p>{storyBible?.summary || "保存起始需求文档后，系统会自动整理内部创作设定，用于后续剧情事件和章节生成。"}</p>
                    {storyBibleTask?.error_message ? <div className="error-box">{storyBibleTask.error_message}</div> : null}
                    {storyBibleError ? <div className="error-box">{storyBibleError}</div> : null}
                  </div>
                  <div className="setting-status-side">
                    <div>
                      <span>设定版本</span>
                      <strong>{storyBible?.version ? `v${storyBible.version}` : "未生成"}</strong>
                    </div>
                    <div>
                      <span>生成方式</span>
                      <strong>{formatStoryBibleSource(storyBible?.source)}</strong>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </section>
        </section>
      </section>

      {showCreateDialog ? (
        <div className="modal-backdrop">
          <form className="confirm-dialog create-project-dialog" onSubmit={createProject}>
            <div className="panel-header bare">
              <div>
                <div className="panel-title">新建作品</div>
                <div className="panel-subtitle">填写初始需求后，系统会自动生成作品圣经。</div>
              </div>
              <button type="button" className="ghost-button" onClick={() => setShowCreateDialog(false)}>关闭</button>
            </div>
            <div className="create-project-body">
              <BriefForm form={createForm} onChange={updateCreateForm} sampleLibrary={sampleLibrary} />
            </div>
            {createError ? <div className="error-box">{createError}</div> : null}
            <div className="inline-actions dialog-actions">
              <button type="button" className="secondary-button" onClick={() => setShowCreateDialog(false)}>取消</button>
              <button className="primary-button">创建作品</button>
            </div>
          </form>
        </div>
      ) : null}
      {pendingDeleteProject ? (
        <div className="modal-backdrop" role="presentation" onClick={() => setPendingDeleteProject(null)}>
          <div className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-project-title" onClick={(event) => event.stopPropagation()}>
            <div>
              <div className="panel-title" id="delete-project-title">删除作品</div>
              <div className="panel-subtitle">该操作会删除作品的全部章节、任务、记忆、剧情事件、审校记录和作品设定。</div>
            </div>
            <div className="delete-preview danger-preview">
              <span>当前作品</span>
              <strong>{pendingDeleteProject.title}</strong>
              <p>请输入下面 8 位数字确认删除：</p>
              <div className="delete-code">{deleteCode}</div>
            </div>
            <label className="field">
              <span>确认码</span>
              <input
                value={deleteCodeInput}
                inputMode="numeric"
                maxLength={8}
                aria-invalid={deleteCodeMismatch || Boolean(deleteCodeError)}
                onChange={(event) => {
                  const nextCode = event.target.value.replace(/\D/g, "").slice(0, 8);
                  setDeleteCodeInput(nextCode);
                  setDeleteCodeError(
                    nextCode.length === 8 && nextCode !== deleteCode
                      ? "确认码不一致，请重新核对上方 8 位数字。"
                      : ""
                  );
                }}
                placeholder="输入上方 8 位数字"
              />
              {deleteCodeError ? <em className="field-error">{deleteCodeError}</em> : null}
            </label>
            <div className="inline-actions dialog-actions">
              <button className="secondary-button" disabled={deletingProjectId === pendingDeleteProject.id} onClick={() => setPendingDeleteProject(null)}>取消</button>
              <button
                className="danger-button"
                disabled={deleteCodeInput !== deleteCode || deletingProjectId === pendingDeleteProject.id}
                onClick={confirmDeleteProject}
              >
                {deletingProjectId === pendingDeleteProject.id ? "删除中" : "确认删除作品"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </AppShell>
  );
}

function BriefForm({ form, onChange, sampleLibrary = [] }) {
  const selectedSampleIds = Array.isArray(form.sample_reference_ids) ? form.sample_reference_ids : [];

  function toggleSampleReference(sampleId) {
    const nextIds = selectedSampleIds.includes(sampleId)
      ? selectedSampleIds.filter((id) => id !== sampleId)
      : [...selectedSampleIds, sampleId];
    onChange("sample_reference_ids", nextIds);
  }

  return (
    <div className="brief-form">
      <section className="brief-section">
        <div className="brief-section-head">
          <span>01</span>
          <div>
            <h3>基础定位</h3>
            <p>定义作品的外部标签和生产目标。</p>
          </div>
        </div>
        <div className="brief-fields grid-2">
          <div className="field"><label>作品名</label><input value={form.title} onChange={(e) => onChange("title", e.target.value)} /></div>
          <div className="field"><label>题材</label><input value={form.genre} onChange={(e) => onChange("genre", e.target.value)} /></div>
          <div className="field"><label>作品类型</label><input value={form.work_type} onChange={(e) => onChange("work_type", e.target.value)} /></div>
          <div className="field"><label>目标字数</label><input type="number" value={form.target_words} onChange={(e) => onChange("target_words", e.target.value)} /></div>
          <div className="field"><label>每章最少字数</label><input type="number" min="500" step="100" value={form.chapter_word_min} onChange={(e) => onChange("chapter_word_min", e.target.value)} /></div>
          <div className="field"><label>每章最多字数</label><input type="number" min="500" step="100" value={form.chapter_word_max} onChange={(e) => onChange("chapter_word_max", e.target.value)} /></div>
        </div>
      </section>

      <section className="brief-section">
        <div className="brief-section-head">
          <span>02</span>
          <div>
            <h3>故事核心</h3>
            <p>让系统知道这本书为什么值得持续写下去。</p>
          </div>
        </div>
        <div className="brief-fields">
          <div className="field"><label>一句话需求</label><textarea value={form.premise} onChange={(e) => onChange("premise", e.target.value)} /></div>
          <div className="field"><label>卖点与读者期待</label><textarea value={form.selling_points} onChange={(e) => onChange("selling_points", e.target.value)} /></div>
          <div className="field"><label>剧情方向</label><textarea value={form.plot_direction} onChange={(e) => onChange("plot_direction", e.target.value)} /></div>
        </div>
      </section>

      <section className="brief-section">
        <div className="brief-section-head">
          <span>03</span>
          <div>
            <h3>人物与世界</h3>
            <p>约束人物身份、动机和主要世界规则。</p>
          </div>
        </div>
        <div className="brief-fields grid-2">
          <div className="field"><label>主角设定</label><textarea value={form.protagonist} onChange={(e) => onChange("protagonist", e.target.value)} /></div>
          <div className="field"><label>世界观与核心规则</label><textarea value={form.worldview} onChange={(e) => onChange("worldview", e.target.value)} /></div>
        </div>
      </section>

      <section className="brief-section">
        <div className="brief-section-head">
          <span>04</span>
          <div>
            <h3>参考样本</h3>
            <p>选择已完成的优秀作品样本，作为本作品的风格、节奏和反 AI 约束。</p>
          </div>
        </div>
        {sampleLibrary.length === 0 ? (
          <div className="empty-inline">
            <h2>暂无可用样本</h2>
            <p>先到“样本分析”上传并完成分析，之后就可以在这里选择复用。</p>
          </div>
        ) : (
          <div className="sample-reference-grid">
            {sampleLibrary.map((sample) => {
              const selected = selectedSampleIds.includes(sample.id);
              return (
                <button
                  className={`sample-reference-card ${selected ? "active" : ""}`}
                  type="button"
                  key={sample.id}
                  onClick={() => toggleSampleReference(sample.id)}
                >
                  <span className={`sample-check ${selected ? "active" : ""}`}>{selected ? "已选" : "选择"}</span>
                  <strong>{sample.sample_title}</strong>
                  <em>{sample.source_novel_title || "样本库"} · {sample.source_genre || "未标注题材"}</em>
                  <p>{sample.summary || "已完成样本分析，可作为当前作品的风格参考。"}</p>
                  <div className="memory-tags">
                    {sample.source_word_count ? <span className="tag">{Number(sample.source_word_count).toLocaleString()} 字</span> : null}
                    {sample.chunk_count ? <span className="tag green">分片 {sample.chunk_count}</span> : null}
                    {sample.report?.llm_style_strategy?.available ? <span className="tag purple">LLM 策略</span> : <span className="tag yellow">量化指标</span>}
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </section>

      <section className="brief-section">
        <div className="brief-section-head">
          <span>05</span>
          <div>
            <h3>写作边界</h3>
            <p>控制风格、禁区和自动推进方式。</p>
          </div>
        </div>
        <div className="brief-fields">
          <div className="grid-2">
            <div className="field"><label>风格参考</label><textarea value={form.style_reference} onChange={(e) => onChange("style_reference", e.target.value)} /></div>
            <div className="field"><label>禁忌内容</label><textarea value={form.forbidden_content} onChange={(e) => onChange("forbidden_content", e.target.value)} /></div>
          </div>
          <div className="field"><label>自动化策略</label><textarea value={form.automation_strategy} onChange={(e) => onChange("automation_strategy", e.target.value)} /></div>
        </div>
      </section>
    </div>
  );
}

function formatStoryBibleSource(source) {
  const sourceMap = {
    llm: "模型生成",
    fallback: "本地生成",
    fallback_after_llm_error: "本地生成",
    auto_fallback_from_create: "自动初始化",
    manual: "手动维护",
    fallback_from_brief: "自动初始化"
  };
  return sourceMap[source] || "待同步";
}
