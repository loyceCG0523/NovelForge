"""剧情事件的自动网络研究规划与缓存。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.novel import Novel
from app.models.research_source import ResearchSource
from app.models.user import User
from app.services.llm_client import (
    LLMClient,
    LLMConfig,
    LLMRequestCancelledError,
    is_official_deepseek_model,
)
from app.services.tavily_search import (
    build_tavily_config,
    is_allowed_research_source,
    query_overlap_score,
    search_tavily,
)


MAX_SEARCHES_PER_EVENT = 3
MAX_RESULTS_PER_SEARCH = 3


def _event_search_seed(novel: Novel, event_plan: dict[str, Any]) -> str:
    """提取能落到当前事件的短检索锚点，避免查询退化成泛泛写作教程。"""
    chapter_events = [
        str(item.get("core_event") or "").strip()
        for item in (event_plan.get("chapter_plans") or [])[:2]
        if isinstance(item, dict) and str(item.get("core_event") or "").strip()
    ]
    parts = [
        str(event_plan.get("event_title") or "").strip(),
        str(event_plan.get("core_conflict") or "").strip(),
        *chapter_events,
    ]
    seed = " ".join(part for part in parts if part)
    seed = " ".join(seed.split())[:64]
    return seed or f"{novel.genre or '小说'} {novel.premise or novel.title}"[:64]


def _required_creative_queries(novel: Novel, event_plan: dict[str, Any]) -> list[str]:
    """每个事件固定检索情节写法和搞笑口语，模型无权跳过。"""
    seed = _event_search_seed(novel, event_plan)
    return _normalize_queries(
        [
            f"{seed} 小说情节怎么写 冲突升级 反转 场景设计",
            f"{seed} 搞笑对话 真实口语 吐槽 接话 话术",
        ]
    )


def _research_type(query: str) -> str:
    if any(marker in query for marker in ("搞笑", "口语", "吐槽", "接话", "话术")):
        return "comedy_language"
    if any(marker in query for marker in ("情节", "冲突", "反转", "场景设计", "怎么写")):
        return "plot_craft"
    return "factual"


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
                "你是小说事件的网络检索规划器。系统会固定检索两项：当前情节怎么写，以及与场景匹配的搞笑口语和接话方式；"
                "你只需判断是否还需要补充一条准确现实知识查询，例如职业流程、医疗法律技术、机构规则、真实地点细节或历史事实。"
                "仅仅填写了故事年代或地点不构成事实检索理由。每条额外查询只解决一个具体事实，"
                "必须能说明它服务于哪个剧情动作。禁止把年代、地点说明全文拼入查询，禁止使用"
                "‘日常生活 科技 通信 交通 职业制度 常用物品’这类分类词堆砌。输出 JSON，不要解释。"
            ),
        },
        {
            "role": "user",
            "content": (
                "根据下列事件决定是否还需要事实检索。最多给出 1 条、每条不超过 60 字的具体事实查询词。"
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
    """强制两类创作检索；LLM 只可追加一条必要的现实事实查询。"""
    required_queries = _required_creative_queries(novel, event_plan)
    if llm_config is None:
        return required_queries
    try:
        _, parsed = LLMClient(llm_config).complete_json(_build_research_plan_prompt(novel, event_plan))
    except LLMRequestCancelledError:
        raise
    except Exception:
        return required_queries
    factual_queries = (
        _normalize_queries(parsed.get("queries"))[:1]
        if bool(parsed.get("needs_research"))
        else []
    )
    return _normalize_queries([*required_queries, *factual_queries])


def collect_event_research(
    db: Session,
    novel: Novel,
    owner: User | None,
    event_plan: dict[str, Any],
    llm_config: LLMConfig | None,
    search_llm_config: LLMConfig | None = None,
) -> dict[str, Any]:
    """每个事件强制检索情节写法与搞笑口语，并按需补充现实事实。"""
    preferences = owner.preferences if owner else {}
    native_search_config = search_llm_config or llm_config
    use_deepseek_web_search = bool(
        native_search_config
        and is_official_deepseek_model(
            native_search_config.base_url,
            native_search_config.model,
        )
    )
    tavily_config = build_tavily_config(preferences)
    if not use_deepseek_web_search and tavily_config is None:
        return {
            "enabled": False,
            "reason": "missing_tavily_api_key",
            "queries": [],
            "source_ids": [],
        }

    search_provider = "deepseek_web_search" if use_deepseek_web_search else "tavily"

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
        research_type = _research_type(query)
        cached = sorted(
            [
                item
                for item in db.scalars(
                    select(ResearchSource)
                    .where(
                        ResearchSource.novel_id == novel.id,
                        ResearchSource.query == query,
                        ResearchSource.provider == search_provider,
                    )
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
            query_reports.append(
                {
                    "query": query,
                    "research_type": research_type,
                    "status": "cached",
                    "source_count": len(cached),
                }
            )
            continue
        try:
            if use_deepseek_web_search:
                results = LLMClient(native_search_config).search_web(
                    query,
                    max_results=MAX_RESULTS_PER_SEARCH,
                )
            else:
                results = search_tavily(
                    tavily_config,
                    query,
                    max_results=MAX_RESULTS_PER_SEARCH,
                    search_depth="advanced",
                    country=_research_country(novel),
                )
        except LLMRequestCancelledError:
            raise
        except RuntimeError as exc:
            query_reports.append(
                {
                    "query": query,
                    "research_type": research_type,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            continue
        results = sorted(
            [
                item
                for item in results
                if is_allowed_research_source(
                    str(item.get("title") or ""),
                    str(item.get("snippet") or ""),
                    str(item.get("published_at") or ""),
                    float(item.get("score") or 0),
                    str(item.get("domain") or ""),
                )
                and query_overlap_score(
                    query,
                    str(item.get("title") or ""),
                    str(item.get("snippet") or ""),
                )
                >= 0.10
            ],
            key=lambda item: (
                float(item.get("score") or 0) * 0.75
                + query_overlap_score(
                    query,
                    str(item.get("title") or ""),
                    str(item.get("snippet") or ""),
                )
                * 0.25
            ),
            reverse=True,
        )[:MAX_RESULTS_PER_SEARCH]
        sources = []
        for item in results:
            item["payload"] = {
                **(item.get("payload") or {}),
                "source": "event_research",
                "query": query,
                "search_provider": search_provider,
                "research_type": research_type,
            }
            sources.append(
                ResearchSource(
                    novel_id=novel.id,
                    query=query,
                    provider=search_provider,
                    **item,
                )
            )
        db.add_all(sources)
        db.commit()
        for source in sources:
            db.refresh(source)
        source_ids.extend(str(item.id) for item in sources)
        query_reports.append(
            {
                "query": query,
                "research_type": research_type,
                "status": "searched",
                "source_count": len(sources),
            }
        )

    return {
        "enabled": True,
        "reason": "completed" if source_ids else "no_relevant_sources",
        "era": (novel.brief or {}).get("story_era", ""),
        "queries": query_reports,
        "source_ids": source_ids,
        "provider": search_provider,
        "search_limit": MAX_SEARCHES_PER_EVENT,
        "results_per_search_limit": MAX_RESULTS_PER_SEARCH,
    }
