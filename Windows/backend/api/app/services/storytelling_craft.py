"""把既定大纲转成可写、可审校的场景执行约束。"""

from __future__ import annotations

from typing import Any


def build_scene_execution_schema() -> dict[str, Any]:
    """事件规划阶段每章都必须填写的执行层；不预写可复制的正文台词。"""
    return {
        "entry_pressure": "开场正在发生的麻烦、期限、误判或未完成动作；不得从天气、起床或背景介绍开始",
        "protagonist_want": "主角在本章此刻想立刻得到、阻止或隐瞒什么",
        "opposing_want": "对手戏人物想得到什么，为什么不会顺着主角",
        "tactic_turns": [
            {
                "actor": "采取行动的人",
                "tactic": "他为达到目的而采取的具体动作或语言策略",
                "counterforce": "另一人、现实条件或新信息怎样顶回来",
                "local_change": "这一轮结束后权力、信息、关系、资源、风险或决定发生什么变化",
            }
        ],
        "dialogue_pressure": {
            "surface_topic": "嘴上正在谈什么",
            "hidden_stakes": "双方真正怕失去或想确认什么",
            "decisive_exchange": "哪一轮试探、回避、拒绝或反问改变局面；只描述功能，不预写完整台词",
        },
        "pov_reaction_chain": {
            "observable_detail": "当前视角先看到或听到的具体细节，不直接宣判他人内心",
            "biased_interpretation": "视角人物受愿望、误判、自尊或经验影响而得出的带人物私心的误读或有限理解",
            "immediate_impulse": "这个理解让人物此刻想掩饰、试探、逃避、逞强或靠近什么",
            "visible_response": "最终落到现场、能被他人观察并接住的动作、表情、停顿或一句话",
        },
        "dialogue_reaction_chain": {
            "trigger": "触发本轮对话的具体一句话、动作或新信息的功能",
            "evasion_or_misread": "听者怎样按自己的性格误读、回避、嘴硬或抓错重点",
            "countermove": "另一方怎样追问、拆穿、顺势利用或被带偏",
            "local_consequence": "这一轮怎样改变关系距离、信息、决定或接下来的行动",
        },
        "voice_contrast": [
            {
                "character": "人物",
                "speech_rhythm": "短促、绕弯、抢话、慢半拍等稳定口语节奏",
                "evasion_habit": "被戳中时如何转移、嘴硬、反问或装没听懂",
                "emotion_leak": "情绪会从哪个小动作、口误或多余解释中漏出来",
            }
        ],
        "absurd_comedy_mode": {
            "enabled": "喜剧场景按需启用，非喜剧或严肃节点可关闭",
            "mechanisms": ["答非所问", "错位联想", "一本正经跑偏", "因果倒置", "概念偷换", "过度字面理解"],
            "character_anchor": "离谱表达来自谁的性格、误判、自尊或即时困境",
            "reality_landing": "由谁的动作、沉默、纠正或后果把离谱逻辑落回现场",
        },
        "ending_residual_force": {
            "last_change": "章末最后一个不可撤销或暂时无法忽略的变化",
            "reader_question": "读者此刻必须追问的具体问题",
            "next_chapter_first_beat": "下一章开头立刻承接的动作、回应或后果",
        },
    }


def build_storytelling_craft_contract() -> dict[str, Any]:
    """正文生成与审校共享的原创写作执行契约。"""
    return {
        "outline_to_scene": (
            "大纲只规定目的地，不规定正文的讲解顺序。每个主要场景都按“即时目标→阻力→策略→反制→局部变化”推进；"
            "若一段场景结束后，权力、信息、关系、资源、风险和决定均未变化，应删除、合并或压缩。"
        ),
        "reader_pull": (
            "吸引力来自人物为了目标做选择、被顶回来并承担后果，不靠凭空增加事故。开场直接承接压力；"
            "章末留下仍在起作用的后果，并让下一章第一拍能够立即承接。"
        ),
        "dialogue": (
            "对白先确定双方当下目的、隐藏利害和不能直说的东西。每轮发言应承担试探、施压、回避、拒绝、误导或决定之一；"
            "至少一轮问答必须改变信息、立场、关系或下一步行动。人物可以听懂却装没听懂，不必把话说全。"
            "优先形成‘话/动作刺激→带偏见的理解或回避→可见反应→对方接招→局面变化’，而不是标准问答或轮流说明。"
        ),
        "human_voice": (
            "不要让人物和旁白替作者作完整、正确、礼貌的总结。把抽象判断改为可见动作、物件、停顿、口误和反应；"
            "不同人物要有不同说话节奏、回避习惯和情绪泄漏方式。长短句、打断和动作插入应由现场压力决定。"
            "近距离视角先写人物能观察到的细节，再允许他带着私心误读、自问、自我辩护，最后落成别人看得见的反应；"
            "不要由旁白越俎代庖给出标准答案。每段只保留一个人物主体：台词可连同说话者自己的动作，"
            "一旦转写听者或另一人物的动作、心理、判断或发言，必须另起一段。"
        ),
        "absurd_comedy": (
            "这里的“抽象”取网络语义：人物在对话或心理活动中出现答非所问、错位联想、一本正经跑偏、因果倒置、"
            "概念偷换或过度字面理解，形成离谱但可回想出人物逻辑的喜剧。每个抽象节拍必须有现实铺垫、人物锚点、"
            "短暂逻辑偏移、他人反应和局面后果；不得随机胡言乱语、全员同频发疯或用旁白解释笑点。"
        ),
        "comedy_density": (
            "笑点优先嵌入攻防：现实铺垫→一次自洽的逻辑偏移→人物一本正经地执行→对方反应→关系或局面后果。"
            "优先使用人物自利解释、抓错重点、过度字面理解、一本正经补救、前文回旋镖和身份反转；"
            "同一场景不叠加过多机制；热梗、职业黑话、随机比喻和孤立段子不算有效喜剧节拍。"
        ),
        "serial_rhythm": (
            "一章尽量围绕一个正在发生的问题和一组核心对手戏推进。说明只在人物眼下需要知道时插入，并尽快回到动作或回应。"
            "短段用于反应、转折和包袱落点，中段用于完整因果；禁止把每个念头都机械切成单行，也禁止长段连续解释。"
        ),
        "romantic_or_emotional_charge": (
            "关系张力优先落在称呼变化、视线停留、距离、步速、递还物品、等待、欲言又止等可见证据上；"
            "由视角人物作有限甚至错误的解释，让读者比人物多懂半步，禁止旁白直接宣布暧昧、心动或感动。"
        ),
        "chapter_handoff": (
            "章末优先停在尚未完成的回应、动作、新信息或会立刻改变下一步的错误判断上；"
            "下一章先兑现这一拍，再切时间地点或补背景。禁止章末总结后、下一章另起炉灶。"
        ),
    }


def normalize_scene_execution(value: Any) -> dict[str, Any]:
    """限制执行计划体积，同时保留正文 Agent 真正需要的字段。"""
    value = value if isinstance(value, dict) else {}
    dialogue = value.get("dialogue_pressure")
    dialogue = dialogue if isinstance(dialogue, dict) else {}
    pov_reaction = value.get("pov_reaction_chain")
    pov_reaction = pov_reaction if isinstance(pov_reaction, dict) else {}
    dialogue_reaction = value.get("dialogue_reaction_chain")
    dialogue_reaction = dialogue_reaction if isinstance(dialogue_reaction, dict) else {}
    absurd = value.get("absurd_comedy_mode")
    absurd = absurd if isinstance(absurd, dict) else {}
    ending = value.get("ending_residual_force")
    ending = ending if isinstance(ending, dict) else {}
    return {
        "entry_pressure": str(value.get("entry_pressure") or "").strip()[:500],
        "protagonist_want": str(value.get("protagonist_want") or "").strip()[:500],
        "opposing_want": str(value.get("opposing_want") or "").strip()[:500],
        "tactic_turns": [
            {
                key: str(item.get(key) or "").strip()[:400]
                for key in ("actor", "tactic", "counterforce", "local_change")
            }
            for item in (value.get("tactic_turns") or [])[:6]
            if isinstance(item, dict)
        ],
        "dialogue_pressure": {
            key: str(dialogue.get(key) or "").strip()[:500]
            for key in ("surface_topic", "hidden_stakes", "decisive_exchange")
        },
        "pov_reaction_chain": {
            key: str(pov_reaction.get(key) or "").strip()[:500]
            for key in (
                "observable_detail",
                "biased_interpretation",
                "immediate_impulse",
                "visible_response",
            )
        },
        "dialogue_reaction_chain": {
            key: str(dialogue_reaction.get(key) or "").strip()[:500]
            for key in (
                "trigger",
                "evasion_or_misread",
                "countermove",
                "local_consequence",
            )
        },
        "voice_contrast": [
            {
                key: str(item.get(key) or "").strip()[:300]
                for key in ("character", "speech_rhythm", "evasion_habit", "emotion_leak")
            }
            for item in (value.get("voice_contrast") or [])[:6]
            if isinstance(item, dict)
        ],
        "absurd_comedy_mode": {
            "enabled": absurd.get("enabled", False),
            "mechanisms": [
                str(item).strip()[:80]
                for item in (absurd.get("mechanisms") or [])[:2]
                if str(item).strip()
            ],
            "character_anchor": str(absurd.get("character_anchor") or "").strip()[:400],
            "reality_landing": str(absurd.get("reality_landing") or "").strip()[:400],
        },
        "ending_residual_force": {
            key: str(ending.get(key) or "").strip()[:500]
            for key in ("last_change", "reader_question", "next_chapter_first_beat")
        },
    }


def scene_execution_is_complete(value: Any) -> bool:
    """规划质量门禁：至少两轮有效攻防，且章末余力可被下一章接住。"""
    value = normalize_scene_execution(value)
    turns = value["tactic_turns"]
    dialogue = value["dialogue_pressure"]
    pov_reaction = value["pov_reaction_chain"]
    dialogue_reaction = value["dialogue_reaction_chain"]
    ending = value["ending_residual_force"]
    return (
        bool(value["entry_pressure"])
        and bool(value["protagonist_want"])
        and bool(value["opposing_want"])
        and len(turns) >= 2
        and all(all(turn.get(key) for key in ("actor", "tactic", "counterforce", "local_change")) for turn in turns[:2])
        and all(dialogue.get(key) for key in ("surface_topic", "hidden_stakes", "decisive_exchange"))
        and all(
            pov_reaction.get(key)
            for key in (
                "observable_detail",
                "biased_interpretation",
                "immediate_impulse",
                "visible_response",
            )
        )
        and all(
            dialogue_reaction.get(key)
            for key in (
                "trigger",
                "evasion_or_misread",
                "countermove",
                "local_consequence",
            )
        )
        and all(ending.get(key) for key in ("last_change", "reader_question", "next_chapter_first_beat"))
    )
