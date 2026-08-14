"""作品网络研究资料接口。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.novel import Novel
from app.models.research_source import ResearchSource
from app.models.user import User
from app.schemas.research import ResearchSearchRequest, ResearchSourceRead
from app.services.llm_client import (
    LLMClient,
    build_llm_config,
    is_official_deepseek_v4_flash,
)
from app.services.tavily_search import build_tavily_config, is_allowed_research_source, search_tavily


router = APIRouter(prefix="/api/novels/{novel_id}/research", tags=["research"])


@router.get("", response_model=list[ResearchSourceRead])
def list_research_sources(novel: Novel = Depends(get_owned_novel), db: Session = Depends(get_db)) -> list[ResearchSource]:
    return [
        source
        for source in db.scalars(
            select(ResearchSource)
            .where(ResearchSource.novel_id == novel.id)
            .order_by(ResearchSource.created_at.desc())
        ).all()
        if is_allowed_research_source(
            source.title,
            source.snippet,
            source.published_at,
            source.score,
            source.domain,
        )
    ]


@router.post("/search", response_model=list[ResearchSourceRead], status_code=status.HTTP_201_CREATED)
def search_research_sources(
    payload: ResearchSearchRequest,
    novel: Novel = Depends(get_owned_novel),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ResearchSource]:
    llm_config = build_llm_config(current_user.preferences)
    use_deepseek_web_search = bool(
        llm_config
        and is_official_deepseek_v4_flash(llm_config.base_url, llm_config.model)
    )
    tavily_config = build_tavily_config(current_user.preferences)
    if not use_deepseek_web_search and tavily_config is None:
        raise HTTPException(
            status_code=400,
            detail="请配置支持 Web Search 的 DeepSeek 模型，或启用 Tavily 网络检索。",
        )
    provider = "deepseek_web_search" if use_deepseek_web_search else "tavily"
    try:
        if use_deepseek_web_search:
            results = LLMClient(llm_config).search_web(
                payload.query.strip(),
                max_results=payload.max_results,
            )
        else:
            results = search_tavily(
                tavily_config,
                payload.query.strip(),
                payload.max_results,
                payload.search_depth,
            )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    sources = []
    for result in results:
        result["payload"] = {
            **(result.get("payload") or {}),
            "source": "manual_search",
            "search_provider": provider,
        }
        sources.append(
            ResearchSource(
                novel_id=novel.id,
                query=payload.query.strip(),
                provider=provider,
                **result,
            )
        )
    db.add_all(sources)
    db.commit()
    for source in sources:
        db.refresh(source)
    return sources


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_research_source(source_id: UUID, novel: Novel = Depends(get_owned_novel), db: Session = Depends(get_db)) -> None:
    deleted = db.execute(delete(ResearchSource).where(ResearchSource.id == source_id, ResearchSource.novel_id == novel.id))
    if not deleted.rowcount:
        raise HTTPException(status_code=404, detail="Research source not found")
    db.commit()
