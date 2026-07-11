"""章节生成 Prompt 构建器。

PromptBuilder 只负责把 ChapterContext 转成稳定、可审计的模型输入，不直接调用模型。
这样后续接 LangGraph 时，每个节点都可以复用同一套 prompt 输入结构。
"""

import json


def build_chapter_prompt(context: dict) -> list[dict[str, str]]:
    """把章节上下文快照转换成 OpenAI-compatible chat messages。"""
    novel = context["novel"]
    story_bible = context.get("story_bible") or {}
    target = context["target"]
    constraints = context["constraints"]
    guidance = context["generation_guidance"]
    chapter_word_range = guidance.get("chapter_word_range") or constraints.get("chapter_word_range") or {}

    system_prompt = "\n".join(
        [
            "你是 NovelForge 的长篇小说章节生成 Agent。",
            "你的目标是生成可继续编辑的中文长篇小说章节草稿，而不是解释你的写作过程。",
            "必须严格保持连续性：人物动机、物品状态、地点关系、伏笔推进不能与上下文冲突。",
            "如果存在 sample_style_references，必须把它作为高优先级风格约束：参考其量化句长、对白比例、节奏模型、反 AI 策略和 LLM 风格策略，但不得复制样本具体内容。",
            "必须降低 AI 味：避免模板化转折、解释性独白、空泛情绪词、过度总结和说教。",
            "输出必须是 JSON 对象，字段固定为 title、summary、content。",
        ]
    )

    user_payload = {
        "task": {
            "type": "generate_chapter",
            "chapter_index": target["chapter_index"],
            "chapter_goal": guidance["chapter_goal"],
            "story_event": target.get("task_input", {}).get("story_event", {}),
            "chapter_plan": target.get("task_input", {}).get("chapter_plan", {}),
            "chapter_word_range": chapter_word_range,
            "expected_output": {
                "title": "章节标题",
                "summary": "200 字以内章节摘要",
                "content": f"完整章节正文草稿，正文长度控制在 {chapter_word_range.get('min', 2000)}-{chapter_word_range.get('max', 3000)} 字",
            },
        },
        "novel": {
            "title": novel["title"],
            "genre": novel["genre"],
            "premise": novel["premise"],
            "brief": novel["brief"],
        },
        "story_bible": story_bible,
        "recent_chapters": context["recent_chapters"],
        "structured_memory": context["memories"],
        "active_foreshadowing": context["foreshadowing"],
        "open_review_issues": context["review_issues"],
        "sample_style_references": context.get("sample_style_references", []),
        "constraints": {
            **constraints,
            "continuity_reminders": guidance["continuity_reminders"],
            "anti_ai_reminders": guidance["anti_ai_reminders"],
        },
    }

    user_prompt = "\n".join(
        [
            "请基于以下结构化上下文生成目标章节。",
            f"正文长度必须尽量控制在 {chapter_word_range.get('min', 2000)}-{chapter_word_range.get('max', 3000)} 字之间，不要明显低于或超过该范围。",
            "样本分析报告是本次生成的强风格参考：优先吸收其可迁移工程特征、节奏约束、对白策略和反 AI 味策略，但不要复刻样本人物、情节或原文表达。",
            "不要复述设定说明，不要输出 Markdown，不要输出 JSON 以外的解释文字。",
            json.dumps(user_payload, ensure_ascii=False, indent=2),
        ]
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
