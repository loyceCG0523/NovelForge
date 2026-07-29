"""按作品定位生成轻量、可执行的基调与节奏约束。"""

from __future__ import annotations

from typing import Any


COMEDY_MARKERS = ("轻喜剧", "喜剧", "搞笑", "幽默", "笑点", "诙谐")


def is_comedy_focused(genre: str = "", brief: dict[str, Any] | None = None) -> bool:
    """判断作品是否明确把喜剧作为主要阅读体验。"""
    brief = brief or {}
    source = "\n".join(
        str(value or "")
        for value in (
            genre,
            brief.get("selling_points"),
            brief.get("style_reference"),
            brief.get("plot_direction"),
        )
    )
    return any(marker in source for marker in COMEDY_MARKERS)


def resolve_event_chapter_count(
    brief: dict[str, Any] | None,
    fallback: int = 6,
) -> int:
    """读取作品级事件章数；普通事件允许 4—12 章。"""
    brief = brief or {}
    try:
        count = int(brief.get("event_chapter_count") or fallback)
    except (TypeError, ValueError):
        count = fallback
    return max(4, min(count, 12))


def build_tone_pacing_contract(
    genre: str = "",
    brief: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把“轻喜剧”翻译成正文和审校都能直接执行的短契约。"""
    brief = brief or {}
    if not is_comedy_focused(genre, brief):
        return {}

    return {
        "mode": "high_density_light_comedy",
        "tone": "困境只提供压力和反差；主导体验是人物主动应对后的轻松、乐观和局势变化。",
        "chapter_rules": [
            "全章围绕一个明确目标或麻烦推进，原则上不超过3个主要场景；开篇前15%进入当章问题。",
            "每个场景必须改变冲突、信息、关系或资源状态；同一结论只确认一次，不得换说法重复。",
            "每章安排2—3个由人物认知差、行动后果、误会升级或回调形成的喜剧节拍；至少1个笑点推动剧情或关系。",
            "手续、准备、训练、调查、赶路、采购和规则确认只保留会制造冲突、笑点或后果的节点，其余简述结果。",
            "连续低落不得占据多个场景；章节落点优先给出行动、微小胜利、关系收益或新的有趣麻烦。",
        ],
        "forbidden": [
            "把任何生活、职业或能力成长流程逐项写全来证明真实。",
            "用插入网络热梗、旁白解释或降低人物智商代替情境喜剧。",
            "一个章节只讨论、核对或协商，却没有实际行动和局势变化。",
        ],
    }
