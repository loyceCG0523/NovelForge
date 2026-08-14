"""Local orchestration for story-bible and chapter generation."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from novelforge_windows.domain.models import Chapter
from novelforge_windows.infrastructure.repositories import (
    ChapterRepository,
    ProjectRepository,
)
from novelforge_windows.services.llm_client import OpenAICompatibleClient
from novelforge_windows.services.model_settings_service import ModelSettingsService


class GenerationService:
    def __init__(
        self,
        projects: ProjectRepository,
        chapters: ChapterRepository,
        model_settings: ModelSettingsService,
        llm: OpenAICompatibleClient,
    ) -> None:
        self.projects = projects
        self.chapters = chapters
        self.model_settings = model_settings
        self.llm = llm

    def generate_story_bible(self, project_id: str) -> str:
        project = self.projects.get(project_id)
        if project is None:
            raise LookupError("作品不存在。")
        settings = self.model_settings.load()
        content = self.llm.complete(
            settings,
            [
                {
                    "role": "system",
                    "content": (
                        "你是长篇小说策划编辑。请输出可直接执行的作品圣经，"
                        "包含核心卖点、世界规则、主要人物、关系、主线、阶段目标、"
                        "伏笔与写作禁忌。使用清晰的 Markdown，不要解释任务本身。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"作品名：{project.title}\n"
                        f"类型：{project.genre or '未指定'}\n"
                        f"目标字数：{project.target_words}\n"
                        f"起始需求：\n{project.brief or '暂无补充要求'}"
                    ),
                },
            ],
        )
        self.projects.update_story_bible(project_id, content)
        return content

    def generate_next_chapter(self, project_id: str, requested_title: str = "") -> Chapter:
        project = self.projects.get(project_id)
        if project is None:
            raise LookupError("作品不存在。")
        settings = self.model_settings.load()
        sequence_no = self.chapters.next_sequence_no(project_id)
        title = requested_title.strip() or f"第{sequence_no}章"
        recent = self.chapters.recent_for_project(project_id, limit=3)
        recent_context = "\n\n".join(
            (
                f"第{item.sequence_no}章 {item.title}\n"
                f"摘要：{item.summary or '无'}\n"
                f"结尾片段：{item.content[-1600:]}"
            )
            for item in recent
        ) or "这是第一章，没有前文章节。"
        story_bible = project.story_bible or "尚未生成作品圣经，请严格遵循起始需求。"
        content = self.llm.complete(
            settings,
            [
                {
                    "role": "system",
                    "content": (
                        "你是职业中文长篇小说作者。只输出本章正文，不输出分析、提纲、"
                        "字数说明或 Markdown 标题。保持人物动机和事实连续，场景具体，"
                        "结尾留下自然推进下一章的动力。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"作品：{project.title}\n"
                        f"类型：{project.genre or '未指定'}\n"
                        f"目标章节：第{sequence_no}章《{title}》\n"
                        f"期望长度：{project.chapter_min_words}-{project.chapter_max_words}字\n\n"
                        f"起始需求：\n{project.brief or '无'}\n\n"
                        f"作品圣经：\n{story_bible}\n\n"
                        f"最近章节上下文：\n{recent_context}"
                    ),
                },
            ],
        )
        summary = _local_summary(content)
        snapshot = json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "model": settings.model,
                "recent_chapter_ids": [item.id for item in recent],
                "story_bible_present": bool(project.story_bible),
            },
            ensure_ascii=False,
        )
        return self.chapters.create(
            project_id=project_id,
            sequence_no=sequence_no,
            title=title,
            summary=summary,
            content=content,
            status="generated",
            context_snapshot=snapshot,
        )


def _local_summary(content: str, maximum: int = 180) -> str:
    compact = " ".join(part.strip() for part in content.splitlines() if part.strip())
    if len(compact) <= maximum:
        return compact
    return compact[:maximum].rstrip("，。；：、 ") + "……"

