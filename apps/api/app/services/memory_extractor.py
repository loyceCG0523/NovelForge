"""从章节正文抽取结构化记忆。

这个服务负责把一章正文转成可查询的长期事实，例如人物状态、地点、道具、关系变化、
世界规则和关键事件。它是 ChapterContextBuilder 的上游，决定后续章节能否稳定承接前文。
"""

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.memory_item import MemoryItem
from app.models.novel import Novel
from app.services.llm_client import LLMClient, LLMConfig


AUTO_MEMORY_SOURCES = {"memory_extractor", "memory_extractor_fallback"}
ALLOWED_MEMORY_TYPES = {
    "character",
    "relationship",
    "location",
    "item",
    "event",
    "timeline",
    "world_rule",
    "chapter_summary",
}
ROLE_PREFIXES = ("男主", "女主", "主角", "男生", "女生", "同桌", "班长", "学生")
RELATIONSHIP_SEPARATORS = ("-", "—", "－", "和", "与", "、", "/", "及")
EVENT_SUFFIXES = ("宣布", "通知", "消息", "开始", "安排")
CHARACTER_PROFILE_FIELDS = (
    "identity",
    "age",
    "grade",
    "appearance",
    "personality",
    "family",
    "ability",
    "goal",
    "secret",
    "relationship",
    "stable_state",
)
CHARACTER_STABLE_KEYWORDS = (
    "学生",
    "老师",
    "班主任",
    "年级",
    "高一",
    "高二",
    "高三",
    "年龄",
    "性格",
    "外表",
    "家庭",
    "父母",
    "成绩",
    "学霸",
    "校花",
    "朋友",
    "同桌",
    "喜欢",
    "擅长",
    "目标",
    "梦想",
    "认真",
    "冷淡",
    "敏感",
    "开朗",
    "内向",
)
CHARACTER_EVENT_KEYWORDS = (
    "宣布",
    "报名",
    "招新",
    "询问",
    "递给",
    "送给",
    "借给",
    "帮助",
    "助攻",
    "受伤",
    "扭伤",
    "比赛",
    "考试",
    "补课",
    "值日",
    "本章",
    "今天",
    "明天",
    "周五",
)


def normalize_entity_name(entity_name: str) -> str:
    """统一实体名，避免“女主许清禾”和“许清禾”被当成两个实体。"""
    name = (entity_name or "").strip()
    for prefix in ROLE_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix):].strip(" ：:，,")
    return name


def normalize_memory_entity_name(memory_type: str, entity_name: str) -> str:
    """按记忆类型生成更严格的实体名，减少人物、关系、道具和事件重复。"""
    name = normalize_entity_name(entity_name)
    if memory_type == "relationship":
        return normalize_relationship_name(name)
    if memory_type == "item":
        return normalize_item_name(name)
    if memory_type == "event":
        return normalize_event_name(name)
    return name


def normalize_relationship_name(entity_name: str) -> str:
    """把“陈野和许清禾”“陈野-许清禾”统一成“陈野-许清禾”。"""
    name = entity_name.strip()
    for separator in RELATIONSHIP_SEPARATORS:
        if separator in name:
            parts = [normalize_entity_name(part) for part in name.split(separator)]
            people = sorted({part for part in parts if part})
            if len(people) >= 2:
                return "-".join(people[:2])
    return name


def normalize_item_name(entity_name: str) -> str:
    """把“许清禾的数学笔记本”“数学笔记（许清禾给陈野）”归到稳定物品名。"""
    name = re.sub(r"[（(].*?[）)]", "", entity_name).strip()
    if "的" in name:
        owner, item_name = name.split("的", 1)
        if 1 <= len(owner) <= 4 and len(item_name) >= 2:
            name = item_name
    if name.endswith("笔记本"):
        name = name[:-1]
    return name


def normalize_event_name(entity_name: str) -> str:
    """把“社团招新宣布”“社团招新开始”归到核心事件名。"""
    name = entity_name.strip()
    for suffix in EVENT_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix) + 1:
            return name[: -len(suffix)]
    return name


def memory_group_key(memory: MemoryItem) -> tuple[str, str]:
    """结构化记忆的规范分组键。"""
    return (
        memory.memory_type,
        normalize_memory_entity_name(memory.memory_type, memory.entity_name),
    )


def _importance_score(memory: MemoryItem) -> int:
    """将 high/medium/low 转成排序分数。"""
    importance = str((memory.payload or {}).get("importance") or "").lower()
    return {"high": 3, "medium": 2, "low": 1}.get(importance, 0)


def _importance_label(score: int) -> str:
    """把重要性分数转回标签。"""
    return {3: "high", 2: "medium", 1: "low"}.get(score, "")


def _dedupe_texts(texts: list[str]) -> list[str]:
    """保留顺序去重，并清理空文本。"""
    deduped = []
    for text in texts:
        value = str(text or "").strip()
        if value and value not in deduped:
            deduped.append(value)
    return deduped


def _is_stable_character_fact(text: str) -> bool:
    """判断一条旧人物摘要是否更像人物档案，而不是一次性事件。"""
    if not text:
        return False
    has_stable_signal = any(keyword in text for keyword in CHARACTER_STABLE_KEYWORDS)
    has_event_signal = any(keyword in text for keyword in CHARACTER_EVENT_KEYWORDS)
    return has_stable_signal and not has_event_signal


def _build_character_payload(items: list[MemoryItem], summaries: list[str], statuses: list[str]) -> dict:
    """把人物记忆合并为档案型信息，而不是事件流水。"""
    profile: dict[str, list[str]] = {field: [] for field in CHARACTER_PROFILE_FIELDS}
    for item in items:
        payload = item.payload or {}
        nested_profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
        for field in CHARACTER_PROFILE_FIELDS:
            value = payload.get(field) or nested_profile.get(field)
            if isinstance(value, list):
                profile[field].extend(str(part) for part in value if part)
            elif value:
                profile[field].append(str(value))

    compact_profile = {
        field: _dedupe_texts(values)[:3]
        for field, values in profile.items()
        if _dedupe_texts(values)
    }
    stable_summaries = [summary for summary in summaries if _is_stable_character_fact(summary)]

    profile_fragments = []
    for label, field in [
        ("身份", "identity"),
        ("年龄", "age"),
        ("年级", "grade"),
        ("外貌", "appearance"),
        ("性格", "personality"),
        ("家庭", "family"),
        ("能力", "ability"),
        ("目标", "goal"),
        ("秘密", "secret"),
        ("关系", "relationship"),
        ("状态", "stable_state"),
    ]:
        values = compact_profile.get(field)
        if values:
            profile_fragments.append(f"{label}：{'；'.join(values)}")

    summary = "；".join(profile_fragments) if profile_fragments else ""
    if not summary and stable_summaries:
        summary = stable_summaries[0]
    if not summary:
        summary = "人物档案信息不足，等待后续章节补充身份、性格、家庭、能力等稳定设定。"

    return {
        "summary": summary,
        "profile": compact_profile,
        "recent_facts": (profile_fragments or stable_summaries)[:5],
        "current_status": statuses[0] if statuses else "",
    }


def build_consolidated_memory_context(memories: list[MemoryItem], limit: int | None = 30) -> list[dict]:
    """把原始逐章记忆合并成实体级记忆组。

    原始记录保留在数据库用于追溯；前端展示和模型输入默认使用这个合并视图。
    """
    grouped: dict[tuple[str, str], list[MemoryItem]] = {}
    for memory in memories:
        key = memory_group_key(memory)
        if key[1]:
            grouped.setdefault(key, []).append(memory)

    consolidated = []
    for (memory_type, entity_name), items in grouped.items():
        ordered_items = sorted(
            items,
            key=lambda item: (item.chapter_index_end or item.chapter_index_start or 0, item.updated_at),
            reverse=True,
        )
        representative = ordered_items[0]
        summaries = []
        statuses = []
        source_memory_ids = []
        source_chapters = []
        for item in ordered_items:
            payload = item.payload or {}
            summary = payload.get("summary") or payload.get("status") or payload.get("evidence")
            if summary and summary not in summaries:
                summaries.append(summary)
            status = payload.get("status")
            if status and status not in statuses:
                statuses.append(status)
            source_memory_ids.append(str(item.id))
            if item.chapter_index_start:
                source_chapters.append(item.chapter_index_start)

        chapter_indices = [
            index
            for item in ordered_items
            for index in (item.chapter_index_start, item.chapter_index_end)
            if index is not None
        ]
        importance = max((_importance_score(item) for item in ordered_items), default=0)

        payload_base = (
            _build_character_payload(ordered_items, summaries, statuses)
            if memory_type == "character"
            else {
                "summary": summaries[0] if summaries else "",
                "recent_facts": summaries[:5],
                "current_status": statuses[0] if statuses else "",
            }
        )

        consolidated.append(
            {
                "id": str(representative.id),
                "novel_id": str(representative.novel_id),
                "memory_type": memory_type,
                "entity_name": entity_name,
                "chapter_index_start": min(chapter_indices) if chapter_indices else None,
                "chapter_index_end": max(chapter_indices) if chapter_indices else None,
                "payload": {
                    **payload_base,
                    "importance": _importance_label(importance),
                    "source_chapters": sorted(set(source_chapters)),
                    "source_memory_count": len(ordered_items),
                    "source_memory_ids": source_memory_ids,
                    "consolidated": True,
                },
            }
        )

    consolidated.sort(
        key=lambda item: (
            {"high": 3, "medium": 2, "low": 1}.get(item["payload"].get("importance"), 0),
            item["chapter_index_end"] or 0,
        ),
        reverse=True,
    )
    return consolidated[:limit] if limit else consolidated


def delete_memory_group(db: Session, novel_id: object, memory_id: object) -> int:
    """按规范分组删除一组记忆，避免只删掉代表卡片后重复项仍然存在。"""
    target = db.get(MemoryItem, memory_id)
    if target is None or target.novel_id != novel_id:
        return 0

    target_key = memory_group_key(target)
    memories = db.scalars(select(MemoryItem).where(MemoryItem.novel_id == novel_id)).all()
    deleted_count = 0
    for memory in memories:
        if memory_group_key(memory) == target_key:
            db.delete(memory)
            deleted_count += 1
    return deleted_count


def build_memory_extraction_prompt(novel: Novel, chapter: Chapter) -> list[dict[str, str]]:
    """构造结构化记忆抽取 prompt，要求模型返回稳定 JSON。"""
    brief = novel.brief or {}
    return [
        {
            "role": "system",
            "content": (
                "你是 NovelForge 的结构化记忆抽取器。你的任务不是润色小说，"
                "而是从章节正文中提取后续创作必须保持一致的事实。"
                "只输出 JSON，不要输出解释、Markdown 或代码块。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请从以下章节中抽取结构化记忆。\n"
                "同一人物、地点、道具必须使用稳定实体名，例如写“许清禾”，不要写“女主许清禾”。\n"
                "同一事件在不同句子中重复出现时，只保留一条最完整的事实。\n"
                "人物记忆只能记录稳定档案信息，例如身份、年龄、年级、外貌、性格、家庭、能力、目标、秘密、长期状态；"
                "不要把某次比赛、送东西、补课、受伤、报名等一次性动作写进 character，应写入 event 或 relationship。\n"
                "输出格式必须为：\n"
                "{\n"
                '  "memories": [\n'
                "    {\n"
                '      "memory_type": "character|relationship|location|item|event|timeline|world_rule",\n'
                '      "entity_name": "实体名称",\n'
                '      "payload": {\n'
                '        "summary": "一句话事实摘要；character 类型必须是人物档案摘要，不能是事件摘要",\n'
                '        "profile": {\n'
                '          "identity": "身份/角色，例如高三二班学生",\n'
                '          "age": "年龄，不明确则留空",\n'
                '          "grade": "年级/班级，不明确则留空",\n'
                '          "appearance": "外貌特征，不明确则留空",\n'
                '          "personality": "稳定性格，不明确则留空",\n'
                '          "family": "家庭信息，不明确则留空",\n'
                '          "ability": "能力/成绩/特长，不明确则留空",\n'
                '          "goal": "长期目标，不明确则留空",\n'
                '          "secret": "秘密/隐情，不明确则留空",\n'
                '          "relationship": "长期人际关系，不明确则留空",\n'
                '          "stable_state": "长期状态，不明确则留空"\n'
                "        },\n"
                '        "evidence": "对应正文依据",\n'
                '        "status": "当前状态",\n'
                '        "importance": "low|medium|high"\n'
                "      }\n"
                "    }\n"
                "  ]\n"
                "}\n\n"
                f"作品标题：{novel.title}\n"
                f"题材：{novel.genre or '未设置'}\n"
                f"作品设定：{brief}\n"
                f"章节序号：第 {chapter.chapter_index} 章\n"
                f"章节标题：{chapter.title or '未命名章节'}\n"
                f"章节摘要：{chapter.summary or '暂无摘要'}\n"
                f"章节正文：\n{chapter.content or ''}"
            ),
        },
    ]


def normalize_memory_items(raw_items: Any, chapter_index: int) -> list[dict[str, Any]]:
    """清洗模型输出，避免脏字段直接进入记忆表。"""
    if not isinstance(raw_items, list):
        return []

    normalized: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue

        memory_type = str(raw.get("memory_type") or "event").strip()
        if memory_type not in ALLOWED_MEMORY_TYPES:
            memory_type = "event"

        entity_name = normalize_memory_entity_name(memory_type, str(raw.get("entity_name") or ""))
        if not entity_name:
            continue

        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        payload = {
            **payload,
            "source": "memory_extractor",
            "auto_generated": True,
        }

        normalized.append(
            {
                "memory_type": memory_type,
                "entity_name": entity_name[:120],
                "chapter_index_start": chapter_index,
                "chapter_index_end": chapter_index,
                "payload": payload,
            }
        )

    return normalized[:20]


def build_fallback_memory(chapter: Chapter) -> list[dict[str, Any]]:
    """没有 LLM 配置时，至少把章节摘要写成可检索记忆。"""
    summary = chapter.summary or (chapter.content or "")[:160]
    if not summary:
        return []
    return [
        {
            "memory_type": "chapter_summary",
            "entity_name": f"第 {chapter.chapter_index} 章摘要",
            "chapter_index_start": chapter.chapter_index,
            "chapter_index_end": chapter.chapter_index,
            "payload": {
                "source": "memory_extractor_fallback",
                "auto_generated": True,
                "summary": summary,
                "chapter_title": chapter.title or "未命名章节",
            },
        }
    ]


def extract_chapter_memories(novel: Novel, chapter: Chapter, llm_config: LLMConfig | None) -> list[dict[str, Any]]:
    """抽取单章结构化记忆；无 API Key 时走摘要级兜底。"""
    if llm_config is None:
        return build_fallback_memory(chapter)

    _, parsed = LLMClient(llm_config).complete_json(build_memory_extraction_prompt(novel, chapter))
    return normalize_memory_items(parsed.get("memories"), chapter.chapter_index)


def sync_chapter_memories(
    db: Session,
    novel: Novel,
    chapter: Chapter,
    llm_config: LLMConfig | None,
) -> list[MemoryItem]:
    """同步单章自动记忆：先清理本章旧自动记忆，再写入新抽取结果。"""
    existing_items = db.scalars(
        select(MemoryItem).where(
            MemoryItem.novel_id == novel.id,
            MemoryItem.chapter_index_start == chapter.chapter_index,
        )
    ).all()
    for item in existing_items:
        if (item.payload or {}).get("source") in AUTO_MEMORY_SOURCES:
            db.delete(item)

    records = extract_chapter_memories(novel=novel, chapter=chapter, llm_config=llm_config)
    memory_items = [MemoryItem(novel_id=novel.id, **record) for record in records]
    db.add_all(memory_items)
    db.commit()
    for item in memory_items:
        db.refresh(item)
    return memory_items


def purge_chapter_memories(db: Session, novel_id: object, chapter_index: int) -> int:
    """删除章节被移除后不应继续参与上下文的结构化记忆。

    自动抽取记忆一定要删除；手动创建但明确绑定到该章节的记忆也一并删除。
    跨章节或全局记忆不在这里处理，避免误删用户维护的长期设定。
    """
    scoped_items = db.scalars(
        select(MemoryItem).where(
            MemoryItem.novel_id == novel_id,
            MemoryItem.chapter_index_start == chapter_index,
        )
    ).all()

    deleted_count = 0
    for item in scoped_items:
        payload = item.payload or {}
        is_auto_memory = payload.get("source") in AUTO_MEMORY_SOURCES or payload.get("auto_generated") is True
        is_exact_chapter_memory = item.chapter_index_end in (None, chapter_index)
        if is_auto_memory or is_exact_chapter_memory:
            db.delete(item)
            deleted_count += 1
    return deleted_count
