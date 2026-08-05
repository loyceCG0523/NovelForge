"""在场景已通过复检时，兜底补入一条自然热梗。"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

from app.services.llm_client import LLMClient, LLMConfig
from app.services.meme_rag import (
    build_final_meme_usage,
    meme_phrase_usage_count,
    meme_phrase_used,
)
from app.services.paragraph_formatter import format_chapter_paragraphs


MAX_CANDIDATES = 5
MAX_PATCH_ATTEMPTS = 2
MAX_DEDUPLICATION_ATTEMPTS = 2
MAX_PARAGRAPH_GROWTH = 120
MAX_DEDUPLICATION_GROWTH = 80


def ensure_minimum_meme_usage(
    *,
    content: str,
    reference_pack: dict[str, Any] | None,
    chapter_progress: dict[str, Any] | None,
    llm_config: LLMConfig | None,
) -> dict[str, Any]:
    """记录自然采用结果；没有合适落点时允许零使用且绝不事后硬塞。"""
    references = [
        item
        for item in ((reference_pack or {}).get("references") or [])[:MAX_CANDIDATES]
        if isinstance(item, dict) and str(item.get("phrase") or "").strip()
    ]
    progress = dict(chapter_progress or {})
    usage = build_final_meme_usage(reference_pack, progress, content)
    if not references:
        return _result("not_required", content, progress, usage)
    if usage["adopted_count"] >= 1:
        return _result("already_satisfied", content, progress, usage)
    return _result(
        "optional_skipped",
        content,
        progress,
        usage,
        reason="候选没有在原生剧情和人物口吻中自然成立，已按准确性优先跳过",
    )


def repair_repeated_meme_usage(
    *,
    content: str,
    phrases: list[str] | None,
    llm_config: LLMConfig | None,
) -> dict[str, Any]:
    """只改写第二次及后续表达，确保同一热梗在本章只出现一次。"""
    repeated = [
        phrase
        for phrase in dict.fromkeys(str(item or "").strip() for item in (phrases or []))
        if phrase and meme_phrase_usage_count(phrase, content) > 1
    ]
    if not repeated:
        return {
            "status": "not_required",
            "content": content,
            "phrases": [],
            "paragraph_indexes": [],
            "reason": "",
        }
    if llm_config is None:
        return {
            "status": "failed",
            "content": content,
            "phrases": repeated,
            "paragraph_indexes": [],
            "reason": "正文模型未配置，无法消除重复热梗",
        }

    paragraphs = _split_paragraphs(content)
    target_indexes: set[int] = set()
    first_occurrences: list[dict[str, Any]] = []
    for phrase in repeated:
        seen_count = 0
        for index, paragraph in enumerate(paragraphs, start=1):
            count = meme_phrase_usage_count(phrase, paragraph)
            if not count:
                continue
            if seen_count == 0:
                first_occurrences.append(
                    {
                        "phrase": phrase,
                        "paragraph_index": index,
                        "text": paragraph,
                    }
                )
                if count > 1:
                    target_indexes.add(index)
            else:
                target_indexes.add(index)
            seen_count += count

    targets = [
        {
            "paragraph_index": index,
            "text": paragraphs[index - 1],
            "previous_two": paragraphs[max(0, index - 3) : index - 1],
            "next_two": paragraphs[index : index + 2],
        }
        for index in sorted(target_indexes)
    ]
    errors: list[str] = []
    client = LLMClient(llm_config)
    for attempt in range(1, MAX_DEDUPLICATION_ATTEMPTS + 1):
        payload = {
            "duplicate_phrases": repeated,
            "required_final_literal_count": 1,
            "protected_first_occurrences": first_occurrences,
            "targets": targets,
            "previous_validation_errors": errors[-1:],
        }
        try:
            _, parsed = client.complete_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是小说正文最小修订模型，只输出 JSON。同一热梗在一个剧情事件内只能使用一次。"
                            "最终全文中每个 duplicate_phrase 的字面或模板命中次数必须恰好等于1。"
                            "保留 protected_first_occurrences 中的首次使用，只改 targets 中第二次及后续表达；"
                            "若首次和重复出现在同一段，保留其中一次，把其余位置换成普通说法。"
                            "带“×”的模板，其不同填槽写法仍算同一个热梗。不得删除重要对白信息，"
                            "不得改变人物意图、事实、时间地点、前后因果，也不得修改目标段之外的内容。"
                            "previous_validation_errors 非空时必须针对错误纠正，不能原样返回上次补丁。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            '返回：{"patches":[{"paragraph_index":1,"new_text":"替换后的完整单段",'
                            '"reason":"如何保留原意并确保热梗只出现一次"}]}\n'
                            + json.dumps(payload, ensure_ascii=False)
                        ),
                    },
                ],
                temperature=0.15,
            )
            patches = parsed.get("patches") or []
            patch_by_index = {
                int(item.get("paragraph_index")): item
                for item in patches
                if isinstance(item, dict)
                and str(item.get("paragraph_index") or "").isdigit()
            }
            if set(patch_by_index) != target_indexes:
                raise ValueError("重复热梗补丁未完整覆盖目标段落")

            revised = list(paragraphs)
            for index in sorted(target_indexes):
                old_text = paragraphs[index - 1]
                new_text = str(patch_by_index[index].get("new_text") or "").strip()
                if not new_text or re.search(r"\r|\n", new_text):
                    raise ValueError(f"第 {index} 段补丁不是完整单段")
                if len(new_text) < max(8, int(len(old_text) * 0.65)):
                    raise ValueError(f"第 {index} 段补丁删减过多")
                if len(new_text) > len(old_text) + MAX_DEDUPLICATION_GROWTH:
                    raise ValueError(f"第 {index} 段补丁扩写过多")
                if SequenceMatcher(None, old_text, new_text).ratio() < 0.55:
                    raise ValueError(f"第 {index} 段补丁改动范围过大")
                revised[index - 1] = new_text

            revised_content = format_chapter_paragraphs("\n\n".join(revised))
            still_repeated = [
                phrase
                for phrase in repeated
                if meme_phrase_usage_count(phrase, revised_content) != 1
            ]
            if still_repeated:
                raise ValueError(
                    "补丁后热梗仍未保持唯一：" + "、".join(still_repeated)
                )
            return {
                "status": "repaired",
                "content": revised_content,
                "phrases": repeated,
                "paragraph_indexes": sorted(target_indexes),
                "attempt": attempt,
                "reason": "已保留首次使用，并改写第二次及后续表达",
            }
        except Exception as exc:
            errors.append(str(exc))

    return {
        "status": "failed",
        "content": content,
        "phrases": repeated,
        "paragraph_indexes": sorted(target_indexes),
        "attempt": MAX_DEDUPLICATION_ATTEMPTS,
        "reason": "；".join(errors[-2:]) or "重复热梗自动改写失败",
    }


def _build_patch_messages(
    *,
    paragraphs: list[str],
    references: list[dict[str, Any]],
    previous_errors: list[str],
) -> list[dict[str, str]]:
    payload = {
        "requirement": "必须从 candidates 中选择且自然采用 1 条",
        "candidates": [
            {
                "phrase": item.get("phrase", ""),
                "meaning": item.get("meaning", ""),
                "suitable_scenes": item.get("suitable_scenes", ""),
                "scene_fit_score": item.get("scene_fit_score", 0),
                "scene_fit": item.get("scene_fit") or {},
            }
            for item in references
        ],
        "paragraphs": [
            {"paragraph_index": index, "text": paragraph}
            for index, paragraph in enumerate(paragraphs, start=1)
        ],
        "previous_validation_errors": previous_errors[-2:],
    }
    return [
        {
            "role": "system",
            "content": (
                "你是小说正文局部修订模型。候选已经通过真实含义、人物关系和情绪场景复检，"
                "每条 scene_fit 给出了应落实的微场景。优先选择能在现有段落中完整实现"
                "required_setup→speech_act/原词→response→plot_consequence 的一条。"
                "可以在该段内补少量铺垫、回应或动作，但不得改变剧情事实、人物动机、时间地点和前后因果，"
                "不得解释梗的出处，不得把对白写成网络段子合集，也不得修改其他段落。"
                "固定短语必须原样出现；带“×”的模板要把“×”替换成当前场景中的具体短词。"
                "如果任何候选都无法在一个现有段落内完整成立，返回 applicable=false，"
                "不得只塞入原词或临时改变人物关系来凑数。"
                "只输出 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                "返回一个最小段落替换："
                '{"applicable":true,"candidate_phrase":"候选库中的原始短语","rendered_phrase":"正文实际写法",'
                '"speaker":"说话人或叙述载体","listener":"听话人","relationship":"人物关系",'
                '"emotion":"当时情绪","speech_act":"这句话完成的语言动作",'
                '"required_setup":"原词前的具体铺垫","scene_anchor":"具体交流节点",'
                '"response":"对方的即时回应","plot_consequence":"对剧情或关系造成的后果",'
                '"paragraph_index":1,"new_text":"替换后的完整单段",'
                '"reason":"为什么这里自然"}\n\n'
                + json.dumps(payload, ensure_ascii=False)
            ),
        },
    ]


def _validate_patch(
    parsed: dict[str, Any],
    paragraphs: list[str],
    references: list[dict[str, Any]],
) -> dict[str, Any]:
    if parsed.get("applicable") is False:
        raise ValueError(
            str(parsed.get("reason") or "现有段落无法承载完整热梗微场景")
        )
    candidate_phrase = str(parsed.get("candidate_phrase") or "").strip()
    allowed = {
        str(item.get("phrase") or "").strip(): item
        for item in references
        if str(item.get("phrase") or "").strip()
    }
    if candidate_phrase not in allowed:
        raise ValueError("candidate_phrase 不属于本章候选库")
    reference = allowed[candidate_phrase]
    scene_fit = (
        reference.get("scene_fit")
        if isinstance(reference.get("scene_fit"), dict)
        else {}
    )
    try:
        paragraph_index = int(parsed.get("paragraph_index"))
    except (TypeError, ValueError) as exc:
        raise ValueError("paragraph_index 无效") from exc
    if not 1 <= paragraph_index <= len(paragraphs):
        raise ValueError("paragraph_index 超出正文范围")

    new_text = str(parsed.get("new_text") or "").strip()
    if not new_text or re.search(r"\r|\n", new_text):
        raise ValueError("new_text 必须是一个完整单段")
    if not meme_phrase_used(candidate_phrase, new_text):
        raise ValueError("new_text 未使用选中的候选热梗")
    old_text = paragraphs[paragraph_index - 1]
    if len(new_text) > len(old_text) + MAX_PARAGRAPH_GROWTH:
        raise ValueError("热梗补丁改写范围过大")
    scene_fields = {
        key: str(parsed.get(key) or scene_fit.get(key) or "").strip()
        for key in (
            "speaker",
            "listener",
            "relationship",
            "emotion",
            "speech_act",
            "required_setup",
            "scene_anchor",
            "response",
            "plot_consequence",
        )
    }
    if scene_fit and not all(
        scene_fields[key]
        for key in (
            "speaker",
            "speech_act",
            "required_setup",
            "scene_anchor",
            "response",
            "plot_consequence",
        )
    ):
        raise ValueError("补丁未落实完整的热梗微场景")
    return {
        "candidate_phrase": candidate_phrase,
        "rendered_phrase": str(parsed.get("rendered_phrase") or candidate_phrase).strip(),
        **{
            key: value[:180] if key != "speaker" else value[:80]
            for key, value in scene_fields.items()
        },
        "paragraph_index": paragraph_index,
        "new_text": new_text,
        "reason": str(parsed.get("reason") or "选择本章最自然的交流节点").strip()[:180],
    }


def _mark_phrase_used(
    chapter_progress: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    result = dict(chapter_progress)
    plan = [
        dict(item)
        for item in (chapter_progress.get("meme_usage_plan") or [])
        if isinstance(item, dict)
    ]
    found = False
    for item in plan:
        if str(item.get("phrase") or "").strip() != patch["candidate_phrase"]:
            continue
        item.update(
            {
                "decision": "use",
                "speaker": patch["speaker"],
                "listener": patch["listener"],
                "relationship": patch["relationship"],
                "emotion": patch["emotion"],
                "speech_act": patch["speech_act"],
                "required_setup": patch["required_setup"],
                "scene_anchor": patch["scene_anchor"],
                "response": patch["response"],
                "plot_consequence": patch["plot_consequence"],
                "reason": patch["reason"],
            }
        )
        found = True
        break
    if not found:
        plan.append(
            {
                "phrase": patch["candidate_phrase"],
                "decision": "use",
                "speaker": patch["speaker"],
                "listener": patch["listener"],
                "relationship": patch["relationship"],
                "emotion": patch["emotion"],
                "speech_act": patch["speech_act"],
                "required_setup": patch["required_setup"],
                "scene_anchor": patch["scene_anchor"],
                "response": patch["response"],
                "plot_consequence": patch["plot_consequence"],
                "reason": patch["reason"],
            }
        )
    result["meme_usage_plan"] = plan[:MAX_CANDIDATES]
    return result


def _split_paragraphs(content: str) -> list[str]:
    return [
        paragraph.strip()
        for paragraph in re.split(r"\r?\n\s*\r?\n|\r?\n+", content or "")
        if paragraph.strip()
    ]


def _result(
    status: str,
    content: str,
    chapter_progress: dict[str, Any],
    usage: dict[str, Any],
    *,
    attempt: int = 0,
    phrase: str = "",
    paragraph_index: int = 0,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "content": content,
        "chapter_progress": chapter_progress,
        "usage": usage,
        "attempt": attempt,
        "phrase": phrase,
        "paragraph_index": paragraph_index,
        "reason": reason,
    }
