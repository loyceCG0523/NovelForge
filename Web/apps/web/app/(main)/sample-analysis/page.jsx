"use client";

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import AppShellRegion from "@/components/AppShellRegion";
import EmptyState from "@/components/EmptyState";
import { apiFetch, getStoredUser, isTaskInFlight, subscribeSse } from "@/lib/api";
import useSWR from "swr";
import { useLiveRefresh } from "@/lib/useLiveRefresh";

const defaultForm = {
  sample_title: "",
  source_author: "",
  source_genre: "",
  visibility: "private",
  reuse_policy: "reference_only",
  rights_declared: false
};

const statusLabels = {
  queued: "排队中",
  running: "分析中",
  completed: "已完成",
  active: "已完成",
  failed: "失败",
  removed: "已下架"
};

const annotationStatusLabels = {
  pending: "待社区确认",
  trusted: "可信标注",
  trusted_private: "私人标注",
  quarantined: "已隔离"
};

const annotationDraftStatusLabels = {
  idle: "待继续",
  generating: "解析中",
  completed: "已完成",
  failed: "失败"
};

const DAILY_ANNOTATION_NOTICE = "标注已保存。公共标注经社区确认后才会进入生成检索。";
const DAILY_ANNOTATION_NOTICE_KEY = "novelforge:sample-annotation-notice-date";
const CHAPTER_SCROLL_TTL_MS = 10 * 60 * 1000;
const CHAPTER_CACHE_LIMIT = 24;

function localDateKey() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

function claimDailyAnnotationNotice() {
  try {
    const today = localDateKey();
    const userKey = `${DAILY_ANNOTATION_NOTICE_KEY}:${getStoredUser()?.id || "anonymous"}`;
    if (window.localStorage.getItem(userKey) === today) return false;
    window.localStorage.setItem(userKey, today);
    return true;
  } catch {
    return true;
  }
}

const AnnotatedChapterText = memo(function AnnotatedChapterText({ content, annotations }) {
  const source = String(content || "");
  const ranges = (annotations || [])
    .map((annotation) => ({
      start: Math.max(0, Math.min(source.length, Number(annotation.start_offset) || 0)),
      end: Math.max(0, Math.min(source.length, Number(annotation.end_offset) || 0))
    }))
    .filter((range) => range.end > range.start)
    .sort((left, right) => left.start - right.start || left.end - right.end)
    .reduce((merged, range) => {
      const previous = merged.at(-1);
      if (previous && range.start <= previous.end) {
        previous.end = Math.max(previous.end, range.end);
      } else {
        merged.push({ ...range });
      }
      return merged;
    }, []);

  if (!ranges.length) return source;
  const parts = [];
  let cursor = 0;
  ranges.forEach((range, index) => {
    if (range.start > cursor) parts.push(source.slice(cursor, range.start));
    parts.push(<mark className="collab-annotated-text" key={`${range.start}-${range.end}-${index}`}>{source.slice(range.start, range.end)}</mark>);
    cursor = range.end;
  });
  if (cursor < source.length) parts.push(source.slice(cursor));
  return parts;
});

const ChapterNavItem = memo(function ChapterNavItem({ item, active, onSelect }) {
  return (
    <button
      type="button"
      className={`${active ? "active" : ""} ${item.annotation_count ? "annotated" : ""}`}
      onClick={() => onSelect(item.sequence_no)}
    >
      <span>{item.title}</span>
      <small>{item.annotation_count ? `已标注 · ${item.annotation_count} 条` : "未标注"}</small>
    </button>
  );
});

const ChapterNavigator = memo(function ChapterNavigator({ items, activeId, onSelect }) {
  return (
    <aside className="collab-chapter-nav">
      <div className="collab-focus-pane-head"><strong>章节目录</strong><span>{items.length} 章</span></div>
      <div className="collab-chapter-list">
        {items.map((item) => (
          <ChapterNavItem
            key={item.id}
            item={item}
            active={activeId === item.id}
            onSelect={onSelect}
          />
        ))}
      </div>
    </aside>
  );
});

function formatBytes(value) {
  const number = Number(value);
  if (!number) return "-";
  if (number >= 1024 * 1024) return `${(number / 1024 / 1024).toFixed(2)} MB`;
  if (number >= 1024) return `${(number / 1024).toFixed(1)} KB`;
  return `${number} B`;
}

function filenameToTitle(filename) {
  return String(filename || "").replace(/\.[^.]+$/, "").trim();
}

function uniqueById(items) {
  const byId = new Map();
  (items || []).forEach((item) => {
    if (item?.id) byId.set(item.id, item);
  });
  return [...byId.values()];
}

function normalizeAnnotations(items) {
  return uniqueById(items).sort(
    (left, right) => Number(left.start_offset || 0) - Number(right.start_offset || 0)
  );
}

function readLruCache(cache, key) {
  const value = cache.get(key);
  if (value === undefined) return undefined;
  cache.delete(key);
  cache.set(key, value);
  return value;
}

function writeLruCache(cache, key, value) {
  cache.delete(key);
  cache.set(key, value);
  while (cache.size > CHAPTER_CACHE_LIMIT) {
    cache.delete(cache.keys().next().value);
  }
}

function profileOf(work) {
  return work?.report?.reference_profile || {};
}

function AnalysisSummary({ work }) {
  const profile = profileOf(work);
  if (!profile.available) {
    return (
      <div className="sample-pending-box">
        <strong>{work.summary || "总体分析尚未完成"}</strong>
        <span>{profile.reason || (work.status === "failed" ? work.error_message : "Worker 完成后会显示分析结果。")}</span>
      </div>
    );
  }
  return (
    <div className="collab-analysis-grid">
      <section className="sample-section-card">
        <h2>总体评价</h2>
        <p>{profile.overall_evaluation}</p>
      </section>
      <section className="sample-section-card">
        <h2>语言表达原则</h2>
        <ul className="sample-guideline-list">
          {(profile.language_principles || []).map((item) => <li key={item}>{item}</li>)}
        </ul>
      </section>
      <section className="sample-section-card">
        <h2>应避免的错误</h2>
        <ul className="sample-guideline-list">
          {(profile.avoid_errors || []).map((item) => <li key={item}>{item}</li>)}
        </ul>
      </section>
    </div>
  );
}

function SampleAnalysisContent() {
  const fileInputRef = useRef(null);
  const readerRef = useRef(null);
  const chapterScrollPositionsRef = useRef(new Map());
  const chapterContentCacheRef = useRef(new Map());
  const chapterAnnotationsCacheRef = useRef(new Map());
  const chapterPrefetchesRef = useRef(new Map());
  const chapterLoadRequestRef = useRef(null);
  const chapterLoadSequenceRef = useRef(0);
  const chapterSelectionHandlerRef = useRef(null);
  const activeChapterIdRef = useRef(null);
  const annotationDraftSessionsRef = useRef(new Map());
  const annotationDraftRequestsRef = useRef(new Map());
  const annotationDraftRequestSequenceRef = useRef(0);
  const activeAnnotationDraftKeyRef = useRef("");
  const [scope, setScope] = useState("public");
  const [submittedQuery, setSubmittedQuery] = useState("");
  // 样本列表与分类目录走 SWR 缓存：切回页面秒显缓存，后台静默刷新。
  const worksKey = scope === "public"
    ? `/api/sample-collaboration/works/public?q=${encodeURIComponent(submittedQuery)}`
    : "/api/sample-analyses";
  const { data: rawWorks = [], mutate: mutateWorks } = useSWR(worksKey);
  const { data: categories = [] } = useSWR("/api/sample-collaboration/categories");
  const works = useMemo(() => uniqueById(rawWorks), [rawWorks]);
  const [selectedWorkId, setSelectedWorkId] = useState("");
  // 选中样本的详情与章节目录走 SWR 缓存：重复切换卡片秒显，后台静默刷新。
  const { data: selectedWork = null, mutate: mutateWorkDetail } = useSWR(
    selectedWorkId ? `/api/sample-collaboration/works/${selectedWorkId}` : null,
    { keepPreviousData: false }
  );
  const { data: rawChapterIndex = [], mutate: mutateChapterIndex, isLoading: chapterIndexLoading } = useSWR(
    selectedWorkId ? `/api/sample-collaboration/works/${selectedWorkId}/chapters/index` : null,
    { keepPreviousData: false }
  );
  const chapterIndex = useMemo(() => uniqueById(rawChapterIndex), [rawChapterIndex]);
  const [chapter, setChapter] = useState(null);
  const [annotations, setAnnotations] = useState([]);
  const [chapterLoading, setChapterLoading] = useState(false);
  const [focusMode, setFocusMode] = useState(false);
  const [navigatorCollapsed, setNavigatorCollapsed] = useState(false);
  const [dockCollapsed, setDockCollapsed] = useState(false);
  const [dockTab, setDockTab] = useState("annotations");
  const [selection, setSelection] = useState(null);
  const [selectedCategories, setSelectedCategories] = useState([]);
  const [annotationNote, setAnnotationNote] = useState("");
  const [annotationNoteMeta, setAnnotationNoteMeta] = useState(null);
  const [generatingAnnotationNote, setGeneratingAnnotationNote] = useState(false);
  const [batchExplainingChapterIds, setBatchExplainingChapterIds] = useState([]);
  const [annotationDraftSessionRevision, setAnnotationDraftSessionRevision] = useState(0);
  const [editingAnnotation, setEditingAnnotation] = useState(null);
  const [form, setForm] = useState(defaultForm);
  const [selectedFile, setSelectedFile] = useState(null);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const selectChapter = useCallback((sequenceNo) => {
    chapterSelectionHandlerRef.current?.(sequenceNo);
  }, []);

  const chapterPosition = useMemo(
    () => chapterIndex.findIndex((item) => item.sequence_no === chapter?.sequence_no),
    [chapterIndex, chapter?.sequence_no]
  );
  const hasLegacyUnits = chapterIndex.some((item) => item.unit_type === "legacy_segment");
  const hasActiveWorks = useMemo(
    () => works.some((item) => isTaskInFlight(item.status)),
    [works]
  );
  const visibleAnnotations = useMemo(
    () => normalizeAnnotations(annotations),
    [annotations]
  );
  const readerAnnotations = useMemo(() => {
    if (!editingAnnotation?.id || !selection) return visibleAnnotations;
    return normalizeAnnotations([
      ...visibleAnnotations.filter((annotation) => annotation.id !== editingAnnotation.id),
      {
        ...editingAnnotation,
        start_offset: selection.start_offset,
        end_offset: selection.end_offset,
        quote_text: selection.quote_text
      }
    ]);
  }, [visibleAnnotations, editingAnnotation, selection]);
  const editableAnnotations = useMemo(
    () => visibleAnnotations.filter((annotation) => annotation.can_edit),
    [visibleAnnotations]
  );
  const missingNoteCount = useMemo(
    () => editableAnnotations.filter((annotation) => !String(annotation.note || "").trim()).length,
    [editableAnnotations]
  );
  const batchExplainingCurrentChapter = Boolean(
    chapter?.id && batchExplainingChapterIds.includes(chapter.id)
  );
  const backgroundAnnotationDrafts = useMemo(
    () => [...annotationDraftSessionsRef.current.values()]
      .filter((draft) => (
        draft.chapterId === chapter?.id
        && draft.key !== activeAnnotationDraftKeyRef.current
        && (
          draft.status === "generating"
          || draft.status === "completed"
          || draft.status === "failed"
          || draft.categories.length
          || String(draft.note || "").trim()
        )
      ))
      .sort((left, right) => right.updatedAt - left.updatedAt),
    [annotationDraftSessionRevision, chapter?.id]
  );
  activeChapterIdRef.current = chapter?.id || null;

  useEffect(() => {
    // 目录加载完成且为空时提示重新分析（区分“加载中”与“确实为空”）。
    if (selectedWorkId && selectedWork?.status === "completed" && !chapterIndexLoading && !chapterIndex.length) {
      setMessage("章节目录尚未生成，可点击“重新分析并章节化”。");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedWorkId, selectedWork?.status, chapterIndexLoading, chapterIndex.length]);

  function prefetchChapter(workId, target) {
    if (!target?.id || chapterPrefetchesRef.current.has(target.id)) return;
    const cachedChapter = readLruCache(chapterContentCacheRef.current, target.id);
    const cachedAnnotations = readLruCache(chapterAnnotationsCacheRef.current, target.id);
    if (cachedChapter && cachedAnnotations !== undefined) return;

    const request = Promise.all([
      cachedChapter
        ? Promise.resolve(cachedChapter)
        : apiFetch(`/api/sample-collaboration/works/${workId}/chapters?sequence_no=${target.sequence_no}`)
          .then((rows) => rows[0] || null),
      cachedAnnotations !== undefined
        ? Promise.resolve(cachedAnnotations)
        : apiFetch(`/api/sample-collaboration/works/${workId}/annotations?segment_id=${target.id}`)
          .then(normalizeAnnotations)
    ])
      .then(([nextChapter, nextAnnotations]) => {
        if (nextChapter) writeLruCache(chapterContentCacheRef.current, target.id, nextChapter);
        writeLruCache(chapterAnnotationsCacheRef.current, target.id, nextAnnotations);
      })
      .catch(() => {})
      .finally(() => chapterPrefetchesRef.current.delete(target.id));
    chapterPrefetchesRef.current.set(target.id, request);
  }

  async function loadChapter(workId, sequenceNo) {
    rememberCurrentChapterScroll();
    detachActiveAnnotationDraft();
    const normalizedSequence = Math.max(1, Number(sequenceNo) || 1);
    const target = chapterIndex.find((item) => item.sequence_no === normalizedSequence);
    if (!target) return null;

    chapterLoadRequestRef.current?.abort();
    const controller = new AbortController();
    const requestSequence = chapterLoadSequenceRef.current + 1;
    chapterLoadSequenceRef.current = requestSequence;
    chapterLoadRequestRef.current = controller;

    const cachedChapter = readLruCache(chapterContentCacheRef.current, target.id);
    const cachedAnnotations = readLruCache(chapterAnnotationsCacheRef.current, target.id);
    setChapter(cachedChapter || {
      ...target,
      start_offset: 0,
      end_offset: 0,
      content: null,
      content_hash: ""
    });
    setAnnotations(cachedAnnotations || []);
    setChapterLoading(!cachedChapter);

    try {
      // 目录已经给出了 segment id，因此原文和标注可以并行读取。
      const [next, nextAnnotations] = await Promise.all([
        cachedChapter
          ? Promise.resolve(cachedChapter)
          : apiFetch(
            `/api/sample-collaboration/works/${workId}/chapters?sequence_no=${normalizedSequence}`,
            { signal: controller.signal }
          ).then((rows) => rows[0] || null),
        apiFetch(
          `/api/sample-collaboration/works/${workId}/annotations?segment_id=${target.id}`,
          { signal: controller.signal }
        ).then(normalizeAnnotations)
      ]);
      if (controller.signal.aborted || chapterLoadSequenceRef.current !== requestSequence) return null;

      if (next) writeLruCache(chapterContentCacheRef.current, target.id, next);
      writeLruCache(chapterAnnotationsCacheRef.current, target.id, nextAnnotations);
      setChapter(next);
      setAnnotations(nextAnnotations);
      setChapterLoading(false);

      const position = chapterIndex.findIndex((item) => item.id === target.id);
      if (position > 0) prefetchChapter(workId, chapterIndex[position - 1]);
      if (position >= 0 && position < chapterIndex.length - 1) {
        prefetchChapter(workId, chapterIndex[position + 1]);
      }
      return next;
    } catch (err) {
      if (controller.signal.aborted || err?.name === "AbortError") return null;
      if (chapterLoadSequenceRef.current === requestSequence) setChapterLoading(false);
      throw err;
    } finally {
      if (chapterLoadRequestRef.current === controller) chapterLoadRequestRef.current = null;
    }
  }

  chapterSelectionHandlerRef.current = (sequenceNo) => {
    if (!selectedWorkId) return;
    loadChapter(selectedWorkId, sequenceNo).catch((err) => setError(err.message));
  };

  function rememberCurrentChapterScroll() {
    if (!chapter?.id || !readerRef.current) return;
    chapterScrollPositionsRef.current.set(chapter.id, {
      scrollTop: readerRef.current.scrollTop,
      savedAt: Date.now()
    });
  }

  async function openWork(work) {
    setError("");
    setMessage("");
    chapterLoadRequestRef.current?.abort();
    chapterContentCacheRef.current.clear();
    chapterAnnotationsCacheRef.current.clear();
    chapterPrefetchesRef.current.clear();
    setChapter(null);
    setAnnotations([]);
    setChapterLoading(false);
    // 只记录 id；详情与目录由 SWR 按 key 供给，访问过的样本立即命中缓存。
    setSelectedWorkId(work.id);
  }

  async function enterFocusMode() {
    if (!selectedWork || !chapterIndex.length) return;
    const sequenceNo = chapter?.sequence_no || chapterIndex[0].sequence_no;
    if (!chapter || chapter.sequence_no !== sequenceNo) {
      await loadChapter(selectedWorkId, sequenceNo);
    }
    setDockTab("annotations");
    setFocusMode(true);
  }

  function leaveFocusMode() {
    rememberCurrentChapterScroll();
    setFocusMode(false);
    detachActiveAnnotationDraft();
  }

  function updateForm(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function handleFile(event) {
    const file = event.target.files?.[0] || null;
    setSelectedFile(file);
    if (file) {
      setForm((current) => ({
        ...current,
        sample_title: current.sample_title || filenameToTitle(file.name)
      }));
    }
  }

  async function createWork(event) {
    event.preventDefault();
    if (!selectedFile) return;
    setLoading(true);
    setError("");
    setMessage("");
    try {
      const body = new FormData();
      Object.entries(form).forEach(([key, value]) => body.append(key, String(value)));
      body.append("file", selectedFile);
      const created = await apiFetch("/api/sample-analyses", { method: "POST", body });
      setForm(defaultForm);
      setSelectedFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      setScope("mine");
      await mutateWorks();
      setMessage(`《${created.sample_title}》已上传，正在识别章节并生成总体分析。`);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function reanalyze(work) {
    setError("");
    chapterLoadRequestRef.current?.abort();
    chapterContentCacheRef.current.clear();
    chapterAnnotationsCacheRef.current.clear();
    const updated = await apiFetch(`/api/sample-analyses/${work.id}/reindex`, { method: "POST" });
    mutateWorkDetail((current) => current?.id === updated.id ? { ...current, ...updated } : current, { revalidate: false });
    mutateWorks((current = []) => current.map((item) => item.id === updated.id ? { ...item, ...updated } : item), { revalidate: false });
    setMessage("已提交总体分析与章节化任务，原有人工标注会安全迁移，不会被覆盖。");
  }

  async function togglePublication(work) {
    const makePublic = work.visibility !== "public";
    if (makePublic && !window.confirm("确认你拥有该文本的分享权利，并将它发布到公共样本空间？")) return;
    const updated = await apiFetch(
      `/api/sample-collaboration/works/${work.id}/publication`,
      {
        method: "PATCH",
        body: JSON.stringify({
          visibility: makePublic ? "public" : "private",
          reuse_policy: work.reuse_policy || "reference_only",
          rights_declared: makePublic,
          version: work.version
        })
      }
    );
    mutateWorks((current = []) => current.map((item) => item.id === work.id ? { ...item, ...updated } : item), { revalidate: false });
    if (selectedWork?.id === work.id) mutateWorkDetail(updated, { revalidate: false });
    setMessage(makePublic ? "样本已发布到公共空间。" : "样本已转为私人可见。");
  }

  async function deleteWork(work) {
    await apiFetch(`/api/sample-analyses/${work.id}`, { method: "DELETE" });
    mutateWorks((current = []) => current.filter((item) => item.id !== work.id), { revalidate: false });
    if (selectedWork?.id === work.id) {
      setSelectedWorkId("");
      setChapter(null);
      setAnnotations([]);
    }
    setMessage(work.visibility === "public" ? "公共样本已下架。" : "私人样本已删除。");
  }

  function captureSelection() {
    if (!readerRef.current) return;
    const browserSelection = window.getSelection();
    if (!browserSelection || browserSelection.rangeCount === 0 || browserSelection.isCollapsed) return;
    const range = browserSelection.getRangeAt(0);
    if (!readerRef.current.contains(range.commonAncestorContainer)) return;
    const prefix = range.cloneRange();
    prefix.selectNodeContents(readerRef.current);
    prefix.setEnd(range.startContainer, range.startOffset);
    const text = browserSelection.toString();
    if (!text.trim()) return;
    persistActiveAnnotationDraft();
    const nextSelection = {
      start_offset: prefix.toString().length,
      end_offset: prefix.toString().length + text.length,
      quote_text: text
    };
    if (editingAnnotation) {
      const draftKey = activeAnnotationDraftKeyRef.current;
      cancelAnnotationDraftRequest(draftKey);
      setSelection(nextSelection);
      setAnnotationNoteMeta(null);
      updateAnnotationDraftSession(draftKey, {
        selection: nextSelection,
        meta: null,
        status: "idle",
        error: ""
      });
      setError("");
      setDockCollapsed(false);
      setDockTab("annotate");
      return;
    }
    const key = annotationDraftKey(chapter.id, nextSelection);
    let draft = annotationDraftSessionsRef.current.get(key);
    if (!draft) {
      draft = {
        key,
        workId: selectedWorkId,
        chapterId: chapter.id,
        selection: nextSelection,
        categories: [],
        note: "",
        meta: null,
        status: "idle",
        error: "",
        editingAnnotation: null,
        updatedAt: Date.now()
      };
      annotationDraftSessionsRef.current.set(key, draft);
      bumpAnnotationDraftSessions();
    }
    activateAnnotationDraft(draft);
    setDockCollapsed(false);
    setDockTab("annotate");
  }

  function toggleCategory(key) {
    cancelAnnotationDraftRequest(activeAnnotationDraftKeyRef.current);
    setAnnotationNoteMeta(null);
    setSelectedCategories((current) => {
      const next = current.includes(key)
        ? current.filter((item) => item !== key)
        : current.length < 5 ? [...current, key] : current;
      updateAnnotationDraftSession(activeAnnotationDraftKeyRef.current, {
        categories: next,
        meta: null,
        status: "idle",
        error: ""
      });
      return next;
    });
  }

  function beginEdit(annotation) {
    persistActiveAnnotationDraft();
    const key = `edit:${annotation.id}`;
    const existingDraft = annotationDraftSessionsRef.current.get(key);
    const draft = existingDraft ? { ...existingDraft, editingAnnotation: annotation } : {
      key,
      workId: selectedWorkId,
      chapterId: chapter.id,
      selection: {
        start_offset: annotation.start_offset,
        end_offset: annotation.end_offset,
        quote_text: annotation.quote_text
      },
      categories: annotation.categories || [],
      note: annotation.note || "",
      meta: null,
      status: "idle",
      error: "",
      editingAnnotation: annotation,
      updatedAt: Date.now()
    };
    annotationDraftSessionsRef.current.set(key, draft);
    bumpAnnotationDraftSessions();
    activateAnnotationDraft(draft);
    setDockCollapsed(false);
    setDockTab("annotate");
  }

  function resetAnnotationForm() {
    const activeKey = activeAnnotationDraftKeyRef.current;
    cancelAnnotationDraftRequest(activeKey);
    if (activeKey) annotationDraftSessionsRef.current.delete(activeKey);
    activeAnnotationDraftKeyRef.current = "";
    bumpAnnotationDraftSessions();
    clearAnnotationFormFields();
    window.getSelection()?.removeAllRanges();
  }

  function detachActiveAnnotationDraft() {
    persistActiveAnnotationDraft();
    activeAnnotationDraftKeyRef.current = "";
    bumpAnnotationDraftSessions();
    clearAnnotationFormFields();
    window.getSelection()?.removeAllRanges();
  }

  function clearAnnotationFormFields() {
    setEditingAnnotation(null);
    setSelection(null);
    setSelectedCategories([]);
    setAnnotationNote("");
    setAnnotationNoteMeta(null);
    setGeneratingAnnotationNote(false);
  }

  function updateAnnotationNote(value) {
    const activeKey = activeAnnotationDraftKeyRef.current;
    const hadActiveRequest = annotationDraftRequestsRef.current.has(activeKey);
    if (hadActiveRequest) {
      cancelAnnotationDraftRequest(activeKey);
      setAnnotationNoteMeta(null);
    }
    setAnnotationNote(value);
    updateAnnotationDraftSession(activeKey, {
      note: value,
      meta: hadActiveRequest ? null : annotationNoteMeta,
      status: !hadActiveRequest && annotationNoteMeta ? "completed" : "idle",
      error: ""
    });
  }

  function annotationDraftKey(chapterId, draftSelection) {
    return `${chapterId}:${draftSelection.start_offset}:${draftSelection.end_offset}`;
  }

  function bumpAnnotationDraftSessions() {
    setAnnotationDraftSessionRevision((current) => current + 1);
  }

  function updateAnnotationDraftSession(key, changes) {
    if (!key) return;
    const current = annotationDraftSessionsRef.current.get(key);
    if (!current) return;
    annotationDraftSessionsRef.current.set(key, {
      ...current,
      ...changes,
      updatedAt: Date.now()
    });
    bumpAnnotationDraftSessions();
  }

  function persistActiveAnnotationDraft() {
    const key = activeAnnotationDraftKeyRef.current;
    if (!key || !selection) return;
    const current = annotationDraftSessionsRef.current.get(key);
    if (!current) return;
    annotationDraftSessionsRef.current.set(key, {
      ...current,
      selection: { ...selection },
      categories: [...selectedCategories],
      note: annotationNote,
      meta: annotationNoteMeta,
      editingAnnotation,
      status: annotationDraftRequestsRef.current.has(key) ? "generating" : current.status,
      updatedAt: Date.now()
    });
    bumpAnnotationDraftSessions();
  }

  function activateAnnotationDraft(draft) {
    activeAnnotationDraftKeyRef.current = draft.key;
    setEditingAnnotation(draft.editingAnnotation || null);
    setSelection({ ...draft.selection });
    setSelectedCategories([...(draft.categories || [])]);
    setAnnotationNote(draft.note || "");
    setAnnotationNoteMeta(draft.meta || null);
    setGeneratingAnnotationNote(annotationDraftRequestsRef.current.has(draft.key));
    setError(draft.error || "");
    bumpAnnotationDraftSessions();
  }

  function switchToAnnotationDraft(draft) {
    persistActiveAnnotationDraft();
    activateAnnotationDraft(draft);
    setDockCollapsed(false);
    setDockTab("annotate");
  }

  function cancelAnnotationDraftRequest(key) {
    if (!key) return;
    const request = annotationDraftRequestsRef.current.get(key);
    if (request) {
      request.controller.abort();
      annotationDraftRequestsRef.current.delete(key);
    }
    const draft = annotationDraftSessionsRef.current.get(key);
    if (draft?.status === "generating") {
      updateAnnotationDraftSession(key, { status: "idle", error: "" });
    }
    if (activeAnnotationDraftKeyRef.current === key) {
      setGeneratingAnnotationNote(false);
    }
  }

  async function submitAnnotation(event) {
    event.preventDefault();
    if (!selection || !selectedCategories.length || !chapter || !selectedWork) return;
    setLoading(true);
    setError("");
    try {
      let saved;
      if (editingAnnotation) {
        saved = await apiFetch(`/api/sample-collaboration/annotations/${editingAnnotation.id}`, {
          method: "PATCH",
          body: JSON.stringify({
            version: editingAnnotation.version,
            ...selection,
            categories: selectedCategories,
            note: annotationNote
          })
        });
        const selectionWasSaved = (
          Number(saved.start_offset) === Number(selection.start_offset)
          && Number(saved.end_offset) === Number(selection.end_offset)
          && saved.quote_text === selection.quote_text
        );
        if (!selectionWasSaved) {
          const nextEditingAnnotation = {
            ...editingAnnotation,
            version: saved.version ?? editingAnnotation.version
          };
          setEditingAnnotation(nextEditingAnnotation);
          updateAnnotationDraftSession(activeAnnotationDraftKeyRef.current, {
            editingAnnotation: nextEditingAnnotation
          });
          throw new Error("后端未保存新的选区，请重启 API 后再试；当前编辑内容仍为你保留。");
        }
        setAnnotations((current) => normalizeAnnotations([
          ...current.filter((item) => item.id !== saved.id),
          saved
        ]));
      } else {
        saved = await apiFetch(`/api/sample-collaboration/works/${selectedWork.id}/annotations`, {
          method: "POST",
          body: JSON.stringify({
            segment_id: chapter.id,
            ...selection,
            categories: selectedCategories,
            note: annotationNote
          })
        });
        setAnnotations((current) => normalizeAnnotations([...current, saved]));
        mutateChapterIndex().catch((err) => setError(err.message));
      }
      resetAnnotationForm();
      setDockTab("annotations");
      setMessage(
        !editingAnnotation
        && selectedWork.visibility === "public"
        && claimDailyAnnotationNotice()
          ? DAILY_ANNOTATION_NOTICE
          : ""
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function generateAnnotationNote() {
    if (!selection || !selectedCategories.length || !chapter || !selectedWork) return;
    const draftKey = activeAnnotationDraftKeyRef.current;
    if (!draftKey) return;
    const requestId = annotationDraftRequestSequenceRef.current + 1;
    annotationDraftRequestSequenceRef.current = requestId;
    const controller = new AbortController();
    cancelAnnotationDraftRequest(draftKey);
    annotationDraftRequestsRef.current.set(draftKey, { requestId, controller });
    const requestedSelection = { ...selection };
    const requestedCategories = [...selectedCategories];
    const requestedChapterId = chapter.id;
    const requestedWorkId = selectedWorkId;
    updateAnnotationDraftSession(draftKey, {
      selection: requestedSelection,
      categories: requestedCategories,
      note: annotationNote,
      meta: null,
      status: "generating",
      error: ""
    });
    setGeneratingAnnotationNote(true);
    setAnnotationNoteMeta(null);
    setError("");
    try {
      const draft = await apiFetch(
        `/api/sample-collaboration/works/${requestedWorkId}/annotation-note-draft`,
        {
          method: "POST",
          signal: controller.signal,
          body: JSON.stringify({
            segment_id: requestedChapterId,
            ...requestedSelection,
            categories: requestedCategories
          })
        }
      );
      if (annotationDraftRequestsRef.current.get(draftKey)?.requestId !== requestId) return;
      updateAnnotationDraftSession(draftKey, {
        note: draft.note || "",
        meta: draft,
        status: "completed",
        error: ""
      });
      if (activeAnnotationDraftKeyRef.current === draftKey) {
        setAnnotationNote(draft.note || "");
        setAnnotationNoteMeta(draft);
      }
    } catch (err) {
      if (controller.signal.aborted || annotationDraftRequestsRef.current.get(draftKey)?.requestId !== requestId) return;
      updateAnnotationDraftSession(draftKey, {
        status: "failed",
        error: err.message
      });
      if (activeAnnotationDraftKeyRef.current === draftKey) setError(err.message);
    } finally {
      if (annotationDraftRequestsRef.current.get(draftKey)?.requestId === requestId) {
        annotationDraftRequestsRef.current.delete(draftKey);
        bumpAnnotationDraftSessions();
        if (activeAnnotationDraftKeyRef.current === draftKey) {
          setGeneratingAnnotationNote(false);
        }
      }
    }
  }

  async function batchGenerateAnnotationNotes() {
    if (!chapter?.id || !selectedWork?.id || !missingNoteCount || editingAnnotation) return;
    const requestedChapterId = chapter.id;
    const requestedWorkId = selectedWorkId;
    setBatchExplainingChapterIds((current) => [...new Set([...current, requestedChapterId])]);
    setError("");
    setMessage("");
    try {
      const result = await apiFetch(
        `/api/sample-collaboration/works/${requestedWorkId}/chapters/${requestedChapterId}/annotation-notes/batch`,
        { method: "POST" }
      );
      if (activeChapterIdRef.current === requestedChapterId) {
        const updatedById = new Map((result.annotations || []).map((item) => [item.id, item]));
        setAnnotations((current) => normalizeAnnotations(
          current.map((item) => updatedById.get(item.id) || item)
        ));
      }
      const summary = `已并行生成 ${result.generated_count} 条说明`;
      const details = [
        result.skipped_existing_count ? `保留 ${result.skipped_existing_count} 条已有说明` : "",
        result.skipped_changed_count ? `跳过 ${result.skipped_changed_count} 条刚被修改的标注` : "",
        result.remaining_count ? `还有 ${result.remaining_count} 条可继续处理` : ""
      ].filter(Boolean).join("，");
      if (result.failed_count) {
        const firstFailure = result.failures?.[0]?.message || "模型调用失败";
        setError(`${summary}，${result.failed_count} 条失败：${firstFailure}${details ? `；${details}` : ""}`);
      } else {
        setMessage(`${summary}${details ? `，${details}` : ""}。`);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBatchExplainingChapterIds((current) => current.filter((id) => id !== requestedChapterId));
    }
  }

  async function vote(annotation, value) {
    try {
      const nextValue = annotation.current_user_vote === value ? 0 : value;
      const updated = await apiFetch(`/api/sample-collaboration/annotations/${annotation.id}/vote`, {
        method: "PUT",
        body: JSON.stringify({ value: nextValue })
      });
      setAnnotations((current) => normalizeAnnotations([
        ...current.filter((item) => item.id !== updated.id),
        updated
      ]));
    } catch (err) {
      setError(err.message);
    }
  }

  async function removeAnnotation(annotation) {
    await apiFetch(`/api/sample-collaboration/annotations/${annotation.id}`, { method: "DELETE" });
    setAnnotations((current) => current.filter((item) => item.id !== annotation.id));
    if (selectedWorkId) mutateChapterIndex().catch((err) => setError(err.message));
  }

  async function reportAnnotation(annotation) {
    try {
      await apiFetch(`/api/sample-collaboration/annotations/${annotation.id}/reports`, {
        method: "POST",
        body: JSON.stringify({ reason: "meaningless", detail: "该标注可能没有有效信息。" })
      });
      setMessage("举报已提交，达到隔离条件后该标注会退出生成检索。");
    } catch (err) {
      setError(err.message);
    }
  }

  async function switchScope(nextScope) {
    chapterLoadRequestRef.current?.abort();
    chapterContentCacheRef.current.clear();
    chapterAnnotationsCacheRef.current.clear();
    chapterPrefetchesRef.current.clear();
    setScope(nextScope);
    setSelectedWorkId("");
    setChapter(null);
    setAnnotations([]);
    setChapterLoading(false);
    setFocusMode(false);
    setSubmittedQuery(nextScope === "public" ? query.trim() : "");
  }

  useEffect(() => () => {
    chapterLoadRequestRef.current?.abort();
    annotationDraftRequestsRef.current.forEach((request) => request.controller.abort());
    annotationDraftRequestsRef.current.clear();
  }, []);

  useEffect(() => {
    if (!chapter?.id || chapter.content == null) return;
    writeLruCache(chapterContentCacheRef.current, chapter.id, chapter);
    writeLruCache(
      chapterAnnotationsCacheRef.current,
      chapter.id,
      normalizeAnnotations(annotations)
    );
  }, [chapter, annotations]);

  useLiveRefresh({
    enabled: scope === "mine" && hasActiveWorks,
    intervalMs: 2500,
    refresh: async () => {
      const data = await mutateWorks();
      if (!selectedWorkId) return;
      const current = data.find((item) => item.id === selectedWorkId);
      if (!current) return;
      const detail = await mutateWorkDetail();
      if (!isTaskInFlight(current.status)) {
        await mutateChapterIndex();
      }
    },
    onError: (err) => setError(err.message)
  });

  useEffect(() => {
    if (!focusMode) return undefined;
    document.body.classList.add("sample-annotation-focus-open");
    return () => document.body.classList.remove("sample-annotation-focus-open");
  }, [focusMode]);

  useEffect(() => {
    if (!chapter?.id || !readerRef.current) return undefined;
    const reader = readerRef.current;
    const saved = chapterScrollPositionsRef.current.get(chapter.id);
    const canRestore = saved && Date.now() - saved.savedAt <= CHAPTER_SCROLL_TTL_MS;
    if (saved && !canRestore) chapterScrollPositionsRef.current.delete(chapter.id);
    const frame = window.requestAnimationFrame(() => {
      reader.scrollTo({ top: canRestore ? saved.scrollTop : 0, behavior: "auto" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [chapter?.id, chapterLoading]);

  useEffect(() => {
    if (message !== DAILY_ANNOTATION_NOTICE) return undefined;
    const timer = window.setTimeout(() => setMessage(""), 6000);
    return () => window.clearTimeout(timer);
  }, [message]);

  useEffect(() => {
    if (!selectedWorkId) return undefined;
    return subscribeSse(
      `/api/sample-collaboration/works/${selectedWorkId}/events`,
      () => {
        mutateWorkDetail().catch(() => {});
        mutateChapterIndex().catch(() => {});
        if (!chapter?.id) return;
        apiFetch(
          `/api/sample-collaboration/works/${selectedWorkId}/annotations?segment_id=${chapter.id}`
        ).then((rows) => {
          const normalized = normalizeAnnotations(rows);
          writeLruCache(chapterAnnotationsCacheRef.current, chapter.id, normalized);
          setAnnotations(normalized);
        }).catch(() => {});
      },
      () => {}
    );
  }, [selectedWorkId, chapter?.id]);

  return (
    <>
    <AppShellRegion title="样本协作" actions={<span className="tag purple">人工标注 · 可信后使用</span>} />
      {(message || error) ? <div className={error ? "error-box" : "hint-panel"}>{error || message}</div> : null}

      <section className="collab-library-toolbar">
        <div className="collab-tabs" role="tablist" aria-label="样本空间">
          <button className={scope === "public" ? "active" : ""} type="button" onClick={() => switchScope("public").catch((err) => setError(err.message))}>公共样本</button>
          <button className={scope === "mine" ? "active" : ""} type="button" onClick={() => switchScope("mine").catch((err) => setError(err.message))}>我的样本</button>
        </div>
        {scope === "public" ? (
          <form className="collab-search" onSubmit={(event) => { event.preventDefault(); const next = query.trim(); if (next === submittedQuery) { mutateWorks().catch((err) => setError(err.message)); } else { setSubmittedQuery(next); } }}>
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索作品名、作者或题材" />
            <button className="secondary-button">搜索</button>
          </form>
        ) : null}
      </section>

      {scope === "mine" ? (
        <details className="panel collab-collapsible-panel">
          <summary>
            <span><strong>上传新样本</strong><small>支持 TXT、MD；默认仅自己可见</small></span>
            <span>展开</span>
          </summary>
          <form className="panel-body collab-upload-form" onSubmit={createWork}>
            <label className="field"><span>作品名</span><input required value={form.sample_title} onChange={(event) => updateForm("sample_title", event.target.value)} /></label>
            <label className="field"><span>作者/来源</span><input value={form.source_author} onChange={(event) => updateForm("source_author", event.target.value)} /></label>
            <label className="field"><span>题材</span><input value={form.source_genre} onChange={(event) => updateForm("source_genre", event.target.value)} /></label>
            <label className="field"><span>可见性</span><select value={form.visibility} onChange={(event) => updateForm("visibility", event.target.value)}><option value="private">仅自己可见</option><option value="public">发布到公共空间</option></select></label>
            <label className="field"><span>复用许可</span><select value={form.reuse_policy} onChange={(event) => updateForm("reuse_policy", event.target.value)}><option value="reference_only">只参考写法</option><option value="excerpt_reuse">允许复用短表达</option></select></label>
            {form.visibility === "public" ? (
              <label className="collab-rights-check"><input type="checkbox" checked={form.rights_declared} onChange={(event) => updateForm("rights_declared", event.target.checked)} /><span>我确认拥有公开分享该文本的权利</span></label>
            ) : null}
            <div className="collab-upload-action-row">
              <div className={`collab-file-picker ${selectedFile ? "has-file" : ""}`}>
                <input ref={fileInputRef} className="collab-file-input" type="file" accept=".txt,.md,.text" onChange={handleFile} />
                <button className="collab-file-cta" type="button" onClick={() => fileInputRef.current?.click()}>{selectedFile ? "更换文件" : "选择文件"}</button>
                <span className="collab-file-meta">
                  <strong>{selectedFile?.name || "尚未选择文件"}</strong>
                  <small>{selectedFile ? `${formatBytes(selectedFile.size)} · TXT / MD` : "支持 TXT、MD 文件"}</small>
                </span>
              </div>
              <button className="primary-button collab-upload-submit" disabled={loading || !selectedFile || (form.visibility === "public" && !form.rights_declared)}>{loading ? "提交中..." : "上传并分析"}</button>
            </div>
          </form>
        </details>
      ) : null}

      <section className="panel collab-library-panel">
        <div className="panel-header">
          <div><div className="panel-title">{scope === "public" ? "公共空间" : "我的样本"}</div><div className="panel-subtitle">{works.length} 部作品，打开后再进入沉浸标注</div></div>
        </div>
        <div className="panel-body">
          {!works.length ? <EmptyState title="暂无样本" description={scope === "public" ? "还没有已发布的公共样本。" : "上传后即可开始私人或协同标注。"} /> : (
            <div className="collab-library-grid">
              {works.map((work) => (
                <article className={`collab-work-card ${selectedWorkId === work.id ? "active" : ""}`} key={work.id}>
                  <button type="button" className="collab-work-open" onClick={() => openWork(work).catch((err) => setError(err.message))}>
                    <span className="collab-card-status">{statusLabels[work.status] || work.status} · {work.visibility === "public" ? "公共" : "私人"}</span>
                    <strong>{work.sample_title}</strong>
                    <span>{work.source_author || "未标注作者"} · {work.source_genre || "未标注题材"}</span>
                    <span>{work.chapter_count ? `${work.chapter_count} 章` : "章节识别中"} · {work.source_word_count ? `${work.source_word_count.toLocaleString()} 字` : formatBytes(work.source_file_size)}</span>
                  </button>
                </article>
              ))}
            </div>
          )}
        </div>
      </section>

      {selectedWork ? (
        <section className="panel collab-work-overview">
          <div className="panel-header collab-overview-header">
            <div>
              <div className="panel-title">{selectedWork.sample_title}</div>
              <div className="panel-subtitle">{chapterIndex.length} 个阅读章节 · {selectedWork.reuse_policy === "excerpt_reuse" ? "允许复用短表达" : "只参考写法"}</div>
            </div>
            <div className="collab-overview-actions">
              {scope === "mine" ? <button className="secondary-button" type="button" onClick={() => reanalyze(selectedWork).catch((err) => setError(err.message))}>{hasLegacyUnits ? "转换为章节" : "重新分析并章节化"}</button> : null}
              {scope === "mine" ? <button className="secondary-button" type="button" onClick={() => togglePublication(selectedWork).catch((err) => setError(err.message))}>{selectedWork.visibility === "public" ? "转为私人" : "发布"}</button> : null}
              {scope === "mine" ? <button className="danger-button" type="button" onClick={() => deleteWork(selectedWork).catch((err) => setError(err.message))}>{selectedWork.visibility === "public" ? "下架" : "删除"}</button> : null}
              <button className="primary-button collab-start-button" type="button" disabled={!chapterIndex.length || selectedWork.status !== "completed"} onClick={() => enterFocusMode().catch((err) => setError(err.message))}>进入沉浸标注</button>
            </div>
          </div>
          {hasLegacyUnits ? <div className="collab-legacy-warning">该样本仍是旧版固定分段。点击“转换为章节”后，系统会重新识别章节并迁移现有标注。</div> : null}
          <details className="collab-analysis-details">
            <summary><span><strong>AI 总体分析</strong><small>总体评价、语言原则和应避免的错误</small></span><span>展开查看</span></summary>
            <div className="panel-body"><AnalysisSummary work={selectedWork} /></div>
          </details>
        </section>
      ) : null}

      {focusMode && selectedWork ? (
        <div className={`collab-focus-workspace ${navigatorCollapsed ? "nav-collapsed" : ""} ${dockCollapsed ? "dock-collapsed" : ""}`} role="dialog" aria-modal="true" aria-label={`${selectedWork.sample_title} 标注工作台`}>
          <header className="collab-focus-header">
            <button className="collab-focus-back" type="button" onClick={leaveFocusMode}>← 退出标注</button>
            <div className="collab-focus-title"><strong>{selectedWork.sample_title}</strong><span>{chapter?.title || "选择章节"}</span></div>
            <div className="collab-focus-chapter-actions">
              <button type="button" disabled={chapterPosition <= 0} onClick={() => selectChapter(chapterIndex[chapterPosition - 1].sequence_no)}>上一章</button>
              <select value={chapter?.sequence_no || ""} onChange={(event) => selectChapter(Number(event.target.value))} aria-label="选择章节">
                {chapterIndex.map((item) => <option key={item.id} value={item.sequence_no}>{item.title}</option>)}
              </select>
              <button type="button" disabled={chapterPosition < 0 || chapterPosition >= chapterIndex.length - 1} onClick={() => selectChapter(chapterIndex[chapterPosition + 1].sequence_no)}>下一章</button>
            </div>
            <div className="collab-focus-layout-actions">
              <button type="button" className={navigatorCollapsed ? "" : "active"} onClick={() => setNavigatorCollapsed((value) => !value)}>目录</button>
              <button type="button" className={dockCollapsed ? "" : "active"} onClick={() => setDockCollapsed((value) => !value)}>标注栏</button>
            </div>
          </header>

          {(message || error) ? <div className={`collab-focus-notice ${error ? "error" : ""}`}>{error || message}<button type="button" onClick={() => { setMessage(""); setError(""); }}>关闭</button></div> : null}

          <div className="collab-focus-body">
            {!navigatorCollapsed ? (
              <ChapterNavigator
                items={chapterIndex}
                activeId={chapter?.id}
                onSelect={selectChapter}
              />
            ) : null}

            <main className="collab-focus-reader" aria-busy={chapterLoading}>
              <div className="collab-reader-heading">
                <div><span>{chapter?.unit_type === "preface" ? "前置内容" : chapter?.unit_type === "fallback" ? "未识别章节" : "章节原文"}</span><h2>{chapter?.title || "原文尚未就绪"}</h2></div>
                <span>{chapterLoading ? "正在切换…" : `${visibleAnnotations.length} 条本章标注`}</span>
              </div>
              {chapter?.content != null ? (
                <div className="collab-reader-text" ref={readerRef} onMouseUp={captureSelection} onKeyUp={captureSelection}><AnnotatedChapterText content={chapter.content} annotations={readerAnnotations} /></div>
              ) : chapterLoading ? (
                <div className="collab-chapter-loading"><span className="collab-loading-spinner" />正在加载章节原文与标注…</div>
              ) : <EmptyState title="章节尚未就绪" description="等待后台完成章节识别后再开始标注。" />}
            </main>

            {!dockCollapsed ? (
              <aside className="collab-focus-dock">
                <div className="collab-dock-tabs" role="tablist" aria-label="标注操作">
                  <button type="button" className={dockTab === "annotate" ? "active" : ""} onClick={() => setDockTab("annotate")}>新建标注{backgroundAnnotationDrafts.length ? ` · 草稿 ${backgroundAnnotationDrafts.length}` : ""}</button>
                  <button type="button" className={dockTab === "annotations" ? "active" : ""} onClick={() => setDockTab("annotations")}>本章标注 {chapterLoading ? "…" : visibleAnnotations.length}</button>
                </div>
                {dockTab === "annotate" ? (
                  <>
                    {backgroundAnnotationDrafts.length ? (
                      <div className="collab-background-drafts">
                        <div><strong>后台标注</strong><small>{backgroundAnnotationDrafts.length} 条</small></div>
                        <div>
                          {backgroundAnnotationDrafts.map((draft) => (
                            <button type="button" key={draft.key} onClick={() => switchToAnnotationDraft(draft)}>
                              <span className={draft.status}>{annotationDraftStatusLabels[draft.status] || "待继续"}</span>
                              <strong>{String(draft.selection?.quote_text || "").replace(/\s+/g, " ").slice(0, 24)}</strong>
                            </button>
                          ))}
                        </div>
                      </div>
                    ) : null}
                    <form className="collab-annotation-form" onSubmit={submitAnnotation}>
                    <div className="field">
                      <span>{editingAnnotation ? "正在修改选区" : "当前选区"}</span>
                      <blockquote>{selection?.quote_text || "在正文中选择句子后，这里会自动出现。"}</blockquote>
                      {editingAnnotation ? <small className="collab-selection-edit-hint">可回到正文重新拖选，分类和说明会保留。</small> : null}
                    </div>
                    <div className="field"><span>分类（可多选，最多 5 个）</span><div className="collab-category-grid">{categories.map((category) => <button type="button" className={selectedCategories.includes(category.key) ? "active" : ""} key={category.key} onClick={() => toggleCategory(category.key)}>{category.label}</button>)}</div></div>
                    <label className="field collab-note-field">
                      <span className="collab-note-field-head">
                        <span>说明</span>
                        <button
                          type="button"
                          className="collab-note-generate"
                          disabled={loading || generatingAnnotationNote || !selection || !selectedCategories.length}
                          onClick={generateAnnotationNote}
                        >
                          {generatingAnnotationNote
                            ? selectedCategories.includes("internet_meme") ? "正在联网解析…" : "正在生成…"
                            : "AI 生成 2—3 句"}
                        </button>
                      </span>
                      <textarea rows={5} maxLength={500} value={annotationNote} onChange={(event) => updateAnnotationNote(event.target.value)} placeholder="可自行填写，也可让模型根据人工标签生成说明草稿。" />
                      <small className="collab-note-hint">
                        {annotationNoteMeta?.used_web_search
                          ? `已用 DeepSeek Web Search 核验，参考 ${annotationNoteMeta.source_count} 条结果；请人工确认后保存。`
                          : annotationNoteMeta
                            ? `已由 ${annotationNoteMeta.model} 生成草稿；请人工确认后保存。`
                            : selectedCategories.includes("internet_meme")
                              ? "网络热梗说明会调用 DeepSeek Web Search；不会改动标签或自动保存。"
                              : "模型只解释当前人工选区和标签，不会自动判断或保存标注。"}
                      </small>
                    </label>
                    <div className="collab-form-actions">
                      <button className="primary-button" disabled={loading || generatingAnnotationNote || !selection || !selectedCategories.length}>{editingAnnotation ? "保存修改" : "保存标注"}</button>
                      {selection ? <button className="secondary-button" type="button" onClick={resetAnnotationForm}>取消</button> : null}
                    </div>
                    </form>
                  </>
                ) : (
                  <div className="collab-annotation-list">
                    {!chapterLoading && visibleAnnotations.length ? (
                      <div className="collab-batch-note-toolbar">
                        <span>
                          <strong>批量补全说明</strong>
                          <small>仅处理我创建且说明为空的标注，已有说明保持不变。</small>
                        </span>
                        <button
                          type="button"
                          disabled={batchExplainingCurrentChapter || !missingNoteCount || Boolean(editingAnnotation)}
                          onClick={batchGenerateAnnotationNotes}
                        >
                          {batchExplainingCurrentChapter
                            ? "并行解释中…"
                            : missingNoteCount
                              ? `批量解释 ${missingNoteCount} 条`
                              : editableAnnotations.length ? "我的说明已补全" : "没有我的标注"}
                        </button>
                      </div>
                    ) : null}
                    {chapterLoading ? (
                      <div className="collab-annotation-loading"><span className="collab-loading-spinner" />正在加载本章标注…</div>
                    ) : !visibleAnnotations.length ? <EmptyState title="本章还没有标注" description="只选择真正有参考价值的句段，不需要把整章标满。" /> : visibleAnnotations.map((annotation) => (
                      <article className={`collab-annotation-card ${annotation.status}`} key={annotation.id}>
                        <div className="collab-annotation-head"><strong>{annotation.creator_name}</strong><span>{annotationStatusLabels[annotation.status] || annotation.status}</span></div>
                        <blockquote>{annotation.quote_text}</blockquote>
                        <div className="collab-annotation-tags">{annotation.categories.map((key) => <span key={key}>{categories.find((item) => item.key === key)?.label || key}</span>)}</div>
                        {annotation.note ? <p>{annotation.note}</p> : null}
                        <div className="collab-annotation-actions">
                          {selectedWork.visibility === "public" && !annotation.can_edit ? <><button type="button" className={annotation.current_user_vote === 1 ? "active" : ""} onClick={() => vote(annotation, 1)}>有用 {annotation.upvotes}</button><button type="button" className={annotation.current_user_vote === -1 ? "active" : ""} onClick={() => vote(annotation, -1)}>不准确 {annotation.downvotes}</button><button type="button" onClick={() => reportAnnotation(annotation)}>举报</button></> : null}
                          {annotation.can_edit ? <><button type="button" disabled={batchExplainingCurrentChapter} onClick={() => beginEdit(annotation)}>编辑</button><button type="button" className="danger" disabled={batchExplainingCurrentChapter} onClick={() => removeAnnotation(annotation).catch((err) => setError(err.message))}>删除</button></> : null}
                        </div>
                      </article>
                    ))}
                  </div>
                )}
              </aside>
            ) : null}
          </div>
        </div>
      ) : null}
    </>
  );
}

export default function SampleAnalysisPage() {
  return <SampleAnalysisContent />;
}
