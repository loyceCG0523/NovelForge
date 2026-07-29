"""剧情事件的自动网络研究规划与缓存。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.novel import Novel
from app.models.research_source import ResearchSource
from app.models.user import User
from app.services.llm_client import LLMClient, LLMConfig
from app.services.tavily_search import (
    build_tavily_config,
    is_allowed_research_source,
    query_overlap_score,
    search_tavily,
)


MAX_SEARCHES_PER_EVENT = 3
MAX_RESULTS_PER_SEARCH = 3


def _normalize_queries(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    queries: list[str] = []
    for item in raw:
        query = str(item or "").strip()[:120]
        key = query.casefold()
        if len(query) < 2 or key in seen:
            continue
        seen.add(key)
        queries.append(query)
        if len(queries) >= MAX_SEARCHES_PER_EVENT:
            break
    return queries


def _research_country(novel: Novel) -> str | None:
    """仅在作品明确位于中国时启用国家结果增强，不把中文查询一律限定在中国。"""
    brief = novel.brief or {}
    text = " ".join(
        str(value or "")
        for value in (
            brief.get("story_era"),
            brief.get("story_location"),
            brief.get("worldview"),
        )
    )
    china_signals = ("中国", "大陆", "上海", "北京", "广州", "深圳", "杭州", "成都", "重庆")
    return "china" if any(signal in text for signal in china_signals) else None


def _build_research_plan_prompt(novel: Novel, event_plan: dict[str, Any]) -> list[dict[str, str]]:
    payload = {
        "novel": {"title": novel.title, "genre": novel.genre, "premise": novel.premise, "brief": novel.brief or {}},
        "story_event": {
            "title": event_plan.get("event_title"),
            "goal": event_plan.get("event_goal"),
            "core_conflict": event_plan.get("core_conflict"),
            "chapter_plans": event_plan.get("chapter_plans", []),
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "你是小说事件的现实资料检索规划器。只在某个具体章节动作确实依赖准确现实知识时检索，"
                "例如职业流程、医疗法律技术、机构规则、真实地点细节或历史事实。仅仅填写了故事年代或地点，"
                "不构成检索理由；架空设定、纯情感推进和常识性日常场景一律不检索。每条查询只解决一个具体事实，"
                "必须能说明它服务于哪个剧情动作。禁止把年代、地点说明全文拼入查询，禁止使用"
                "‘日常生活 科技 通信 交通 职业制度 常用物品’这类分类词堆砌。输出 JSON，不要解释。"
            ),
        },
        {
            "role": "user",
            "content": (
                "根据下列事件决定是否需要检索。最多给出 3 条、每条不超过 60 字的具体事实查询词。"
                "例如章节确实涉及裁员手续时可查‘2025年上海互联网公司裁员经济补偿流程’，"
                "不要查‘2025上海社会生活背景’。\n"
                "格式：{\"needs_research\": true, \"queries\": [\"查询词\"]}\n"
                + json.dumps(payload, ensure_ascii=False)
            ),
        },
    ]


def plan_event_research_queries(
    novel: Novel,
    event_plan: dict[str, Any],
    llm_config: LLMConfig | None,
) -> list[str]:
    """由 LLM 判断现实资料需求；没有 LLM 配置时保守地不自动联网。"""
    if llm_config is None:
        return []
    try:
        _, parsed = LLMClient(llm_config).complete_json(_build_research_plan_prompt(novel, event_plan))
    except Exception:
        return []
    return _normalize_queries(parsed.get("queries")) if bool(parsed.get("needs_research")) else []


def collect_event_research(
    db: Session,
    novel: Novel,
    owner: User | None,
    event_plan: dict[str, Any],
    llm_config: LLMConfig | None,
) -> dict[str, Any]:
    """按事件规划少量现实资料查询；相同查询优先命中作品缓存。"""
    preferences = owner.preferences if owner else {}
    web_search = (preferences or {}).get("web_search") or {}
    tavily_config = build_tavily_config(preferences)
    if tavily_config is None:
        reason = "disabled_by_user" if web_search.get("enabled") is False else "missing_tavily_api_key"
        return {
            "enabled": False,
            "reason": reason,
            "queries": [],
            "source_ids": [],
        }

    event_queries = plan_event_research_queries(novel, event_plan, llm_config)
    queries = _normalize_queries(event_queries)
    if not queries:
        return {
            "enabled": True,
            "reason": "not_needed",
            "queries": [],
            "source_ids": [],
        }

    source_ids: list[str] = []
    query_reports: list[dict[str, Any]] = []
    for query in queries[:MAX_SEARCHES_PER_EVENT]:
        cached = sorted(
            [
                item
                for item in db.scalars(
                    select(ResearchSource)
                    .where(ResearchSource.novel_id == novel.id, ResearchSource.query == query)
                    .order_by(ResearchSource.created_at.desc())
                    .limit(MAX_RESULTS_PER_SEARCH * 3)
                ).all()
                if is_allowed_research_source(
                    item.title,
                    item.snippet,
                    item.published_at,
                    item.score,
                    item.domain,
                )
                and query_overlap_score(query, item.title, item.snippet) >= 0.10
            ],
            key=lambda item: (
                float(item.score or 0) * 0.75
                + query_overlap_score(query, item.title, item.snippet) * 0.25
            ),
            reverse=True,
        )[:MAX_RESULTS_PER_SEARCH]
        if cached:
            source_ids.extend(str(item.id) for item in cached)
            query_reports.append({"query": query, "status": "cached", "source_count": len(cached)})
            continue
        try:
            results = search_tavily(
                tavily_config,
                query,
                max_results=MAX_RESULTS_PER_SEARCH,
                search_depth="advanced",
                country=_research_country(novel),
            )
        except RuntimeError as exc:
            query_reports.append({"query": query, "status": "failed", "error": str(exc)})
            continue
        sources = []
        for item in results:
            item["payload"] = {**(item.get("payload") or {}), "source": "event_research", "query": query}
            sources.append(ResearchSource(novel_id=novel.id, query=query, provider="tavily", **item))
        db.add_all(sources)
        db.commit()
        for source in sources:
            db.refresh(source)
        source_ids.extend(str(item.id) for item in sources)
        query_reports.append({"query": query, "status": "searched", "source_count": len(sources)})

    return {
        "enabled": True,
        "reason": "completed" if source_ids else "no_relevant_sources",
        "era": (novel.brief or {}).get("story_era", ""),
        "queries": query_reports,
        "source_ids": source_ids,
        "search_limit": MAX_SEARCHES_PER_EVENT,
        "results_per_search_limit": MAX_RESULTS_PER_SEARCH,
    }
