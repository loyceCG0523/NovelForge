"""根据人工选区和人工标签生成可编辑的简短标注说明。"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from app.services.llm_client import (
    LLMClient,
    LLMConfig,
    LLMRequestCancelledError,
    build_llm_config,
    build_review_llm_config,
    is_official_deepseek_model,
)
from app.services.sample_collaboration import ANNOTATION_CATEGORIES


MAX_NOTE_LENGTH = 500
MAX_SEARCH_QUOTE_LENGTH = 160
MAX_BATCH_WORKERS = 4


class AnnotationNoteGenerationError(RuntimeError):
    """标注说明无法安全生成。"""


@dataclass(frozen=True)
class AnnotationNoteDraft:
    note: str
    provider: str
    model: str
    used_web_search: bool
    source_count: int = 0


@dataclass(frozen=True)
class AnnotationNoteJob:
    annotation_id: str
    quote_text: str
    surrounding_text: str
    categories: list[str]


@dataclass(frozen=True)
class AnnotationNoteJobResult:
    annotation_id: str
    draft: AnnotationNoteDraft | None = None
    error: str = ""


def _configured_models(preferences: dict[str, Any]) -> list[LLMConfig]:
    configs = [
        build_review_llm_config(preferences),
        build_llm_config(preferences),
    ]
    unique: list[LLMConfig] = []
    seen: set[tuple[str, str, str]] = set()
    for config in configs:
        if config is None:
            continue
        key = (config.base_url.rstrip("/"), config.model, config.api_key)
        if key not in seen:
            unique.append(config)
            seen.add(key)
    return unique


def _compact(value: str, *, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _clean_note(value: str) -> str:
    note = str(value or "").strip()
    note = re.sub(r"^```(?:text|markdown)?\s*|\s*```$", "", note, flags=re.I)
    note = re.sub(r"(?m)^\s*(?:[-*•]|\d+[.)、])\s*", "", note)
    note = re.sub(r"[ \t]+", " ", note)
    note = re.sub(r"\n{2,}", "\n", note).strip()
    if not note:
        raise AnnotationNoteGenerationError("模型没有返回有效说明，请稍后重试")
    sentences = [
        item.strip()
        for item in re.findall(r"[^。！？!?]+(?:[。！？!?]+|$)", note)
        if item.strip()
    ]
    if len(sentences) > 3:
        note = "".join(sentences[:3])
    if len(note) > MAX_NOTE_LENGTH:
        note = note[: MAX_NOTE_LENGTH - 1].rstrip("，,；;：:") + "。"
    return note


def _category_labels(categories: list[str]) -> str:
    return "、".join(ANNOTATION_CATEGORIES[item] for item in categories)


def _source_digest(sources: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for index, source in enumerate(sources[:5], start=1):
        title = _compact(str(source.get("title") or ""), limit=80)
        snippet = _compact(str(source.get("snippet") or ""), limit=260)
        url = _compact(str(source.get("url") or ""), limit=300)
        rows.append(f"{index}. {title}\n摘要：{snippet}\n网址：{url}")
    return "\n".join(rows)


def _generic_note_prompt(
    *, quote_text: str, surrounding_text: str, categories: list[str]
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你只负责解释人工已经选好的小说标注，不得重新判断标签、不得改写原文。"
                "写2—3句精练中文：先说明这段表达如何发挥作用，再提炼可复用的写法。"
                "必须结合原句具体分析，禁止空泛夸奖、禁止Markdown列表、禁止虚构背景。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"人工标签：{_category_labels(categories)}\n"
                f"人工选中的原文：{quote_text}\n"
                f"附近上下文：{surrounding_text}\n"
                "请直接输出2—3句说明，500字以内。"
            ),
        },
    ]


def _meme_note_prompt(
    *,
    quote_text: str,
    surrounding_text: str,
    categories: list[str],
    sources: list[dict[str, Any]],
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是中文网络热梗释义编辑。以下网页是刚由DeepSeek web_search检索到的资料。"
                "只依据检索资料和原文解释，不得编造梗义、出处或流行度。"
                "严格写3个短句：第一句以“梗义：”开头，说明当前通行含义；"
                "第二句以“此处：”开头，说明它在所选语境中的笑点或表达作用；"
                "第三句以“通用模板：”开头，抽象出可迁移的句式或使用条件。"
                "模板不得照搬作品中的人物名和专有设定，不要输出Markdown列表或引文。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"人工标签：{_category_labels(categories)}\n"
                f"人工选中的原文：{quote_text}\n"
                f"附近上下文：{surrounding_text}\n"
                f"联网检索资料：\n{_source_digest(sources)}\n"
                "请直接输出3句说明，500字以内。"
            ),
        },
    ]


def generate_annotation_note(
    *,
    preferences: dict[str, Any],
    quote_text: str,
    surrounding_text: str,
    categories: list[str],
) -> AnnotationNoteDraft:
    """生成说明草稿；网络热梗必须使用DeepSeek原生web_search，不降级Tavily。"""
    configs = _configured_models(preferences)
    if not configs:
        raise AnnotationNoteGenerationError("请先在设置中配置可用的正文或审校模型")

    if "internet_meme" not in categories:
        config = configs[0]
        note = LLMClient(config).complete_text(
            _generic_note_prompt(
                quote_text=quote_text,
                surrounding_text=surrounding_text,
                categories=categories,
            )
        )
        return AnnotationNoteDraft(
            note=_clean_note(note),
            provider="review_model",
            model=config.model,
            used_web_search=False,
        )

    config_candidates = [
        config
        for config in configs
        if is_official_deepseek_model(config.base_url, config.model)
    ]
    if not config_candidates:
        raise AnnotationNoteGenerationError(
            "网络热梗说明需要将正文或审校模型配置为 DeepSeek 官方系列模型"
        )
    query_quote = _compact(quote_text, limit=MAX_SEARCH_QUOTE_LENGTH)
    for config in config_candidates:
        try:
            sources = LLMClient(config).search_web(
                f'中文网络热梗“{query_quote}”的含义、常见语境和使用方式',
                max_results=5,
            )
        except LLMRequestCancelledError:
            raise
        except Exception:
            # 该型号不支持服务端 web_search（如 flash 系列只会输出 DSML 文本），
            # 或本次检索异常，尝试下一个已配置的 DeepSeek 模型。
            continue
        if not sources:
            continue
        note = LLMClient(config).complete_text(
            _meme_note_prompt(
                quote_text=quote_text,
                surrounding_text=surrounding_text,
                categories=categories,
                sources=sources,
            )
        )
        return AnnotationNoteDraft(
            note=_clean_note(note),
            provider="deepseek_web_search",
            model=config.model,
            used_web_search=True,
            source_count=len(sources),
        )

    # 已配置的 DeepSeek 型号都不支持服务端 web_search（或均未检到资料）时，
    # 回退为无联网核验的通用说明，避免批量解释被单条硬失败打断。
    fallback_config = configs[0]
    note = LLMClient(fallback_config).complete_text(
        _generic_note_prompt(
            quote_text=quote_text,
            surrounding_text=surrounding_text,
            categories=categories,
        )
    )
    return AnnotationNoteDraft(
        note=_clean_note(note),
        provider="review_model",
        model=fallback_config.model,
        used_web_search=False,
    )


def generate_annotation_notes_parallel(
    *,
    preferences: dict[str, Any],
    jobs: list[AnnotationNoteJob],
    max_workers: int = MAX_BATCH_WORKERS,
) -> list[AnnotationNoteJobResult]:
    """有界并行生成，保持输入顺序，并将单条异常隔离在对应结果中。"""
    if not jobs:
        return []

    def run(job: AnnotationNoteJob) -> AnnotationNoteJobResult:
        try:
            draft = generate_annotation_note(
                preferences=preferences,
                quote_text=job.quote_text,
                surrounding_text=job.surrounding_text,
                categories=job.categories,
            )
            return AnnotationNoteJobResult(job.annotation_id, draft=draft)
        except Exception as exc:
            return AnnotationNoteJobResult(job.annotation_id, error=str(exc))

    worker_count = max(1, min(int(max_workers), MAX_BATCH_WORKERS, len(jobs)))
    if worker_count == 1:
        return [run(job) for job in jobs]

    results: dict[str, AnnotationNoteJobResult] = {}
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="annotation-note",
    ) as executor:
        future_jobs = {executor.submit(run, job): job for job in jobs}
        for future in as_completed(future_jobs):
            job = future_jobs[future]
            try:
                results[job.annotation_id] = future.result()
            except Exception as exc:
                results[job.annotation_id] = AnnotationNoteJobResult(
                    job.annotation_id,
                    error=str(exc),
                )
    return [results[job.annotation_id] for job in jobs]
