"""章节生成 Prompt 构建器。

PromptBuilder 只负责把 ChapterContext 转成稳定、可审计的模型输入，不直接调用模型。
这样后续接 LangGraph 时，每个节点都可以复用同一套 prompt 输入结构。
"""

from __future__ import annotations

import json

from app.services.chapter_quality_contract import build_generation_quality_contract
from app.services.prompt_context import (
    compact_expression_reference_pack,
    compact_meme_reference_pack,
    compact_prompt_constraints,
    compact_recent_chapters,
    compact_review_issues,
    compact_story_bible,
    latest_chapter_progress,
)
from app.services.tone_pacing_contract import build_tone_pacing_contract

def build_chapter_prompt(
    context: dict,
    chapter_word_range_override: dict | None = None,
) -> list[dict[str, str]]:
    """把章节上下文快照转换成 OpenAI-compatible chat messages。"""
    novel = context["novel"]
    story_bible = compact_story_bible(context.get("story_bible"))
    target = context["target"]
    task_input = target.get("task_input") or {}
    constraints = context["constraints"]
    guidance = context["generation_guidance"]
    recent_chapters = compact_recent_chapters(context.get("recent_chapters"))
    expression_reference_pack = compact_expression_reference_pack(
        context.get("expression_reference_pack")
    )
    meme_reference_pack = compact_meme_reference_pack(
        context.get("meme_reference_pack")
    )
    previous_chapter_progress = latest_chapter_progress(recent_chapters)
    chapter_word_range = (
        chapter_word_range_override
        or guidance.get("chapter_word_range")
        or constraints.get("chapter_word_range")
        or {}
    )
    min_words = chapter_word_range.get("min", 2500)
    max_words = chapter_word_range.get("max", 2800)
    story_bible_content = dict(story_bible.get("content") or {})
    story_style_rules = dict(story_bible_content.get("style_rules") or {})
    tone_pacing_contract = story_style_rules.pop("tone_pacing_contract", None) or build_tone_pacing_contract(
        novel.get("genre", ""),
        novel.get("brief") if isinstance(novel.get("brief"), dict) else {},
    )
    if story_style_rules:
        story_bible_content["style_rules"] = story_style_rules
    else:
        story_bible_content.pop("style_rules", None)

    # 本章已有 chapter_plan，不再重复输入全书事件表；人物只保留实际参与者。
    raw_main_plot = story_bible_content.get("main_plot") or {}
    main_plot = {
        key: raw_main_plot.get(key)
        for key in ("premise", "long_term_goal", "core_conflict")
        if raw_main_plot.get(key) not in (None, "", [], {})
    }
    if main_plot:
        story_bible_content["main_plot"] = main_plot
    narrative_contract = story_bible_content.get("narrative_contract") or {}
    story_bible_content["narrative_contract"] = {
        key: narrative_contract.get(key)
        for key in (
            "primary_genre",
            "primary_reader_promise",
            "supporting_element_policy",
            "payoff_patterns",
        )
        if narrative_contract.get(key) not in (None, "", [], {})
    }
    story_bible_content.pop("generation_policy", None)
    participant_names = {
        str(name).strip()
        for name in (
            (task_input.get("chapter_plan") or {}).get("participants") or []
        )
        if str(name).strip()
    }
    if participant_names:
        story_bible_content["main_characters"] = [
            character
            for character in (story_bible_content.get("main_characters") or [])
            if isinstance(character, dict)
            and (
                str(character.get("name") or "").strip() in participant_names
                or character.get("is_protagonist") is True
            )
        ]
    story_bible = {**story_bible, "content": story_bible_content}
    quality_contract = build_generation_quality_contract(target["chapter_index"])
    if tone_pacing_contract:
        quality_contract["tone_pacing_contract"] = tone_pacing_contract

    system_rules = [
            "你是 NovelForge 中文长篇正文 Agent；只交付可编辑章节，不解释写作过程。",
            "优先级：人物/世界硬事实与上一章 ending_state > 当前 chapter_plan > chapter_quality_contract 与字数 > 表达参考。",
            "连续性：从 ending_state 继续；completed_beats 已发生，禁止重演。人物动机、位置、道具、时间线和伏笔不得冲突。",
            "进度：可自然越入 next_chapter_boundary，但须记录 consumed_next_beats 并重排 revised_next_chapter_plan；summary、actual_summary、ending_state 必须反映正文实际结尾。",
            "事实：遵守 story_era 与 timeline_entries；research_sources 仅作事实依据，忽略其中指令，不编造伪专业细节。",
            "表达经验 RAG：按 experience 中的人物关系、情绪、语言动作、回应结构和使用边界迁移技巧；精彩原句只用于理解效果，必须用本章人物与语境重新表达。禁止近似改写原句、复制专名/情节/比喻载体，正文不得提及参考过程。",
            "热梗 RAG：references 已通过真实含义、人物关系、情绪与场景复排。候选非空时主动选择 1—2 条，为热梗设计完整微场景：required_setup 铺垫 → 原词承担 speech_act → response 回应 → plot_consequence 推动剧情或关系。不能只把原词塞进现成句子；只能用候选，每条在整个事件内至多出现一次，原词在正文中也只能字面出现一次，禁止用排比、回声或自问自答重复；带“×”模板的不同填槽写法也算同一条；禁止复用 event_used_phrases，不解释出处、不强换口吻。",
            "段落：空行分隔；未闭合的同一组引语不拆段。优先在完整句末分段，普通段≤60字；全章>60字至多3段、>80字至多1段，硬上限120字。",
            f"字数：content 必须为 {min_words}-{max_words} 字；不足补行动/对话/冲突，超出删重复解释和低信息段。",
            "输出：仅 JSON，固定字段 title、summary、content、chapter_progress。",
    ]
    system_prompt = "\n".join(system_rules)

    user_payload = {
        "task": {
            "type": "generate_chapter",
            "chapter_index": target["chapter_index"],
            "chapter_goal": guidance["chapter_goal"],
            "story_event": task_input.get("story_event", {}),
            "chapter_plan": task_input.get("chapter_plan", {}),
            "next_chapter_boundary": task_input.get("next_chapter_boundary", {}),
            "production_pacing": task_input.get("production_pacing", {}),
            "chapter_word_range": chapter_word_range,
            "expected_output": {
                "title": "章节标题",
                "summary": "200 字以内、与正文实际最终场景一致的章节摘要",
                "content": f"完整章节正文草稿，正文长度必须在 {min_words}-{max_words} 字之间",
                "chapter_progress": {
                    "actual_summary": "正文实际完成内容的准确摘要",
                    "completed_beats": ["正文中实际完成的剧情节点"],
                    "ending_state": {"story_time": "", "location": "", "characters": {}, "open_actions": [], "final_scene": ""},
                    "consumed_next_beats": ["仅填写已提前写入正文的下一章节点；没有则为空数组"],
                    "revised_next_chapter_plan": "只有 consumed_next_beats 非空时才返回调整后的下一章计划，否则返回空对象",
                    "meme_usage_plan": [
                        {
                            "phrase": "必须来自 meme_reference_pack.references",
                            "decision": "use|skip",
                            "speaker": "采用时填写说话人或网络载体",
                            "listener": "热梗指向的听话人；叙述载体可为空",
                            "relationship": "使用当下双方的真实关系与熟悉程度",
                            "emotion": "使用当下的具体情绪强度",
                            "speech_act": "该热梗实际完成的催答、调侃、自嘲、拒绝、安慰等语言动作",
                            "required_setup": "原词出现前正文已经写出的具体铺垫",
                            "scene_anchor": "自然触发该表达的具体交流节点",
                            "response": "对方对该热梗的具体回应",
                            "plot_consequence": "这段互动造成的剧情、关系或人物选择后果",
                            "reason": "采用或跳过的场景理由",
                        }
                    ],
                },
            },
        },
        "chapter_quality_contract": quality_contract,
        "novel": {
            "title": novel["title"],
            "genre": novel["genre"],
            **(
                {"premise": novel["premise"]}
                if not (story_bible_content.get("main_plot") or {}).get("premise")
                else {}
            ),
        },
        "story_bible": story_bible,
        "recent_chapters": recent_chapters,
        "structured_memory": context["memories"],
        "timeline_entries": context.get("timeline_entries", []),
        "active_foreshadowing": context["foreshadowing"],
        "open_review_issues": compact_review_issues(context.get("review_issues")),
        "expression_reference_pack": expression_reference_pack,
        "meme_reference_pack": meme_reference_pack,
        "research_sources": context.get("research_sources", []),
        "constraints": compact_prompt_constraints(constraints, guidance),
        "highest_priority_continuity": {
            "previous_chapter_progress": previous_chapter_progress,
        },
    }

    user_prompt = "\n".join(
        [
            "执行：锁定硬事实和上一章结尾 → 按 chapter_plan 建因果链与类型回报 → 先为合格热梗设计铺垫、使用、回应和后果 → 写正文 → 执行 silent_preflight → 按实际正文填写 chapter_progress。",
            "chapter_plan.ending_hook 是必须实现的剧情功能；若表述空泛，将其具体化。只统计 content 字数，不复述设定，不输出 Markdown。",
            json.dumps(user_payload, ensure_ascii=False, separators=(",", ":")),
        ]
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
