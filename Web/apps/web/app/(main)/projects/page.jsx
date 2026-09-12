"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import AppShellRegion from "@/components/AppShellRegion";
import EmptyState from "@/components/EmptyState";
import { apiDownload, apiFetch, buildTimestampedDownloadFilename, isTaskInFlight } from "@/lib/api";
import { parseMarkdownJsonArray } from "@/lib/markdownImport.mjs";
import { useLiveRefresh } from "@/lib/useLiveRefresh";

const defaultBrief = {
  work_type: "",
  story_era: "",
  story_location: "",
  selling_points: "",
  protagonist: "",
  worldview: "",
  plot_direction: "",
  chapter_word_min: "",
  chapter_word_max: "",
  event_chapter_count: "",
  style_reference: "",
  forbidden_content: "",
  automation_strategy: "",
  characters: [],
  planned_events: []
};

function makeItemKey(prefix) {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function createEmptyCharacter(key = makeItemKey("character")) {
  return { key, name: "", gender: "", age: "", occupation: "", is_protagonist: false, goal: "", detailed_setting: "" };
}

function createEmptyEvent(key = makeItemKey("event")) {
  return { key, title: "", event_type: "", story_stage: "", planned_time: "", summary: "", participant_character_ids: [], detailed_setting: "" };
}

const emptyProjectForm = {
  title: "",
  genre: "",
  target_words: "",
  premise: "",
  ...defaultBrief,
  characters: [{ ...createEmptyCharacter("character-1"), is_protagonist: true }],
  planned_events: []
};

const briefPlaceholders = {
  title: "请输入作品名",
  genre: "请输入题材，如都市、玄幻、悬疑、言情等",
  work_type: "请输入作品类型，如长篇小说、短篇合集、系列文等",
  story_era: "必须填写，如2020年代中国、1990年代上海、唐玄宗开元年间",
  story_location: "主要国家、城市或架空地域；用于时代资料检索",
  target_words: "请输入预计总字数",
  chapter_word_min: "请输入单章最少字数",
  chapter_word_max: "请输入单章最多字数",
  event_chapter_count: "普通事件建议4-6章，复杂转折事件可适当增加",
  premise: "用一两句话说明故事起点、核心悬念或主角处境",
  selling_points: "说明读者为什么愿意持续追读，如爽点、反转、关系张力",
  plot_direction: "说明主线会如何推进，阶段目标或最终方向是什么",
  protagonist: "说明主角身份、目标、缺陷、动机和成长方向",
  worldview: "说明故事背景、世界规则、势力关系或关键限制",
  style_reference: "说明希望接近的文风、节奏、叙事距离和对白风格",
  forbidden_content: "说明不希望出现的套路、雷点、题材禁区或表达方式",
  automation_strategy: "说明自动生成时哪些情况可以自动推进，哪些需要暂停确认"
};

const requirementDocSections = [
  {
    title: "基础定位",
    fields: [
      { key: "title", label: "作品名", placeholder: briefPlaceholders.title },
      { key: "genre", label: "题材", placeholder: briefPlaceholders.genre },
      { key: "work_type", label: "作品类型", placeholder: briefPlaceholders.work_type },
      { key: "story_era", label: "故事发生年代", placeholder: briefPlaceholders.story_era },
      { key: "story_location", label: "主要发生地", placeholder: briefPlaceholders.story_location },
      { key: "target_words", label: "目标字数", placeholder: briefPlaceholders.target_words },
      { key: "chapter_word_min", label: "每章最少字数", placeholder: briefPlaceholders.chapter_word_min },
      { key: "chapter_word_max", label: "每章最多字数", placeholder: briefPlaceholders.chapter_word_max },
      { key: "event_chapter_count", label: "每个事件章节数", placeholder: briefPlaceholders.event_chapter_count }
    ]
  },
  {
    title: "故事核心",
    fields: [
      { key: "premise", label: "一句话需求", placeholder: briefPlaceholders.premise },
      { key: "selling_points", label: "卖点与读者期待", placeholder: briefPlaceholders.selling_points },
      { key: "plot_direction", label: "剧情方向", placeholder: briefPlaceholders.plot_direction }
    ]
  },
  {
    title: "世界设定",
    fields: [
      { key: "worldview", label: "世界观与核心规则", placeholder: briefPlaceholders.worldview }
    ]
  },
  {
    title: "写作边界",
    fields: [
      { key: "style_reference", label: "风格参考", placeholder: briefPlaceholders.style_reference },
      { key: "forbidden_content", label: "禁忌内容", placeholder: briefPlaceholders.forbidden_content },
      { key: "automation_strategy", label: "自动化策略", placeholder: briefPlaceholders.automation_strategy }
    ]
  }
];

const requirementDocFields = requirementDocSections.flatMap((section) => section.fields);

function projectToForm(project) {
  const brief = project?.brief || {};
  const characters = Array.isArray(brief.characters) ? brief.characters.map((item, index) => ({
    ...createEmptyCharacter(item.key || `character-${index + 1}`),
    ...item,
    age: String(item.age ?? ""),
    is_protagonist: Boolean(item.is_protagonist)
  })) : [];
  const plannedEvents = Array.isArray(brief.planned_events) ? brief.planned_events.map((item, index) => ({
    ...createEmptyEvent(item.key || `event-${index + 1}`),
    ...item,
    participant_character_ids: Array.isArray(item.participant_character_ids) ? item.participant_character_ids : []
  })) : [];
  return {
    title: project?.title || "",
    genre: project?.genre || "",
    target_words: project?.target_words || "",
    premise: project?.premise || "",
    work_type: brief.work_type || "",
    story_era: brief.story_era || "",
    story_location: brief.story_location || "",
    selling_points: brief.selling_points || "",
    protagonist: brief.protagonist || "",
    worldview: brief.worldview || "",
    plot_direction: brief.plot_direction || "",
    chapter_word_min: brief.chapter_word_min || "",
    chapter_word_max: brief.chapter_word_max || "",
    event_chapter_count: brief.event_chapter_count || "",
    style_reference: brief.style_reference || "",
    forbidden_content: brief.forbidden_content || "",
    automation_strategy: brief.automation_strategy || "",
    characters,
    planned_events: plannedEvents
  };
}

function formToNovelPayload(form) {
  const { title, genre, target_words, premise, ...brief } = form;
  const characters = (brief.characters || []).map((item) => ({
    key: item.key,
    name: item.name.trim(),
    gender: item.gender,
    age: String(item.age).trim(),
    occupation: item.occupation.trim(),
    is_protagonist: Boolean(item.is_protagonist),
    goal: item.goal.trim(),
    detailed_setting: item.detailed_setting.trim()
  }));
  const plannedEvents = (brief.planned_events || []).map((item) => ({
    key: item.key,
    title: item.title.trim(),
    event_type: item.event_type.trim(),
    story_stage: item.story_stage.trim(),
    planned_time: item.planned_time.trim(),
    summary: item.summary.trim(),
    participant_character_ids: Array.isArray(item.participant_character_ids) ? item.participant_character_ids : [],
    detailed_setting: item.detailed_setting.trim()
  }));
  const protagonist = characters.find((item) => item.is_protagonist);
  return {
    title,
    genre,
    target_words: _numberOrDefault(target_words, 300000),
    premise,
    brief: {
      ...brief,
      protagonist: protagonist
        ? `${protagonist.name}，${protagonist.age}，${protagonist.occupation}。${protagonist.detailed_setting}`
        : brief.protagonist,
      characters,
      planned_events: plannedEvents,
      chapter_word_min: _numberOrDefault(brief.chapter_word_min, 2500),
      chapter_word_max: _numberOrDefault(brief.chapter_word_max, 2800),
      event_chapter_count: _numberOrDefault(brief.event_chapter_count, 6)
    }
  };
}

function validateStructuredRequirements(form) {
  if (!form.story_era?.trim()) return "请填写故事发生年代，这是生成内容的时代硬约束。";
  const characters = Array.isArray(form.characters) ? form.characters : [];
  if (characters.length === 0) return "请至少添加一名人物。";
  for (let index = 0; index < characters.length; index += 1) {
    const item = characters[index];
    if (!item.name?.trim() || !item.gender || !String(item.age || "").trim() || !item.occupation?.trim()) {
      return `人物 ${index + 1} 必须填写姓名、性别、年龄和职业。`;
    }
  }
  if (!characters.some((item) => item.is_protagonist)) return "请至少将一名人物标记为主角。";
  return "";
}

function _numberOrDefault(value, fallback) {
  if (value === "" || value === null || value === undefined) return fallback;
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : fallback;
}

function _safeFilename(name) {
  return (name || "未命名作品").replace(/[\\/:*?"<>|]/g, "_").replace(/\s+/g, "-").slice(0, 80);
}

function downloadTextFile(filename, content, mimeType = "text/markdown;charset=utf-8") {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function buildRequirementMarkdown(form, { template = false } = {}) {
  const lines = [
    "# 起始需求文档",
    "",
    template
      ? "> 按每个小标题填写内容后，可在 NovelForge「作品管理」中导入该 Markdown 文件。"
      : "> 该文档由 NovelForge 作品管理页导出，可再次导入并填充起始需求表单。",
    ""
  ];

  requirementDocSections.forEach((section) => {
    lines.push(`## ${section.title}`, "");
    section.fields.forEach((field) => {
      const value = form?.[field.key];
      lines.push(`### ${field.label}`);
      lines.push(template ? `<!-- ${field.placeholder} -->` : String(value ?? "").trim());
      lines.push("");
    });
  });

  const characters = template
    ? [{ name: "", gender: "", age: "", occupation: "", is_protagonist: true, goal: "", detailed_setting: "" }]
    : (form?.characters || []).map(({ key, ...item }) => item);
  lines.push(
    "## 结构化人物设定",
    "",
    "> 可直接增删 JSON 数组中的对象。人物必填姓名、性别、年龄、职业、是否主角；剧情事件由系统自动规划。",
    "",
    "### 人物设定（JSON）",
    "```json",
    JSON.stringify(characters, null, 2),
    "```",
    ""
  );

  return `${lines.join("\n").trim()}\n`;
}

function _normalizeHeading(text) {
  return String(text || "")
    .replace(/^[#\s]+/, "")
    .replace(/[：:]\s*$/, "")
    .replace(/[`*_]/g, "")
    .trim();
}

function _stripMdValue(text) {
  const value = String(text || "")
    .replace(/<!--[\s\S]*?-->/g, "")
    .split("\n")
    .map((line) => line.replace(/^>\s?/, "").trimEnd())
    .join("\n")
    .trim();
  return value;
}

function _escapeRegExp(text) {
  return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function _extractColonValue(markdown, label) {
  const match = markdown.match(new RegExp(`(?:^|\\n)\\s*(?:[-*]\\s*)?${_escapeRegExp(label)}\\s*[:：]\\s*([^\\n]+)`, "i"));
  return _stripMdValue(match?.[1] || "");
}

function _parseJsonSection(value, label) {
  if (!value) return null;
  try {
    return parseMarkdownJsonArray(value);
  } catch (error) {
    throw new Error(`${label} JSON 格式错误：${error.message}`);
  }
}

function parseRequirementMarkdown(markdown, currentForm) {
  const nextForm = { ...currentForm };
  const headingValues = {};
  const lines = String(markdown || "").split(/\r?\n/);
  let currentLabel = "";
  let buffer = [];

  function flush() {
    if (!currentLabel) return;
    const value = _stripMdValue(buffer.join("\n"));
    if (value) headingValues[currentLabel] = value;
  }

  lines.forEach((line) => {
    const heading = line.match(/^\s*(#{2,6})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      flush();
      currentLabel = _normalizeHeading(heading[2]);
      buffer = [];
      return;
    }
    buffer.push(line);
  });
  flush();

  let filledCount = 0;
  requirementDocFields.forEach((field) => {
    const value = headingValues[field.label] || _extractColonValue(markdown, field.label);
    if (!value) return;
    nextForm[field.key] = ["target_words", "chapter_word_min", "chapter_word_max", "event_chapter_count"].includes(field.key)
      ? value.replace(/[^\d]/g, "")
      : value;
    filledCount += 1;
  });

  const characterSection = headingValues["人物设定（JSON）"]
    || headingValues["人物设定(JSON)"]
    || Object.entries(headingValues).find(
      ([heading, value]) => /人物设定/i.test(heading) && /[\[{]/.test(value)
    )?.[1];
  const importedCharacters = _parseJsonSection(characterSection, "人物设定");
  if (importedCharacters) {
    nextForm.characters = importedCharacters.map((item, index) => ({
      ...createEmptyCharacter(item.key || makeItemKey(`character-${index + 1}`)),
      ...item,
      age: String(item.age ?? ""),
      is_protagonist: Boolean(item.is_protagonist)
    }));
    filledCount += 1;
  }
  return { form: nextForm, filledCount };
}

const DELETE_CONFIRM_CODE_LENGTH = 4;

function createDeleteCode() {
  return String(Math.floor(1000 + Math.random() * 9000));
}

export default function ProjectsPage() {
  const router = useRouter();
  const projectPickerRef = useRef(null);
  const requirementImportInputRef = useRef(null);
  const createRequirementImportInputRef = useRef(null);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  // 作品列表/作品圣经走 SWR 缓存：切回页面秒显缓存，后台静默刷新。
  const { data: projects = [], mutate: mutateProjects } = useSWR("/api/novels");
  const [projectMenuOpen, setProjectMenuOpen] = useState(false);
  const [briefForm, setBriefForm] = useState(projectToForm(null));
  const [createForm, setCreateForm] = useState(emptyProjectForm);
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [storyBibleTask, setStoryBibleTask] = useState(null);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleteProjectIds, setDeleteProjectIds] = useState([]);
  const [deleteCode, setDeleteCode] = useState("");
  const [deleteCodeInput, setDeleteCodeInput] = useState("");
  const [deleteCodeError, setDeleteCodeError] = useState("");
  const [deletingProjectIds, setDeletingProjectIds] = useState([]);
  const [exportingProjectId, setExportingProjectId] = useState("");
  const [projectError, setProjectError] = useState("");
  const [createError, setCreateError] = useState("");
  const [createDocMessage, setCreateDocMessage] = useState("");
  const [storyBibleError, setStoryBibleError] = useState("");
  const [briefDocMessage, setBriefDocMessage] = useState("");

  const selectedProject = useMemo(
    () => projects.find((project) => project.id === selectedProjectId),
    [projects, selectedProjectId]
  );
  const selectedDeleteProjects = useMemo(
    () => projects.filter((project) => deleteProjectIds.includes(project.id)),
    [projects, deleteProjectIds]
  );
  const isDeletingProjects = deletingProjectIds.length > 0;
  const deleteCodeMismatch = (
    deleteCodeInput.length === DELETE_CONFIRM_CODE_LENGTH
    && deleteCodeInput !== deleteCode
  );

  const { data: storyBible = null, mutate: mutateStoryBible } = useSWR(
    selectedProject?.id ? `/api/novels/${selectedProject.id}/story-bible` : null
  );

  useEffect(() => {
    // 作品列表由 SWR 供给；默认选中第一部作品。
    if (!selectedProjectId && projects.length) setSelectedProjectId(projects[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projects]);

  useEffect(() => {
    function closeProjectMenuOnOutsideClick(event) {
      if (!projectPickerRef.current?.contains(event.target)) {
        setProjectMenuOpen(false);
      }
    }

    document.addEventListener("mousedown", closeProjectMenuOnOutsideClick);
    return () => document.removeEventListener("mousedown", closeProjectMenuOnOutsideClick);
  }, []);

  useEffect(() => {
    setBriefForm(projectToForm(selectedProject));
    setProjectError("");
    setStoryBibleError("");
    setBriefDocMessage("");
    setStoryBibleTask(null);
    if (selectedProject?.id) {
      loadActiveStoryBibleTask(selectedProject.id).catch((err) => setStoryBibleError(err.message));
    }
  }, [selectedProject?.id]);

  useLiveRefresh({
    enabled: Boolean(selectedProjectId && storyBibleTask?.id && isTaskInFlight(storyBibleTask?.status)),
    intervalMs: 1800,
    refresh: async () => {
      const task = await apiFetch(`/api/novels/${selectedProjectId}/tasks/${storyBibleTask.id}`);
      setStoryBibleTask(task);
      setStoryBibleError("");
      if (["completed", "waiting"].includes(task.status)) {
        await mutateStoryBible();
      }
    },
    onError: (err) => setStoryBibleError(err.message)
  });

  function updateBriefForm(key, value) {
    setBriefForm((current) => ({ ...current, [key]: value }));
  }

  function updateCreateForm(key, value) {
    setCreateForm((current) => ({ ...current, [key]: value }));
  }

  function openCreateDialog() {
    setCreateError("");
    setCreateDocMessage("");
    setShowCreateDialog(true);
  }

  async function createProject(event) {
    event.preventDefault();
    setCreateError("");
    if (!createForm.title.trim()) {
      setCreateError("请先填写作品名。");
      return;
    }
    if (!createForm.target_words || Number(createForm.target_words) <= 0) {
      setCreateError("请填写有效的目标字数。");
      return;
    }
    if (!createForm.premise.trim()) {
      setCreateError("请填写一句话需求，方便系统生成作品圣经。");
      return;
    }
    const structuredError = validateStructuredRequirements(createForm);
    if (structuredError) {
      setCreateError(structuredError);
      return;
    }
    try {
      const created = await apiFetch("/api/novels", {
        method: "POST",
        body: JSON.stringify(formToNovelPayload(createForm))
      });
      mutateProjects((current = []) => [created, ...current], { revalidate: false });
      setSelectedProjectId(created.id);
      setCreateForm(emptyProjectForm);
      setCreateDocMessage("");
      setShowCreateDialog(false);
      router.push(`/workbench?novel=${created.id}`);
    } catch (err) {
      setCreateError(err.message);
    }
  }

  async function saveBrief(regenerateBible = false) {
    if (!selectedProject) return;
    setProjectError("");
    const structuredError = validateStructuredRequirements(briefForm);
    if (structuredError) {
      setProjectError(structuredError);
      return;
    }
    try {
      const saved = await apiFetch(`/api/novels/${selectedProject.id}`, {
        method: "PATCH",
        body: JSON.stringify(formToNovelPayload(briefForm))
      });
      mutateProjects((current = []) => current.map((project) => (project.id === saved.id ? saved : project)), { revalidate: false });
      if (regenerateBible) {
        await generateStoryBible(saved.id, "brief_saved");
      }
    } catch (err) {
      setProjectError(err.message);
    }
  }

  async function loadActiveStoryBibleTask(projectId) {
    const [runningTasks, queuedTasks] = await Promise.all([
      apiFetch(`/api/novels/${projectId}/tasks?status_filter=running`),
      apiFetch(`/api/novels/${projectId}/tasks?status_filter=queued`)
    ]);
    const task = [...runningTasks, ...queuedTasks].find(
      (item) => item.task_type === "build_story_bible"
    );
    setStoryBibleTask((current) => (
      task || (current && isTaskInFlight(current.status) ? current : null)
    ));
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

  function openDeleteProjects() {
    setDeleteProjectIds(selectedProjectId ? [selectedProjectId] : []);
    setDeleteDialogOpen(true);
    setDeleteCode(createDeleteCode());
    setDeleteCodeInput("");
    setDeleteCodeError("");
    setProjectError("");
  }

  function closeDeleteDialog() {
    if (isDeletingProjects) return;
    setDeleteDialogOpen(false);
    setDeleteProjectIds([]);
    setDeleteCode("");
    setDeleteCodeInput("");
    setDeleteCodeError("");
  }

  function toggleDeleteProject(projectId) {
    setDeleteProjectIds((current) => (
      current.includes(projectId)
        ? current.filter((id) => id !== projectId)
        : [...current, projectId]
    ));
    setDeleteCodeError("");
  }

  async function confirmDeleteProjects() {
    if (selectedDeleteProjects.length === 0) {
      setDeleteCodeError("请至少选择一部要删除的作品。");
      return;
    }
    if (deleteCodeInput !== deleteCode) {
      setDeleteCodeError(
        deleteCodeInput.length === DELETE_CONFIRM_CODE_LENGTH
          ? "确认码不一致，请重新核对上方 4 位数字。"
          : "请完整输入上方 4 位确认码。"
      );
      return;
    }
    setDeletingProjectIds(deleteProjectIds);
    setProjectError("");
    const deletedProjectIds = [];
    try {
      for (const projectId of deleteProjectIds) {
        await apiFetch(`/api/novels/${projectId}`, {
          method: "DELETE",
          body: JSON.stringify({
            confirmation_code: deleteCodeInput,
            expected_code: deleteCode
          })
        });
        deletedProjectIds.push(projectId);
      }
      const remainingProjects = projects.filter((project) => !deleteProjectIds.includes(project.id));
      mutateProjects(remainingProjects, { revalidate: false });
      setSelectedProjectId((current) => (deleteProjectIds.includes(current) ? remainingProjects[0]?.id || "" : current));
      setDeleteDialogOpen(false);
      setDeleteProjectIds([]);
      setDeleteCode("");
      setDeleteCodeInput("");
      setDeleteCodeError("");
    } catch (err) {
      if (deletedProjectIds.length > 0) {
        const remainingProjects = projects.filter((project) => !deletedProjectIds.includes(project.id));
        mutateProjects(remainingProjects, { revalidate: false });
        setSelectedProjectId((current) => (deletedProjectIds.includes(current) ? remainingProjects[0]?.id || "" : current));
        setDeleteProjectIds((current) => current.filter((projectId) => !deletedProjectIds.includes(projectId)));
      }
      setDeleteCodeError(`删除失败：${err.message}`);
    } finally {
      setDeletingProjectIds([]);
    }
  }

  async function exportProject(project, format) {
    if (!project?.id) return;
    setProjectError("");
    setExportingProjectId(`${project.id}:${format}`);
    try {
      await apiDownload(
        `/api/novels/${project.id}/export?format=${format}`,
        buildTimestampedDownloadFilename(
          project.title || "novel",
          format === "txt" ? "txt" : "md"
        )
      );
    } catch (err) {
      setProjectError(err.message);
    } finally {
      setExportingProjectId("");
    }
  }

  function exportCurrentRequirementDoc() {
    if (!selectedProject) return;
    const filename = `${_safeFilename(briefForm.title || selectedProject.title)}-起始需求文档.md`;
    downloadTextFile(filename, buildRequirementMarkdown(briefForm));
    setBriefDocMessage("已导出当前起始需求文档。");
  }

  function exportRequirementTemplate() {
    downloadTextFile("NovelForge-起始需求文档模板.md", buildRequirementMarkdown(emptyProjectForm, { template: true }));
    setBriefDocMessage("已导出起始需求文档模板。");
  }

  function exportCreateRequirementTemplate() {
    downloadTextFile("NovelForge-起始需求文档模板.md", buildRequirementMarkdown(emptyProjectForm, { template: true }));
    setCreateDocMessage("已导出起始需求文档模板。");
  }

  async function importRequirementDocToForm(event, { currentForm, setForm, setMessage, setError, actionText }) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setError("");
    setMessage("");
    if (!file.name.toLowerCase().endsWith(".md") && file.type && !file.type.includes("markdown")) {
      setError("请导入 Markdown（.md）文件。");
      return;
    }
    try {
      const text = await file.text();
      const result = parseRequirementMarkdown(text, currentForm);
      if (result.filledCount === 0) {
        setError("没有识别到可填充字段。建议使用“导出模板文件”生成的格式。");
        return;
      }
      setForm(result.form);
      setMessage(`已从 ${file.name} 解析并填充 ${result.filledCount} 个字段，请检查后再${actionText}。`);
    } catch (err) {
      setError(`导入失败：${err.message}`);
    }
  }

  async function importRequirementDoc(event) {
    await importRequirementDocToForm(event, {
      currentForm: briefForm,
      setForm: setBriefForm,
      setMessage: setBriefDocMessage,
      setError: setProjectError,
      actionText: "保存"
    });
  }

  async function importCreateRequirementDoc(event) {
    await importRequirementDocToForm(event, {
      currentForm: createForm,
      setForm: setCreateForm,
      setMessage: setCreateDocMessage,
      setError: setCreateError,
      actionText: "创建作品"
    });
  }

  return (
    <>
    <AppShellRegion
      title="作品管理"
      actions={(
        <div className="project-top-actions">
          <div className="project-picker" ref={projectPickerRef}>
            <button
              type="button"
              className={`project-picker-trigger ${projectMenuOpen ? "open" : ""}`}
              disabled={projects.length === 0}
              onClick={() => setProjectMenuOpen((open) => !open)}
            >
              <span>
                <em>当前作品</em>
                <strong>{selectedProject?.title || "请选择作品"}</strong>
              </span>
              <i aria-hidden="true">
                <svg viewBox="0 0 20 20">
                  <path d="M5 7.5L10 12.5L15 7.5" />
                </svg>
              </i>
            </button>
            {projectMenuOpen ? (
              <div className="project-picker-menu" role="listbox">
                {projects.map((project) => (
                  <button
                    type="button"
                    className={`project-picker-option ${project.id === selectedProjectId ? "active" : ""}`}
                    key={project.id}
                    onClick={() => {
                      setSelectedProjectId(project.id);
                      setProjectMenuOpen(false);
                    }}
                  >
                    <span>
                      <strong>{project.title}</strong>
                      <em>{project.genre || "未分类"} · {project.brief?.work_type || "小说"}</em>
                    </span>
                    <small>{Number(project.target_words || 0).toLocaleString()} 字</small>
                  </button>
                ))}
              </div>
            ) : null}
          </div>
          <button className="danger-button" disabled={projects.length === 0} onClick={openDeleteProjects}>删除作品</button>
          <button className="primary-button" onClick={openCreateDialog}>新建作品</button>
        </div>
      )}
    />
      <section className="projects-workspace">
        <section className="project-detail-stack">
          <section className="panel project-overview-panel">
            <div className="panel-body">
              {!selectedProject ? (
                <EmptyState title="还没有作品" description="点击右上角新建作品，填写第一份起始需求文档。" />
              ) : (
                <div className="project-overview">
                  <div>
                    <div className="project-type">{selectedProject.genre || "未分类"} · {selectedProject.brief?.work_type || "小说"}</div>
                    <h2>{selectedProject.title}</h2>
                    <p>{selectedProject.premise || "暂无起始需求。"}</p>
                  </div>
                  <div className="project-meta-grid">
                    <div><span>章节</span><strong>{selectedProject.current_chapter_index}</strong></div>
                    <div><span>目标</span><strong>{Number(selectedProject.target_words || 0).toLocaleString()}</strong></div>
                    <div><span>单章</span><strong>{selectedProject.brief?.chapter_word_min || 2500}-{selectedProject.brief?.chapter_word_max || 2800}</strong></div>
                    <div><span>单事件</span><strong>{selectedProject.brief?.event_chapter_count || 6} 章</strong></div>
                  </div>
                  <div className="inline-actions">
                    <button className="secondary-button" disabled={exportingProjectId === `${selectedProject.id}:txt`} onClick={() => exportProject(selectedProject, "txt")}>导出 TXT</button>
                    <button className="secondary-button" disabled={exportingProjectId === `${selectedProject.id}:markdown`} onClick={() => exportProject(selectedProject, "markdown")}>导出 MD</button>
                    <span className="tag green">{selectedProject.status}</span>
                  </div>
                </div>
              )}
            </div>
          </section>

          <section className="panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">起始需求文档</div>
                <div className="panel-subtitle">
                  {selectedProject ? `当前作品：${selectedProject.title}` : "选择作品后可维护其起始需求文档。"}
                </div>
              </div>
              <div className="inline-actions">
                <input
                  ref={requirementImportInputRef}
                  type="file"
                  accept=".md,text/markdown,text/plain"
                  className="visually-hidden-input"
                  onChange={importRequirementDoc}
                />
                <button className="secondary-button" disabled={!selectedProject} onClick={() => requirementImportInputRef.current?.click()}>导入 MD</button>
                <button className="secondary-button" disabled={!selectedProject} onClick={exportCurrentRequirementDoc}>导出当前文档</button>
                <button className="secondary-button" onClick={exportRequirementTemplate}>导出模板文件</button>
                <button className="secondary-button" disabled={!selectedProject} onClick={() => saveBrief(false)}>仅保存</button>
                <button className="primary-button" disabled={!selectedProject} onClick={() => saveBrief(true)}>保存并重建创作设定</button>
              </div>
            </div>
            <div className="panel-body">
              {briefDocMessage ? <div className="success-box">{briefDocMessage}</div> : null}
              {projectError ? <div className="error-box">{projectError}</div> : null}
              {!selectedProject ? (
                <EmptyState title="尚未选择作品" description="从左侧选择作品，或点击右上角新建作品。" />
              ) : (
                <BriefForm form={briefForm} onChange={updateBriefForm} />
              )}
            </div>
          </section>

          <section className="panel story-bible-panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">创作设定状态</div>
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
        <div className="modal-backdrop create-project-backdrop" role="presentation">
          <form
            className="confirm-dialog create-project-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="create-project-title"
            onSubmit={createProject}
          >
            <div className="panel-header bare">
              <div>
                <div className="panel-title" id="create-project-title">新建作品</div>
              </div>
              <div className="inline-actions">
                <input
                  ref={createRequirementImportInputRef}
                  type="file"
                  accept=".md,text/markdown,text/plain"
                  className="visually-hidden-input"
                  onChange={importCreateRequirementDoc}
                />
                <button type="button" className="secondary-button" onClick={() => createRequirementImportInputRef.current?.click()}>导入 MD</button>
                <button type="button" className="secondary-button" onClick={exportCreateRequirementTemplate}>导出模板文件</button>
                <button type="button" className="ghost-button" onClick={() => setShowCreateDialog(false)}>关闭</button>
              </div>
            </div>
            {createDocMessage ? <div className="success-box">{createDocMessage}</div> : null}
            {createError ? <div className="error-box">{createError}</div> : null}
            <div className="create-project-body">
              <BriefForm form={createForm} onChange={updateCreateForm} />
            </div>
            <div className="inline-actions dialog-actions">
              <button type="button" className="secondary-button" onClick={() => setShowCreateDialog(false)}>取消</button>
              <button className="primary-button">创建作品</button>
            </div>
          </form>
        </div>
      ) : null}
      {deleteDialogOpen ? (
        <div className="modal-backdrop" role="presentation" onClick={closeDeleteDialog}>
          <div className="confirm-dialog delete-project-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-project-title" onClick={(event) => event.stopPropagation()}>
            <div>
              <div className="panel-title" id="delete-project-title">删除作品</div>
              <div className="panel-subtitle">可一次选择多部作品，只需要输入一次确认码。该操作会删除作品的全部章节、任务、记忆、剧情事件、审校记录和作品设定。</div>
            </div>
            <div className="delete-project-list" role="group" aria-label="选择要删除的作品">
              {projects.map((project) => {
                const selected = deleteProjectIds.includes(project.id);
                return (
                  <button
                    type="button"
                    className={`delete-project-option ${selected ? "active" : ""}`}
                    key={project.id}
                    disabled={isDeletingProjects}
                    onClick={() => toggleDeleteProject(project.id)}
                  >
                    <span className={`checkmark ${selected ? "active" : ""}`}>{selected ? "✓" : ""}</span>
                    <span>
                      <strong>{project.title}</strong>
                      <em>{project.genre || "未分类"} · {Number(project.target_words || 0).toLocaleString()} 字 · {project.current_chapter_index || 0} 章</em>
                    </span>
                  </button>
                );
              })}
            </div>
            <div className="delete-preview danger-preview">
              <span>将删除 {selectedDeleteProjects.length} 部作品</span>
              <strong>{selectedDeleteProjects.map((project) => project.title).join("、") || "尚未选择作品"}</strong>
              <p>请输入下面 4 位数字确认删除：</p>
              <div className="delete-code">{deleteCode}</div>
            </div>
            <label className="field">
              <span>确认码</span>
              <input
                value={deleteCodeInput}
                inputMode="numeric"
                maxLength={DELETE_CONFIRM_CODE_LENGTH}
                aria-invalid={deleteCodeMismatch || Boolean(deleteCodeError)}
                onChange={(event) => {
                  const nextCode = event.target.value.replace(/\D/g, "").slice(0, DELETE_CONFIRM_CODE_LENGTH);
                  setDeleteCodeInput(nextCode);
                  setDeleteCodeError(
                    nextCode.length === DELETE_CONFIRM_CODE_LENGTH && nextCode !== deleteCode
                      ? "确认码不一致，请重新核对上方 4 位数字。"
                      : ""
                  );
                }}
                placeholder="输入上方 4 位数字"
              />
              {deleteCodeError ? <em className="field-error">{deleteCodeError}</em> : null}
            </label>
            <div className="inline-actions dialog-actions">
              <button className="secondary-button" disabled={isDeletingProjects} onClick={closeDeleteDialog}>取消</button>
              <button
                className="danger-button"
                disabled={deleteCodeInput !== deleteCode || selectedDeleteProjects.length === 0 || isDeletingProjects}
                onClick={confirmDeleteProjects}
              >
                {isDeletingProjects ? "删除中" : `确认删除 ${selectedDeleteProjects.length || ""} 部作品`}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}

function BriefForm({ form, onChange }) {
  const characters = Array.isArray(form.characters) ? form.characters : [];

  function updateCharacter(characterKey, field, value) {
    onChange("characters", characters.map((item) => (
      item.key === characterKey ? { ...item, [field]: value } : item
    )));
  }

  function addCharacter() {
    onChange("characters", [...characters, createEmptyCharacter()]);
  }

  function removeCharacter(characterKey) {
    onChange("characters", characters.filter((item) => item.key !== characterKey));
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
          <div className="field"><label>作品名</label><input value={form.title} placeholder={briefPlaceholders.title} onChange={(e) => onChange("title", e.target.value)} /></div>
          <div className="field"><label>题材</label><input value={form.genre} placeholder={briefPlaceholders.genre} onChange={(e) => onChange("genre", e.target.value)} /></div>
          <div className="field"><label>作品类型</label><input value={form.work_type} placeholder={briefPlaceholders.work_type} onChange={(e) => onChange("work_type", e.target.value)} /></div>
          <div className="field"><label>故事发生年代 *</label><input required value={form.story_era} placeholder={briefPlaceholders.story_era} onChange={(e) => onChange("story_era", e.target.value)} /></div>
          <div className="field"><label>主要发生地</label><input value={form.story_location} placeholder={briefPlaceholders.story_location} onChange={(e) => onChange("story_location", e.target.value)} /></div>
          <div className="field"><label>目标字数</label><input type="number" value={form.target_words} placeholder={briefPlaceholders.target_words} onChange={(e) => onChange("target_words", e.target.value)} /></div>
          <div className="field"><label>每章最少字数</label><input type="number" min="500" step="100" value={form.chapter_word_min} placeholder={briefPlaceholders.chapter_word_min} onChange={(e) => onChange("chapter_word_min", e.target.value)} /></div>
          <div className="field"><label>每章最多字数</label><input type="number" min="500" step="100" value={form.chapter_word_max} placeholder={briefPlaceholders.chapter_word_max} onChange={(e) => onChange("chapter_word_max", e.target.value)} /></div>
          <div className="field"><label>每个事件章节数</label><input type="number" min="4" max="12" step="1" value={form.event_chapter_count} placeholder={briefPlaceholders.event_chapter_count} onChange={(e) => onChange("event_chapter_count", e.target.value)} /></div>
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
          <div className="field"><label>一句话需求</label><textarea value={form.premise} placeholder={briefPlaceholders.premise} onChange={(e) => onChange("premise", e.target.value)} /></div>
          <div className="field"><label>卖点与读者期待</label><textarea value={form.selling_points} placeholder={briefPlaceholders.selling_points} onChange={(e) => onChange("selling_points", e.target.value)} /></div>
          <div className="field"><label>剧情方向</label><textarea value={form.plot_direction} placeholder={briefPlaceholders.plot_direction} onChange={(e) => onChange("plot_direction", e.target.value)} /></div>
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
        <div className="brief-fields">
          <div className="field"><label>世界观与核心规则</label><textarea value={form.worldview} placeholder={briefPlaceholders.worldview} onChange={(e) => onChange("worldview", e.target.value)} /></div>
          <div className="structured-editor-head">
            <div><strong>人物设定</strong><p>姓名、性别、年龄、职业和是否主角为生成前必填信息。</p></div>
            <button type="button" className="secondary-button" onClick={addCharacter}>添加人物</button>
          </div>
          <div className="structured-card-list">
            {characters.map((character, index) => (
              <article className="structured-card" key={character.key}>
                <div className="structured-card-head">
                  <strong>人物 {index + 1}{character.name ? ` · ${character.name}` : ""}</strong>
                  <button type="button" className="text-danger-button" onClick={() => removeCharacter(character.key)}>删除</button>
                </div>
                <div className="grid-2">
                  <div className="field"><label>姓名 *</label><input value={character.name} onChange={(e) => updateCharacter(character.key, "name", e.target.value)} /></div>
                  <div className="field"><label>性别 *</label><select value={character.gender} onChange={(e) => updateCharacter(character.key, "gender", e.target.value)}><option value="">请选择</option><option value="女">女</option><option value="男">男</option><option value="非二元/其他">非二元/其他</option><option value="不适用">不适用</option></select></div>
                  <div className="field"><label>年龄 *</label><input value={character.age} placeholder="可填具体年龄或年龄段" onChange={(e) => updateCharacter(character.key, "age", e.target.value)} /></div>
                  <div className="field"><label>职业 *</label><input value={character.occupation} onChange={(e) => updateCharacter(character.key, "occupation", e.target.value)} /></div>
                </div>
                <label className="structured-checkbox"><input type="checkbox" checked={character.is_protagonist} onChange={(e) => updateCharacter(character.key, "is_protagonist", e.target.checked)} /><span>是否为主角</span></label>
                <div className="field"><label>人物目标</label><input value={character.goal} onChange={(e) => updateCharacter(character.key, "goal", e.target.value)} /></div>
                <div className="field"><label>详细设定</label><textarea value={character.detailed_setting} placeholder="补充外貌、性格、缺陷、动机、关系、成长方向等" onChange={(e) => updateCharacter(character.key, "detailed_setting", e.target.value)} /></div>
              </article>
            ))}
            {characters.length === 0 ? <p className="structured-empty">尚未添加人物，至少需要一名主角。</p> : null}
          </div>
        </div>
      </section>

      <section className="brief-section">
        <div className="brief-section-head">
          <span>04</span>
          <div>
            <h3>写作边界</h3>
            <p>控制风格、禁区和自动推进方式。</p>
          </div>
        </div>
        <div className="brief-fields">
          <div className="grid-2">
            <div className="field"><label>风格参考</label><textarea value={form.style_reference} placeholder={briefPlaceholders.style_reference} onChange={(e) => onChange("style_reference", e.target.value)} /></div>
            <div className="field"><label>禁忌内容</label><textarea value={form.forbidden_content} placeholder={briefPlaceholders.forbidden_content} onChange={(e) => onChange("forbidden_content", e.target.value)} /></div>
          </div>
          <div className="field"><label>自动化策略</label><textarea value={form.automation_strategy} placeholder={briefPlaceholders.automation_strategy} onChange={(e) => onChange("automation_strategy", e.target.value)} /></div>
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
