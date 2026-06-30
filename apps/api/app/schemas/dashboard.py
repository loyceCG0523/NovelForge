from pydantic import BaseModel


class NovelDashboardRead(BaseModel):
    novel: dict
    counts: dict
    latest_chapters: list[dict]
    latest_tasks: list[dict]
    open_review_issues: list[dict]
