"""章节字数护栏。

采用安全目标区间、动态差值修正、最佳候选保留和低温局部精修。多次修正后仍未
达标时返回最接近范围的草稿，由上层保存并暂停，而不是让整条生产任务直接失败。
"""

from __future__ import annotations

import json
import math
import hashlib
from statistics import median
from typing import Any, Callable

from app.services.paragraph_formatter import meaningful_length


DEFAULT_CHAPTER_WORD_MIN = 2500
DEFAULT_CHAPTER_WORD_MAX = 2800
MAX_WORD_GUARD_ATTEMPTS = 3
WORD_GUARD_FINE_TUNE_TEMPERATURE = 0.2
MODEL_CALIBRATION_MAX_SAMPLES = 3
MODEL_CALIBRATION_MIN_RATIO = 0.67
MODEL_CALIBRATION_MAX_RATIO = 2.0
MODEL_CALIBRATION_DAMPING = 0.5


def normalize_chapter_word_range(word_range: dict[str, Any] | None) -> dict[str, int | str]:
    """把上下文中的章节字数范围纠偏成稳定结构。"""
    word_range = word_range or {}
    min_words = max(500, _safe_int(word_range.get("min"), DEFAULT_CHAPTER_WORD_MIN))
    max_words = max(500, _safe_int(word_range.get("max"), DEFAULT_CHAPTER_WORD_MAX))
    if min_words > max_words:
        min_words, max_words = max_words, min_words
    return {"min": min_words, "max": max_words, "unit": "字"}


def build_safe_chapter_word_range(word_range: dict[str, Any] | None) -> dict[str, int | str]:
    """生成保守的模型提示目标；验收仍严格使用用户硬范围。

    初稿目标放在硬范围内部并预留上下缓冲。只有拿到该轮真实偏差后，后续目标才
    做有限幅度校准，避免用固定折扣把原本正常的模型直接压成欠长稿。
    """
    normalized = normalize_chapter_word_range(word_range)
    min_words = int(normalized["min"])
    max_words = int(normalized["max"])
    width = max_words - min_words
    if width < 200:
        midpoint = (min_words + max_words) // 2
        return {"min": midpoint, "max": midpoint, "unit": "字"}
    midpoint = (min_words + max_words) / 2
    target_width = max(100, min(200, int(width * 0.20)))
    safe_min = int(math.ceil((midpoint - target_width / 2) / 100) * 100)
    safe_max = int(math.floor((midpoint + target_width / 2) / 100) * 100)
    if safe_min > safe_max:
        safe_min = safe_max = max(500, int(midpoint // 100 * 100))
    return {"min": safe_min, "max": safe_max, "unit": "字"}


def build_model_calibration_key(base_url: str, model: str) -> str:
    """生成不包含 API Key 的模型配置标识，避免混用不同供应商的同名模型。"""
    normalized = f"{(base_url or '').strip().rstrip('/').lower()}::{(model or '').strip().lower()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def build_calibrated_chapter_word_target(
    word_range: dict[str, Any] | None,
    recent_chapters: list[dict[str, Any]] | None,
    model_calibration_key: str,
) -> dict[str, Any]:
    """根据同一模型最近章节的首轮偏差，预校准下一章的提示目标。

    这里只校准生成前的提示字数，最终验收仍使用用户设置的硬范围。历史样本只读取
    首轮生成结果，避免把后续重写或审校修改造成的长度变化错误归因给模型初稿。
    """
    nominal = build_safe_chapter_word_range(word_range)
    nominal_min = int(nominal["min"])
    nominal_max = int(nominal["max"])
    ratios: list[float] = []

    for chapter in reversed(recent_chapters or []):
        guard = chapter.get("word_guard") if isinstance(chapter, dict) else None
        if not isinstance(guard, dict):
            continue
        if guard.get("model_calibration_key") != model_calibration_key:
            continue
        attempts = guard.get("attempts")
        first_attempt = attempts[0] if isinstance(attempts, list) and attempts else guard
        if not isinstance(first_attempt, dict):
            continue
        prompted_min = _safe_int(first_attempt.get("prompt_target_min"), 0)
        prompted_max = _safe_int(first_attempt.get("prompt_target_max"), 0)
        actual_words = _safe_int(first_attempt.get("actual_words"), 0)
        prompted_midpoint = (prompted_min + prompted_max) / 2
        if prompted_midpoint <= 0 or actual_words <= 0:
            continue
        ratio = actual_words / prompted_midpoint
        if 0.4 <= ratio <= 3.0:
            ratios.append(ratio)
        if len(ratios) >= MODEL_CALIBRATION_MAX_SAMPLES:
            break

    if not ratios:
        return {
            "nominal_target_min": nominal_min,
            "nominal_target_max": nominal_max,
            "prompt_target_min": nominal_min,
            "prompt_target_max": nominal_max,
            "observed_ratio": 1.0,
            "sample_count": 0,
            "strategy": "middle_range",
        }

    observed_ratio = median(ratios)
    # 只吸收一半历史偏差，让目标逐章收敛，避免一次偶发超长导致下一章直接摆到过短。
    damped_ratio = 1.0 + (observed_ratio - 1.0) * MODEL_CALIBRATION_DAMPING
    applied_ratio = max(
        MODEL_CALIBRATION_MIN_RATIO,
        min(damped_ratio, MODEL_CALIBRATION_MAX_RATIO),
    )
    prompt_min = max(500, int(math.floor((nominal_min / applied_ratio) / 100) * 100))
    prompt_max = max(prompt_min + 100, int(math.ceil((nominal_max / applied_ratio) / 100) * 100))
    return {
        "nominal_target_min": nominal_min,
        "nominal_target_max": nominal_max,
        "prompt_target_min": prompt_min,
        "prompt_target_max": prompt_max,
        "observed_ratio": round(observed_ratio, 4),
        "applied_ratio": round(applied_ratio, 4),
        "damping": MODEL_CALIBRATION_DAMPING,
        "sample_count": len(ratios),
        "strategy": "damped_same_model_history",
    }


def get_context_chapter_word_range(context: dict[str, Any]) -> dict[str, int | str]:
    """从 ChapterContext 中读取单章字数范围。"""
    guidance = context.get("generation_guidance") or {}
    constraints = context.get("constraints") or {}
    return normalize_chapter_word_range(
        guidance.get("chapter_word_range") or constraints.get("chapter_word_range")
    )


def count_chapter_words(content: str | None) -> int:
    """统计正文可见字符；自动分段新增的空行不应增加章节字数。"""
    return meaningful_length(content or "")


def build_word_guard_report(
    content: str | None,
    word_range: dict[str, Any] | None,
    attempt: int = 1,
) -> dict[str, Any]:
    """生成字数校验报告，便于写入上下文快照和任务结果。"""
    normalized_range = normalize_chapter_word_range(word_range)
    actual_words = count_chapter_words(content)
    min_words = int(normalized_range["min"])
    max_words = int(normalized_range["max"])
    within_range = min_words <= actual_words <= max_words
    if within_range:
        status, delta = "within_range", 0
    elif actual_words < min_words:
        status, delta = "too_short", min_words - actual_words
    else:
        status, delta = "too_long", actual_words - max_words
    return {
        "target_min": min_words,
        "target_max": max_words,
        "actual_words": actual_words,
        "within_range": within_range,
        "status": status,
        "delta": delta,
        "attempt": attempt,
    }


def estimate_chapter_max_tokens(word_range: dict[str, Any] | None) -> int:
    """给 Chat Completions 足够的输出上限，避免供应商默认截断正文。"""
    normalized_range = normalize_chapter_word_range(word_range)
    max_words = int(normalized_range["max"])
    return max(4096, min(32768, max_words * 3 + 1200))


def _build_initial_target_messages(
    base_messages: list[dict[str, str]],
    prompt_range: dict[str, Any],
) -> list[dict[str, str]]:
    return [
        *base_messages,
        {
            "role": "user",
            "content": (
                f"本轮用于模型规划篇幅的 content 目标是 {prompt_range['min']}-{prompt_range['max']} 字。"
                "请直接按这个目标安排完整章节结构，不要自行放大字数，也不要为了长度改变既定章节计划。"
            ),
        },
    ]


def _build_correction_target_range(
    guard_report: dict[str, Any],
    hard_range: dict[str, Any],
) -> dict[str, int]:
    """根据上一轮“提示目标/实际字符”的偏差，反推下一轮模型目标。"""
    hard_min = int(hard_range["min"])
    hard_max = int(hard_range["max"])
    hard_width = max(200, hard_max - hard_min)
    desired_min = hard_min + int(hard_width * 0.20)
    desired_max = hard_max - int(hard_width * 0.20)
    desired_midpoint = (desired_min + desired_max) / 2

    prompted_min = int(guard_report.get("prompt_target_min") or hard_min)
    prompted_max = int(guard_report.get("prompt_target_max") or hard_max)
    prompted_midpoint = max(1.0, (prompted_min + prompted_max) / 2)
    actual_words = max(1, int(guard_report.get("actual_words") or 0))
    raw_factor = desired_midpoint / actual_words
    # 单轮最多缩小 35% 或放大 35%，并限制全局中心范围，避免 1400→3700 之类的震荡。
    bounded_factor = max(0.65, min(raw_factor, 1.35))
    calibrated_midpoint = prompted_midpoint * bounded_factor
    global_min_midpoint = max(700.0, hard_min * 0.65)
    global_max_midpoint = hard_max * 1.125
    calibrated_midpoint = max(global_min_midpoint, min(calibrated_midpoint, global_max_midpoint))

    target_width = max(300, min(500, int(hard_width * 0.40)))
    target_min = max(500, int(math.floor((calibrated_midpoint - target_width / 2) / 100) * 100))
    target_max = max(target_min + 300, int(math.ceil((calibrated_midpoint + target_width / 2) / 100) * 100))
    return {"min": target_min, "max": target_max}


def build_word_guard_retry_messages(
    base_messages: list[dict[str, str]],
    generated: dict[str, Any],
    guard_report: dict[str, Any],
    target_range: dict[str, int] | None = None,
    local_edit: bool = False,
) -> list[dict[str, str]]:
    """基于当前最佳候选构造带明确增删字数的修正消息。"""
    target_min = int(guard_report["target_min"])
    target_max = int(guard_report["target_max"])
    actual_words = int(guard_report["actual_words"])
    correction_range = target_range or {"min": target_min, "max": target_max}
    correction_min = int(correction_range["min"])
    correction_max = int(correction_range["max"])

    if guard_report["status"] == "too_short":
        add_min = max(0, correction_min - actual_words)
        add_max = max(add_min, correction_max - actual_words)
        instruction = (
            f"上一版正文只有 {actual_words} 字，低于最低 {target_min} 字。"
            f"请增加约 {add_min}-{add_max} 字；按模型估算把正文控制到约 {correction_min}-{correction_max} 字。"
            "优先补足能推进情节的场景动作、人物互动、有效细节和冲突，不要用重复解释凑字数。"
            "不得用天气、灯光、家具、服装、食物、品牌或日常动作的连续铺陈凑字数。"
        )
    else:
        remove_min = max(0, actual_words - correction_max)
        remove_max = max(remove_min, actual_words - correction_min)
        instruction = (
            f"上一版正文有 {actual_words} 字，超过最高 {target_max} 字。"
            f"请至少删除约 {remove_min} 字，理想删除 {remove_min}-{remove_max} 字；"
            f"按模型估算把正文压缩到约 {correction_min}-{correction_max} 字。"
            "不得只改写措辞却几乎不减少长度；优先压缩过量环境描写、无功能生活细节、"
            "重复心理活动、解释性文字和低信息密度桥段，保留核心剧情与章末钩子。"
        )

    edit_scope = (
        "这是最后一次长度精修。只能做局部增删和句段压缩，不得重排段落、改变人物行为、改写核心事件或替换章末钩子。"
        if local_edit
        else "基于上一版正文定向修正长度，不要另起炉灶重写无关剧情。"
    )
    retry_prompt = "\n".join(
        [
            "需要修正上一版章节，因为正文长度没有满足单章字数范围。",
            "下面的模型目标已根据上一轮真实偏差做有限校准；系统仍以硬范围验收，不要擅自贴近硬范围上沿。",
            instruction,
            edit_scope,
            f"硬性要求：content 正文长度必须落在 {target_min}-{target_max} 字之间。",
            "只输出 JSON 对象，字段仍为 title、summary、content、chapter_progress；修改正文后必须同步更新实际摘要、完成节点和结尾状态。",
        ]
    )
    assistant_payload = {
        "title": generated.get("title") or "",
        "summary": generated.get("summary") or "",
        "content": generated.get("content") or "",
        "chapter_progress": generated.get("chapter_progress") or {},
    }
    return [
        *base_messages,
        {"role": "assistant", "content": json.dumps(assistant_payload, ensure_ascii=False)},
        {"role": "user", "content": retry_prompt},
    ]


def generate_chapter_with_word_guard(
    llm_client: Any,
    prompt_messages: list[dict[str, str]],
    word_range: dict[str, Any] | None,
    max_attempts: int = MAX_WORD_GUARD_ATTEMPTS,
    initial_target_range: dict[str, Any] | None = None,
    stream_callback: Callable[[str, int], None] | None = None,
    stream_reset_callback: Callable[[int], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """生成章节；若全部修正失败，返回最佳草稿并标记为待修订。"""
    normalized_range = normalize_chapter_word_range(word_range)
    safe_range = build_safe_chapter_word_range(normalized_range)
    selected_initial_range = initial_target_range or safe_range
    current_target_range = {
        "min": max(500, _safe_int(selected_initial_range.get("min"), int(safe_range["min"]))),
        "max": max(500, _safe_int(selected_initial_range.get("max"), int(safe_range["max"]))),
    }
    if current_target_range["min"] > current_target_range["max"]:
        current_target_range["min"], current_target_range["max"] = (
            current_target_range["max"],
            current_target_range["min"],
        )
    messages = _build_initial_target_messages(prompt_messages, current_target_range)
    attempts: list[dict[str, Any]] = []
    best_result: dict[str, Any] = {"title": "", "summary": "", "content": ""}
    best_report: dict[str, Any] = {}
    max_tokens = estimate_chapter_max_tokens(normalized_range)

    for attempt in range(1, max_attempts + 1):
        temperature = WORD_GUARD_FINE_TUNE_TEMPERATURE if attempt == max_attempts and attempt > 1 else None
        extra: dict[str, Any] = {}
        if stream_callback is not None:
            extra["on_content_delta"] = lambda delta, current_attempt=attempt: stream_callback(delta, current_attempt)
            extra["on_stream_reset"] = lambda current_attempt=attempt: (
                stream_reset_callback(current_attempt) if stream_reset_callback is not None else None
            )
        result = llm_client.generate_chapter(messages, max_tokens=max_tokens, temperature=temperature, **extra)
        report = build_word_guard_report(result.get("content"), normalized_range, attempt)
        report = {
            **report,
            "safe_target_min": int(safe_range["min"]),
            "safe_target_max": int(safe_range["max"]),
            "prompt_target_min": int(current_target_range["min"]),
            "prompt_target_max": int(current_target_range["max"]),
            "strategy": "low_temperature_local_edit" if temperature is not None else ("safe_target_generation" if attempt == 1 else "dynamic_length_rewrite"),
        }
        attempts.append(report)
        if not best_report or int(report["delta"]) < int(best_report["delta"]):
            best_result, best_report = result, report

        if report["within_range"]:
            return result, {
                **report,
                "attempts": attempts,
                "max_attempts": max_attempts,
                "enforced": True,
                "accepted": True,
                "needs_revision": False,
                "selected_attempt": attempt,
            }

        correction_range = _build_correction_target_range(best_report, normalized_range)
        current_target_range = correction_range
        messages = build_word_guard_retry_messages(
            prompt_messages,
            best_result,
            best_report,
            target_range=correction_range,
            local_edit=attempt + 1 == max_attempts,
        )

    return best_result, {
        **best_report,
        "attempts": attempts,
        "max_attempts": max_attempts,
        "enforced": True,
        "accepted": True,
        "strictly_within_range": False,
        "accepted_by_nearest": True,
        "needs_revision": False,
        "selected_attempt": best_report.get("attempt", max_attempts),
        "message": (
            f"已保留距离 {normalized_range['min']}-{normalized_range['max']} 字最近的"
            f"第 {best_report.get('attempt', max_attempts)} 版草稿；三次尝试后按最接近版本完成本章。"
        ),
    }


def attach_word_guard_to_context(context: dict, guard_report: dict[str, Any]) -> dict[str, Any]:
    """把字数护栏结果写入章节上下文快照。"""
    return {**context, "word_guard": guard_report}


def _safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
