"""工作台聚合响应的数据契约。"""

from pydantic import BaseModel


class NovelDashboardRead(BaseModel):
    """工作台页面一次请求需要的主要数据块。"""

    novel: dict
    counts: dict
    latest_chapters: list[dict]
    latest_tasks: list[dict]
    open_review_issues: list[dict]
    review_issues: list[dict] = []
    current_story_event: dict | None = None
    current_auto_run: dict | None = None
