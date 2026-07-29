"""正文生成与单章审校共用的质量契约。

把容易被模型忽略的质量要求集中维护，避免生成提示词和审校提示词各说一套。
这些规则来自真实人工标注中的高频问题，不涉及跨章剧情判断。
"""

from __future__ import annotations

from typing import Any


CHAPTER_REVIEW_ISSUE_TYPES = {
    "chapter_logic",
    "scene_continuity",
    "behavior_realism",
    "dialogue_realism",
    "state_continuity",
    "viewpoint_knowledge",
    "character_consistency",
    "factual_plausibility",
    "detail_relevance",
    "meme_fit",
    "prose_rhythm",
    "grammar",
    "ai_style",
    "ending_hook",
}


QUALITY_DIMENSIONS: tuple[dict[str, str], ...] = (
    {
        "key": "scene_continuity",
        "generation_rule": "时间、地点和动作切换必须有可感知的过渡；不能从交通工具、室内或街道直接跳到另一地点。",
        "review_question": "人物如何从上一位置到达下一位置？时间经过、离开和抵达是否在正文中成立？",
    },
    {
        "key": "behavior_realism",
        "generation_rule": "陌生人信任、借物、进入私人空间、留宿和重大决定必须有充分动机、阻力与安全缓冲。",
        "review_question": "角色为什么这样做？现实中的顾虑、成本和拒绝是否被跳过，是否只是为了强推剧情？",
    },
    {
        "key": "dialogue_realism",
        "generation_rule": "对白要符合身份、权力关系、熟悉程度和体面需求；问答必须真正承接，不能前后矛盾或只为交代信息。",
        "review_question": "这句话在真实社会关系中会这样说吗？相邻问答是否回应同一件事，语气是否失真？",
    },
    {
        "key": "state_continuity",
        "generation_rule": "道具、衣物、食物、门锁和人物位置必须遵循取得、使用、归属、放置的完整状态链。",
        "review_question": "物品是否在取得前出现、反复无意义切换、归属说反，或状态与前文动作冲突？",
    },
    {
        "key": "viewpoint_knowledge",
        "generation_rule": "叙述只能写当前视角能够看到、听到、推断或已知的信息；不能无来源知道建筑内部和他人隐私。",
        "review_question": "当前视角凭什么知道这条信息？正文是否给出了观察或推断依据？",
    },
    {
        "key": "character_consistency",
        "generation_rule": "消费、卫生、职业习惯和生活能力要符合人物设定；反常状态必须给出原因，不能把能力短板写成极端邋遢或降智。",
        "review_question": "人物行为是否与收入、职业、性格和生活条件相符？反差是有依据的人设，还是刻板化工具？",
    },
    {
        "key": "factual_plausibility",
        "generation_rule": "职业、技术、校园和日常生活细节必须可行；不确定时宁可写少，不用伪专业细节制造聪明感。",
        "review_question": "技术操作和职业习惯在现实中是否成立？是否出现看似专业、实际错误或过时的细节？",
    },
    {
        "key": "detail_relevance",
        "generation_rule": (
            "环境与具体细节至少服务于空间建立、动作推进、人物塑造、冲突、线索或当前情绪之一；"
            "不要连续铺陈天气、灯光、家具、服装、食物、品牌和生活动作。"
            "手续、费用、登记、采购、通勤、清单和规则不得逐项展开；除非该节点会制造冲突、笑点或后果，"
            "否则只交代必要结果。"
            "删掉后不影响情节、人物、氛围功能或阅读理解的描写应压缩或删除。"
        ),
        "review_question": (
            "环境描写是否过量，是否挤占了人物行动和剧情推进？"
            "删掉这句是否不影响情节、人物、必要氛围或理解？"
            "是否只是随机景物、品牌、数字、生活物件或重复动作的堆积？"
            "是否把手续、费用、采购、登记或规则逐项写全，导致场景没有局势变化？"
        ),
    },
    {
        "key": "meme_fit",
        "generation_rule": (
            "若使用热梗，必须同时满足原词真实含义、说话人与听话人的关系、熟悉程度、"
            "情绪强度和适用场景；正文必须写出必要铺垫、原词、对方回应及剧情或关系后果。"
            "不能只因词面相关或候选数量要求而硬塞。"
        ),
        "review_question": (
            "逐条核对已使用热梗的 meaning、suitable_scenes 与 usage_plan："
            "正文前文是否真实形成 required_setup，原词是否完成对应 speech_act，"
            "双方关系和情绪是否允许这种口吻，后文是否出现 response 与 plot_consequence？"
            "缺少任一关键条件都应判为问题。"
        ),
    },
    {
        "key": "ai_style",
        "generation_rule": "连续两个及以上短句先判断是否属于同一语义链；同链用准确连接合并，只有真实强调或语气需要才分句，禁止残句。避免随机比喻、强行拟人和职业标签比喻；尽量少用破折号（——或—），能用准确标点、连接词、人物动作、语气或停顿表达的，不用破折号代替；不能因为角色从事技术工作，就反复用算法、变量或数学题形容一切。",
        "review_question": "连续短句是否属于应合并的同一语义链，是否存在残句？比喻是否贴合当下感官和人物视角，是否可互换、空泛、职业刻板或明显为模型惯用句？是否出现可以用准确标点、连接词、人物动作、语气或停顿替换的破折号？",
    },
    {
        "key": "ending_hook",
        "generation_rule": "章末必须留下具体的未完成动作、新信息、危险、选择或关系变化；不能只用雨停、天亮、植物、灯光等抒情意象收束。",
        "review_question": "读者是否得到一个明确的继续阅读理由？结尾是剧情钩子，还是只有柔和意象和情绪总结？",
    },
)


def build_generation_quality_contract(chapter_index: int) -> dict[str, Any]:
    """给正文模型的结构化质量门槛。"""
    return {
        "priority": "仅次于人物、世界和剧情硬事实；与字数要求同为交付门槛。",
        "rules": [
            {"dimension": item["key"], "requirement": item["generation_rule"]}
            for item in QUALITY_DIMENSIONS
        ],
        "first_chapter_extra": (
            [
                "首章必须尽快建立主角困境、核心人物吸引力和本书主要矛盾。",
                "首章可以推进快，但每次重大关系跃迁都要有可信的因果台阶。",
                "最后 2-4 段必须落在具体悬念或新矛盾上，不能以抒情晨景结束。",
            ]
            if chapter_index == 1
            else []
        ),
        "silent_preflight": "输出前逐项核对 rules；只修正文，不输出检查过程。",
    }


def build_review_quality_contract(chapter_index: int) -> dict[str, Any]:
    """给审校模型的逐项审读清单和严重度口径。"""
    return {
        "audit_order": [
            {"dimension": item["key"], "question": item["review_question"]}
            for item in QUALITY_DIMENSIONS
        ],
        "first_chapter_extra": (
            "首章必须检查人物关系和叙事状态是否从作品初始状态自然建立、重大关系推进是否过快、章末是否形成明确追读钩子。"
            if chapter_index == 1
            else ""
        ),
        "severity_rubric": {
            "high": "破坏核心因果、人物可信度、技术事实或章末核心吸引力，需要优先修改。",
            "medium": "明显让读者困惑、出戏或感到情节由作者强推。",
            "low": "局部语言、节奏或细节效率问题，但仍应给出可执行修法。",
        },
    }
