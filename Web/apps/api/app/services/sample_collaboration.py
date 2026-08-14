"""协同样本的权限、校验、可信度和实时事件服务。"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.sample_analysis import SampleAnalysis
from app.models.sample_collaboration import (
    SampleAnnotation,
    SampleAnnotationReport,
    SampleAnnotationVote,
    SampleCollaborationEvent,
    SampleTextSegment,
    SampleUserReputation,
)
from app.models.user import User
from app.services.object_storage import read_text_object


ANNOTATION_CATEGORIES: dict[str, str] = {
    "humor": "幽默与笑点",
    "internet_meme": "网络热梗",
    "abstract": "抽象表达",
    "characterization": "人物塑造",
    "dialogue": "人物对话",
    "inner_voice": "心理活动",
    "environment": "环境描写",
    "foreshadowing": "伏笔",
    "hook": "开篇或章尾钩子",
    "pacing": "节奏与转折",
}

REPORT_REASONS = {"spam", "meaningless", "wrong_quote", "copyright", "abuse", "other"}

ANNOTATION_CONTEXT_RADIUS = 320


def get_accessible_sample(
    db: Session,
    analysis_id: UUID,
    user_id: UUID,
    *,
    owner_only: bool = False,
) -> SampleAnalysis:
    analysis = db.get(SampleAnalysis, analysis_id)
    allowed = analysis is not None and (
        analysis.owner_id == user_id
        or (
            not owner_only
            and analysis.visibility == "public"
            and analysis.publication_status == "published"
        )
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="Sample not found")
    return analysis


def validate_categories(categories: list[str]) -> list[str]:
    normalized = list(dict.fromkeys(str(item).strip() for item in categories if str(item).strip()))
    invalid = [item for item in normalized if item not in ANNOTATION_CATEGORIES]
    if invalid:
        raise HTTPException(status_code=400, detail=f"不支持的标注分类：{', '.join(invalid)}")
    if not normalized or len(normalized) > 5:
        raise HTTPException(status_code=400, detail="请选择1—5个有效分类")
    return normalized


def load_and_validate_selection(
    segment: SampleTextSegment,
    *,
    start_offset: int,
    end_offset: int,
    quote_text: str,
) -> str:
    selected, _ = load_selection_context(
        segment,
        start_offset=start_offset,
        end_offset=end_offset,
        quote_text=quote_text,
    )
    return selected


def load_selection_context(
    segment: SampleTextSegment,
    *,
    start_offset: int,
    end_offset: int,
    quote_text: str,
) -> tuple[str, str]:
    """校验人工选区，并只截取附近少量上下文供说明生成使用。"""
    content = read_text_object(segment.source_object_key)
    return selection_context_from_content(
        content,
        start_offset=start_offset,
        end_offset=end_offset,
        quote_text=quote_text,
    )


def selection_context_from_content(
    content: str,
    *,
    start_offset: int,
    end_offset: int,
    quote_text: str,
) -> tuple[str, str]:
    """从已读取的章节原文校验并提取选区，供章节批处理复用。"""
    if start_offset < 0 or end_offset <= start_offset or end_offset > len(content):
        raise HTTPException(status_code=400, detail="标注选区已经越出当前原文分段")
    selected = content[start_offset:end_offset]
    if len(selected) < 2 or len(selected) > 1000:
        raise HTTPException(status_code=400, detail="标注选区长度必须为2—1000字")
    if selected != quote_text:
        raise HTTPException(status_code=409, detail="选区原文已变化，请刷新阅读器后重新选择")
    context_start = max(0, start_offset - ANNOTATION_CONTEXT_RADIUS)
    context_end = min(len(content), end_offset + ANNOTATION_CONTEXT_RADIUS)
    return selected, content[context_start:context_end]


def build_annotation_hash(quote_text: str, categories: list[str], note: str) -> str:
    canonical = json.dumps(
        {"quote": quote_text, "categories": sorted(categories), "note": note.strip()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def enforce_collaboration_rate_limit(
    user_id: UUID,
    action: str,
    *,
    limit: int,
    window_seconds: int = 60,
) -> None:
    """Redis不可用时不阻断写入，最终一致性仍由数据库约束保证。"""
    try:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        key = f"novelforge:sample-collab:rate:{action}:{user_id}"
        count = int(client.incr(key))
        if count == 1:
            client.expire(key, window_seconds)
        if count > limit:
            raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
    except HTTPException:
        raise
    except Exception:
        return


def get_or_create_reputation(db: Session, user_id: UUID) -> SampleUserReputation:
    reputation = db.scalar(
        select(SampleUserReputation).where(SampleUserReputation.user_id == user_id)
    )
    if reputation is None:
        reputation = SampleUserReputation(user_id=user_id, score=1.0)
        db.add(reputation)
        db.flush()
    return reputation


def recompute_annotation_consensus(db: Session, annotation: SampleAnnotation) -> bool:
    """重算公共标注可信状态，返回是否需要创建或重建向量。"""
    analysis = db.get(SampleAnalysis, annotation.sample_analysis_id)
    if analysis is None:
        return False
    previous_status = annotation.status
    if analysis.visibility == "private":
        annotation.status = "trusted_private"
        annotation.trust_score = 1.0
        return annotation.embedding is None

    votes = db.scalars(
        select(SampleAnnotationVote).where(
            SampleAnnotationVote.annotation_id == annotation.id
        )
    ).all()
    reports = db.scalar(
        select(func.count(SampleAnnotationReport.id)).where(
            SampleAnnotationReport.annotation_id == annotation.id,
            SampleAnnotationReport.status == "open",
        )
    ) or 0
    user_ids = {vote.user_id for vote in votes}
    reputations = {
        item.user_id: max(0.2, min(2.0, float(item.score or 1.0)))
        for item in db.scalars(
            select(SampleUserReputation).where(SampleUserReputation.user_id.in_(user_ids))
        ).all()
    } if user_ids else {}
    up_votes = [vote for vote in votes if vote.value == 1]
    down_votes = [vote for vote in votes if vote.value == -1]
    up_weight = sum(reputations.get(vote.user_id, 1.0) for vote in up_votes)
    down_weight = sum(reputations.get(vote.user_id, 1.0) for vote in down_votes)
    score = max(0.0, min(1.0, 0.35 + 0.2 * up_weight - 0.25 * down_weight - 0.3 * reports))
    annotation.trust_score = round(score, 4)
    if reports >= 2 or len(down_votes) >= 3 or score <= 0.1:
        annotation.status = "quarantined"
        annotation.embedding = None
        annotation.embedding_model = ""
    elif len(up_votes) >= 2 and score >= 0.65:
        annotation.status = "trusted"
    else:
        annotation.status = "pending"

    if previous_status != annotation.status and annotation.status in {"trusted", "quarantined"}:
        creator_reputation = get_or_create_reputation(db, annotation.creator_id)
        if annotation.status == "trusted":
            creator_reputation.score = min(2.0, float(creator_reputation.score or 1.0) + 0.05)
            creator_reputation.trusted_contribution_count += 1
        else:
            creator_reputation.score = max(0.2, float(creator_reputation.score or 1.0) - 0.2)
            creator_reputation.rejected_contribution_count += 1
    return annotation.status in {"trusted", "trusted_private"} and annotation.embedding is None


def annotation_read_payload(
    db: Session,
    annotation: SampleAnnotation,
    current_user: User,
) -> dict[str, Any]:
    return annotation_read_payloads(db, [annotation], current_user)[0]


def annotation_read_payloads(
    db: Session,
    annotations: list[SampleAnnotation],
    current_user: User,
) -> list[dict[str, Any]]:
    """批量装配标注读取结果，避免列表接口按标注逐条查询关联数据。"""
    if not annotations:
        return []

    annotation_ids = [annotation.id for annotation in annotations]
    creator_ids = {annotation.creator_id for annotation in annotations}
    votes = db.scalars(
        select(SampleAnnotationVote).where(
            SampleAnnotationVote.annotation_id.in_(annotation_ids)
        )
    ).all()
    report_rows = db.execute(
        select(
            SampleAnnotationReport.annotation_id,
            func.count(SampleAnnotationReport.id),
        )
        .where(
            SampleAnnotationReport.annotation_id.in_(annotation_ids),
            SampleAnnotationReport.status == "open",
        )
        .group_by(SampleAnnotationReport.annotation_id)
    ).all()
    creators = db.scalars(select(User).where(User.id.in_(creator_ids))).all()

    votes_by_annotation: dict[UUID, list[SampleAnnotationVote]] = {}
    for vote in votes:
        votes_by_annotation.setdefault(vote.annotation_id, []).append(vote)
    report_counts = {
        annotation_id: int(report_count or 0)
        for annotation_id, report_count in report_rows
    }
    creators_by_id = {creator.id: creator for creator in creators}

    payloads = []
    for annotation in annotations:
        annotation_votes = votes_by_annotation.get(annotation.id, [])
        creator = creators_by_id.get(annotation.creator_id)
        own_vote = next(
            (vote.value for vote in annotation_votes if vote.user_id == current_user.id),
            0,
        )
        payloads.append(
            {
                "id": annotation.id,
                "sample_analysis_id": annotation.sample_analysis_id,
                "segment_id": annotation.segment_id,
                "creator_id": annotation.creator_id,
                "creator_name": creator.display_name if creator else "已注销用户",
                "start_offset": annotation.start_offset,
                "end_offset": annotation.end_offset,
                "quote_text": annotation.quote_text,
                "categories": list(annotation.categories or []),
                "note": annotation.note,
                "status": annotation.status,
                "trust_score": annotation.trust_score,
                "version": annotation.version,
                "upvotes": sum(vote.value == 1 for vote in annotation_votes),
                "downvotes": sum(vote.value == -1 for vote in annotation_votes),
                "report_count": report_counts.get(annotation.id, 0),
                "current_user_vote": int(own_vote),
                "can_edit": annotation.creator_id == current_user.id,
                "created_at": annotation.created_at,
                "updated_at": annotation.updated_at,
            }
        )
    return payloads


def emit_collaboration_event(
    db: Session,
    *,
    analysis_id: UUID,
    actor_id: UUID | None,
    event_type: str,
    payload: dict[str, Any],
) -> SampleCollaborationEvent:
    # 锁定作品行，保证同一作品的事件序号在多API实例下仍然唯一。
    db.execute(
        select(SampleAnalysis.id)
        .where(SampleAnalysis.id == analysis_id)
        .with_for_update()
    ).first()
    latest = db.scalar(
        select(func.max(SampleCollaborationEvent.sequence_no)).where(
            SampleCollaborationEvent.sample_analysis_id == analysis_id
        )
    ) or 0
    event = SampleCollaborationEvent(
        sample_analysis_id=analysis_id,
        actor_id=actor_id,
        sequence_no=int(latest) + 1,
        event_type=event_type,
        payload=payload,
    )
    db.add(event)
    db.flush()
    return event


def publish_collaboration_event(event: SampleCollaborationEvent) -> None:
    """Redis只做低延迟通知；数据库事件才是断线恢复事实源。"""
    try:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        client.publish(
            f"novelforge:sample-collab:{event.sample_analysis_id}",
            json.dumps(
                {"sequence_no": event.sequence_no, "event_type": event.event_type},
                ensure_ascii=False,
            ),
        )
    except Exception:
        return
