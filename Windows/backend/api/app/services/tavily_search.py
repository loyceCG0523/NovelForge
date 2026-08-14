"""Tavily 网络检索适配器。

搜索服务独立于 LLM 提供商，任何 OpenAI-compatible 模型都只接收规范化后的资料摘要。
"""

from __future__ import annotations

from datetime import date, timedelta
from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.desktop_secrets import reveal_preference_secret


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
MAX_SNIPPET_CHARS = 1200
RECENT_YEARS_DAYS = 365 * 3
RECENT_MONTH_DAYS = 30
MIN_TAVILY_RELEVANCE_SCORE = 0.50
MIN_QUERY_OVERLAP_SCORE = 0.10
MAX_RESULTS_PER_DOMAIN = 2
BLOCKED_RESEARCH_DOMAINS = {
    "classic-blog.udn.com",
}
BOILERPLATE_SIGNALS = (
    "账号密码登录",
    "手机号注册登录",
    "忘记密码",
    "打开微信",
    "分享到我的朋友圈",
    "Image 20",
    "Image 21",
)
# 这些 Unicode 段能可靠识别出不属于简体中文或英语的主要文字系统。
OTHER_LANGUAGE_SCRIPT_RE = re.compile(
    r"[\u3040-\u30ff\u0400-\u052f\u0600-\u06ff\u0900-\u097f\u0e00-\u0e7f\u1100-\u11ff\uac00-\ud7af]"
)
TRADITIONAL_ONLY_CHARS = set("萬與為於後國們個這裡對發現實關係應還會進過當說讓開學業東風畫話體龍醫藥網證變")
SIMPLIFIED_ONLY_CHARS = set("万与为于后国们个这里对发现实关系应还会进过当说让开学业东风画话体龙医药网证变")
QUERY_GENERIC_UNITS = {
    "一个",
    "什么",
    "如何",
    "怎么",
    "相关",
    "介绍",
    "最近",
    "今年",
    "当前",
    "实际",
    "使用",
    "场景",
    "中国",
    "中文",
}


@dataclass
class TavilyConfig:
    api_key: str


def build_tavily_config(preferences: dict | None) -> TavilyConfig | None:
    config = (preferences or {}).get("web_search") or {}
    if config.get("enabled") is False:
        return None
    api_key = reveal_preference_secret(config.get("tavily_api_key") or "")
    return TavilyConfig(api_key=api_key) if api_key else None


def recent_source_start_date(days: int = RECENT_YEARS_DAYS) -> str:
    """返回滚动时间窗的检索下限，使用 Tavily 支持的 YYYY-MM-DD 格式。"""
    return (date.today() - timedelta(days=max(1, int(days)))).isoformat()


def classify_target_language(title: str, snippet: str) -> str:
    """仅接受简体中文或英文；无法可靠识别时宁可过滤，避免把其它语言交给小说 Agent。"""
    text = f"{title}\n{snippet}".strip()
    if not text or OTHER_LANGUAGE_SCRIPT_RE.search(text):
        return ""

    han_count = sum("\u4e00" <= char <= "\u9fff" for char in text)
    latin_count = sum(("a" <= char.lower() <= "z") for char in text)
    if han_count >= 2 and han_count >= latin_count * 0.4:
        traditional_count = sum(char in TRADITIONAL_ONLY_CHARS for char in text)
        simplified_count = sum(char in SIMPLIFIED_ONLY_CHARS for char in text)
        if traditional_count > simplified_count:
            return ""
        return "zh-Hans"
    if latin_count >= 5 and latin_count >= han_count * 2:
        return "en"
    return ""


def is_allowed_research_source(
    title: str,
    snippet: str,
    published_at: str,
    score: float | None = None,
    domain: str = "",
    start_date: str | None = None,
    min_score: float = MIN_TAVILY_RELEVANCE_SCORE,
    allowed_languages: set[str] | None = None,
    required_any_terms: tuple[str, ...] = (),
) -> bool:
    """用于 Tavily 新结果与历史缓存，统一执行语言和近三年约束。"""
    host = str(domain or "").strip().lower().split(":", 1)[0]
    if any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_RESEARCH_DOMAINS):
        return False
    if score is not None:
        try:
            if float(score) < min_score:
                return False
        except (TypeError, ValueError):
            return False
    combined = f"{title}\n{snippet}"
    if sum(signal in combined for signal in BOILERPLATE_SIGNALS) >= 2:
        return False
    language = classify_target_language(title, snippet)
    if not language or (allowed_languages is not None and language not in allowed_languages):
        return False
    normalized_combined = re.sub(r"\s+", "", combined).casefold()
    if required_any_terms and not any(
        re.sub(r"\s+", "", term).casefold() in normalized_combined
        for term in required_any_terms
    ):
        return False
    value = str(published_at or "").strip()
    if not value:
        # Tavily 已通过 start_date 按发布时间或更新时间过滤；部分结果不会回传日期字段。
        return True
    try:
        return date.fromisoformat(value[:10]) >= date.fromisoformat(start_date or recent_source_start_date())
    except ValueError:
        return True


def _query_units(text: str) -> set[str]:
    """提取中英文查询锚点；中文使用双字片段，避免依赖额外分词服务。"""
    units = {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._+-]{1,}", text or "")
    }
    for run in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        units.update(run[index : index + 2] for index in range(max(0, len(run) - 1)))
    return {unit for unit in units if unit not in QUERY_GENERIC_UNITS}


def query_overlap_score(query: str, title: str, snippet: str) -> float:
    """计算来源文本对查询锚点的覆盖率，供 Tavily 分数之后的本地二次重排。"""
    query_units = _query_units(query)
    if not query_units:
        return 1.0
    source_units = _query_units(f"{title}\n{snippet}")
    return round(len(query_units.intersection(source_units)) / len(query_units), 4)


def _canonical_result_url(value: str) -> str:
    parsed = urlparse(value)
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def search_tavily(
    config: TavilyConfig,
    query: str,
    max_results: int = 5,
    search_depth: str = "basic",
    *,
    start_date: str | None = None,
    time_range: str | None = None,
    topic: str = "general",
    country: str | None = None,
    min_score: float = MIN_TAVILY_RELEVANCE_SCORE,
    min_query_overlap: float = MIN_QUERY_OVERLAP_SCORE,
    allowed_languages: set[str] | None = None,
    required_any_terms: tuple[str, ...] = (),
    include_domains: tuple[str, ...] = (),
    diagnostics: dict[str, Any] | None = None,
    max_snippet_chars: int = MAX_SNIPPET_CHARS,
    chunks_per_source: int = 2,
) -> list[dict[str, Any]]:
    """调用 Tavily 并只返回安全、有限长度的来源摘要。"""
    filter_start_date = start_date or recent_source_start_date(
        RECENT_MONTH_DAYS if time_range == "month" else RECENT_YEARS_DAYS
    )
    payload = {
        "query": query,
        "search_depth": search_depth,
        # 官方提示 max_results 越高越容易引入低质量项；只做两倍候选池。
        "max_results": min(max_results * 2, 10),
        "include_answer": False,
        "include_raw_content": False,
        "topic": topic,
        "exclude_domains": sorted(BLOCKED_RESEARCH_DOMAINS),
    }
    if search_depth == "advanced":
        payload["chunks_per_source"] = max(1, min(int(chunks_per_source or 2), 3))
    if time_range:
        payload["time_range"] = time_range
    else:
        payload["start_date"] = filter_start_date
    if country and topic == "general":
        payload["country"] = country
    if include_domains:
        payload["include_domains"] = list(dict.fromkeys(include_domains))[:20]
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                TAVILY_SEARCH_URL,
                headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise RuntimeError("Tavily API Key 无效或没有访问权限") from exc
        raise RuntimeError(f"Tavily 搜索失败：HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("Tavily 网络连接失败，请检查网络后重试") from exc

    response_data = response.json()
    raw_results = response_data.get("results") or []
    search_diagnostics: dict[str, Any] = {
        "query": query,
        "request_id": str(response_data.get("request_id") or ""),
        "raw_result_count": len(raw_results),
        "invalid_url_count": 0,
        "duplicate_url_count": 0,
        "source_policy_rejected_count": 0,
        "query_overlap_rejected_count": 0,
        "domain_limited_count": 0,
        "candidate_count": 0,
        "returned_count": 0,
    }
    candidates: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in raw_results:
        url = str(item.get("url") or "").strip()
        if not url.startswith(("https://", "http://")):
            search_diagnostics["invalid_url_count"] += 1
            continue
        canonical_url = _canonical_result_url(url)
        if canonical_url in seen_urls:
            search_diagnostics["duplicate_url_count"] += 1
            continue
        title = str(item.get("title") or url)[:500]
        snippet = str(item.get("content") or "").strip()[: max(200, min(int(max_snippet_chars), 5000))]
        published_at = str(item.get("published_date") or "")[:80]
        domain = urlparse(url).netloc.lower()
        score = float(item.get("score") or 0)
        language = classify_target_language(title, snippet)
        if not language or not is_allowed_research_source(
            title,
            snippet,
            published_at,
            score,
            domain,
            start_date=filter_start_date,
            min_score=min_score,
            allowed_languages=allowed_languages,
            required_any_terms=required_any_terms,
        ):
            search_diagnostics["source_policy_rejected_count"] += 1
            continue
        overlap_score = query_overlap_score(query, title, snippet)
        if overlap_score < min_query_overlap:
            search_diagnostics["query_overlap_rejected_count"] += 1
            continue
        rerank_score = round(score * 0.75 + overlap_score * 0.25, 6)
        seen_urls.add(canonical_url)
        candidates.append(
            {
                "title": title,
                "url": url,
                "domain": domain,
                "snippet": snippet,
                "score": score,
                "published_at": published_at,
                "payload": {
                    "favicon": str(item.get("favicon") or ""),
                    "search_depth": search_depth,
                    "language": language,
                    "topic": topic,
                    "time_range": time_range or "",
                    "start_date": filter_start_date,
                    "provider_score": score,
                    "query_overlap_score": overlap_score,
                    "rerank_score": rerank_score,
                    "tavily_request_id": str(response_data.get("request_id") or ""),
                },
            }
        )
    search_diagnostics["candidate_count"] = len(candidates)
    candidates.sort(
        key=lambda item: (
            float((item.get("payload") or {}).get("rerank_score") or 0),
            float(item.get("score") or 0),
        ),
        reverse=True,
    )
    results: list[dict[str, Any]] = []
    domain_counts: dict[str, int] = {}
    for candidate in candidates:
        domain = candidate["domain"]
        if domain_counts.get(domain, 0) >= MAX_RESULTS_PER_DOMAIN:
            search_diagnostics["domain_limited_count"] += 1
            continue
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        results.append(candidate)
        if len(results) >= max_results:
            break
    search_diagnostics["returned_count"] = len(results)
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update(search_diagnostics)
    return results
