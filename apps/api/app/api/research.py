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
    config = build_tavily_config(current_user.preferences)
    if config is None:
        raise HTTPException(status_code=400, detail="请先在设置中启用 Tavily 网络检索并配置 API Key")
    try:
        results = search_tavily(config, payload.query.strip(), payload.max_results, payload.search_depth)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    sources = []
    for result in results:
        result["payload"] = {**(result.get("payload") or {}), "source": "manual_search"}
        sources.append(ResearchSource(novel_id=novel.id, query=payload.query.strip(), provider="tavily", **result))
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
