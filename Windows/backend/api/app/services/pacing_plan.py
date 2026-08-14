"""全书节奏计划工具。

目标字数不是简单的完成按钮，而是整本书的篇幅预算和阶段坐标。
这个模块把 target_words 转换成可复用的阶段计划，供 StoryBible、自动生产总控
和事件规划 Agent 共同判断：当前应该开新矛盾、推进终局，还是进入收束。
"""

from __future__ import annotations

from typing import Any


DEFAULT_WORD_TOLERANCE = {
    "min_completion_ratio": 0.95,
    "ideal_completion_ratio": 1.0,
    "max_overrun_ratio": 1.08,
}

DEFAULT_PHASES = [
    {
        "phase": "opening",
        "label": "开篇建立",
        "start_ratio": 0.0,
        "end_ratio": 0.12,
        "narrative_goal": "建立主角处境、核心卖点、初始冲突和读者期待。",
        "event_policy": "允许引入核心设定和主要人物，但避免堆叠过多支线。",
    },
    {
        "phase": "development",
        "label": "主线展开",
        "start_ratio": 0.12,
        "end_ratio": 0.4,
        "narrative_goal": "围绕长期目标展开阶段性事件，形成稳定追读动力。",
        "event_policy": "可以开启阶段支线，但每个事件必须推进主线或人物关系。",
    },
    {
        "phase": "midpoint",
        "label": "中段转折",
        "start_ratio": 0.4,
        "end_ratio": 0.65,
        "narrative_goal": "制造认知变化、关系重排或阶段真相，让主线压力升级。",
        "event_policy": "减少日常消耗型事件，优先安排反转、代价和新信息释放。",
    },
    {
        "phase": "escalation",
        "label": "高压升级",
        "start_ratio": 0.65,
        "end_ratio": 0.85,
        "narrative_goal": "把核心冲突推向不可回避，准备进入终局。",
        "event_policy": "控制新坑数量，优先回收中长期伏笔并压缩支线。",
    },
    {
        "phase": "final_arc",
        "label": "终局推进",
        "start_ratio": 0.85,
        "end_ratio": 0.95,
        "narrative_goal": "集中解决主线冲突、关键人物选择和核心伏笔。",
        "event_policy": "原则上不再开启大型新矛盾；下一事件钩子只能服务最终冲突或续作。",
    },
    {
        "phase": "ending",
        "label": "结局收束",
        "start_ratio": 0.95,
        "end_ratio": 1.08,
        "narrative_goal": "完成结局事件、人物弧光落点、伏笔回收和必要余波。",
        "event_policy": "允许不再留下下一事件钩子；优先闭环而不是延展。",
    },
]

ENDING_PHASES = {"final_arc", "ending"}
NARRATIVE_CLOSING_EVENT_TYPES = {"finale", "epilogue"}


def build_default_pacing_plan(target_words: int) -> dict[str, Any]:
    """按目标字数生成默认全书节奏计划。"""
    target_words = _safe_positive_int(target_words, 300000)
    return {
        "schema_version": "pacing_plan.v1",
        "target_words": target_words,
        "word_tolerance": DEFAULT_WORD_TOLERANCE.copy(),
        "global_phases": [phase.copy() for phase in DEFAULT_PHASES],
        "completion_criteria": [
            "字数进入允许完结窗口",
            "主线冲突已经解决",
            "主角成长线有明确落点",
            "关键伏笔已回收或明确转为番外/续作钩子",
            "最后一个剧情事件类型为 finale 或 epilogue",
        ],
        "ending_policy": {
            "start_final_arc_ratio": 0.85,
            "min_completion_ratio": DEFAULT_WORD_TOLERANCE["min_completion_ratio"],
            "max_overrun_ratio": DEFAULT_WORD_TOLERANCE["max_overrun_ratio"],
            "allow_new_major_hooks_after_ratio": 0.85,
            "require_narrative_closure": True,
        },
    }


def normalize_pacing_plan(plan: dict[str, Any] | None, target_words: int) -> dict[str, Any]:
    """清洗外部或旧版 StoryBible 中的 pacing_plan，保证关键字段存在。"""
    fallback = build_default_pacing_plan(target_words)
    if not isinstance(plan, dict):
        return fallback

    normalized = {**fallback, **plan}
    normalized["target_words"] = _safe_positive_int(normalized.get("target_words"), target_words)

    tolerance = normalized.get("word_tolerance")
    if not isinstance(tolerance, dict):
        tolerance = {}
    normalized["word_tolerance"] = {
        **fallback["word_tolerance"],
        **tolerance,
    }

    phases = normalized.get("global_phases")
    if not isinstance(phases, list) or not phases:
        phases = fallback["global_phases"]
    normalized["global_phases"] = _normalize_phases(phases, fallback["global_phases"])

    criteria = normalized.get("completion_criteria")
    if not isinstance(criteria, list) or not criteria:
        criteria = fallback["completion_criteria"]
    normalized["completion_criteria"] = criteria

    ending_policy = normalized.get("ending_policy")
    if not isinstance(ending_policy, dict):
        ending_policy = {}
    normalized["ending_policy"] = {
        **fallback["ending_policy"],
        **ending_policy,
    }
    normalized["schema_version"] = fallback["schema_version"]
    return normalized


def resolve_pacing_state(
    *,
    target_words: int,
    current_words: int,
    pacing_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """根据当前字数解析全书阶段和完结窗口。"""
    target_words = _safe_positive_int(target_words, 300000)
    current_words = max(0, _safe_int(current_words, 0))
    plan = normalize_pacing_plan(pacing_plan, target_words)
    tolerance = plan["word_tolerance"]
    min_completion_words = round(target_words * _safe_float(tolerance.get("min_completion_ratio"), 0.95))
    ideal_completion_words = round(target_words * _safe_float(tolerance.get("ideal_completion_ratio"), 1.0))
    max_overrun_words = round(target_words * _safe_float(tolerance.get("max_overrun_ratio"), 1.08))
    progress_ratio = current_words / target_words if target_words else 0.0
    phase = _resolve_phase(plan["global_phases"], progress_ratio)

    return {
        "schema_version": "pacing_state.v1",
        "target_words": target_words,
        "current_words": current_words,
        "remaining_words": max(0, target_words - current_words),
        "progress_ratio": round(progress_ratio, 4),
        "progress_percent": min(999, round(progress_ratio * 100, 1)),
        "phase": phase["phase"],
        "phase_label": phase.get("label", phase["phase"]),
        "phase_goal": phase.get("narrative_goal", ""),
        "event_policy": phase.get("event_policy", ""),
        "min_completion_words": min_completion_words,
        "ideal_completion_words": ideal_completion_words,
        "max_overrun_words": max_overrun_words,
        "is_in_completion_window": current_words >= min_completion_words,
        "is_past_target": current_words >= target_words,
        "is_overrun": current_words >= max_overrun_words,
        "is_final_phase": phase["phase"] in ENDING_PHASES,
        "allow_new_major_hooks": progress_ratio < _safe_float(
            (plan.get("ending_policy") or {}).get("allow_new_major_hooks_after_ratio"),
            0.85,
        ),
        "requires_narrative_closure": bool((plan.get("ending_policy") or {}).get("require_narrative_closure", True)),
    }


def is_closing_event(event_payload: dict[str, Any] | None) -> bool:
    """判断某个剧情事件是否声明完成了整书收束。"""
    if not isinstance(event_payload, dict):
        return False

    completion = event_payload.get("narrative_completion")
    if not isinstance(completion, dict):
        event_type = str(event_payload.get("event_type") or "").strip().lower()
        return event_type in NARRATIVE_CLOSING_EVENT_TYPES
    checks = [
        completion.get("main_conflict_resolved"),
        completion.get("protagonist_arc_completed"),
        completion.get("key_foreshadowing_resolved"),
        completion.get("ending_satisfied"),
    ]
    return all(bool(item) for item in checks)


def _resolve_phase(phases: list[dict[str, Any]], progress_ratio: float) -> dict[str, Any]:
    for phase in phases:
        start_ratio = _safe_float(phase.get("start_ratio"), 0.0)
        end_ratio = _safe_float(phase.get("end_ratio"), 1.0)
        if start_ratio <= progress_ratio < end_ratio:
            return phase
    return phases[-1] if phases else DEFAULT_PHASES[-1]


def _normalize_phases(phases: list[Any], fallback_phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for index, phase in enumerate(phases):
        if not isinstance(phase, dict):
            continue
        fallback = fallback_phases[min(index, len(fallback_phases) - 1)]
        normalized.append(
            {
                **fallback,
                **phase,
                "start_ratio": _safe_float(phase.get("start_ratio"), fallback["start_ratio"]),
                "end_ratio": _safe_float(phase.get("end_ratio"), fallback["end_ratio"]),
            }
        )
    return normalized or [phase.copy() for phase in fallback_phases]


def _safe_positive_int(value: Any, default: int) -> int:
    return max(1, _safe_int(value, default))


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
