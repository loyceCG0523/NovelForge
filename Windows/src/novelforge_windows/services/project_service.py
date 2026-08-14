"""Project and chapter use cases."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from novelforge_windows.domain.models import Chapter, Project
from novelforge_windows.infrastructure.repositories import (
    ChapterRepository,
    ProjectRepository,
)


def _required(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label}不能为空。")
    return normalized


def _validate_word_range(minimum: int, maximum: int) -> None:
    if minimum < 300:
        raise ValueError("单章最少字数不能低于 300。")
    if maximum < minimum:
        raise ValueError("单章最多字数不能小于最少字数。")


class ProjectService:
    def __init__(self, projects: ProjectRepository) -> None:
        self.projects = projects

    def list_projects(self) -> list[Project]:
        return self.projects.list()

    def get_project(self, project_id: str) -> Project:
        project = self.projects.get(project_id)
        if project is None:
            raise LookupError("作品不存在。")
        return project

    def create_project(
        self,
        *,
        title: str,
        author: str = "",
        genre: str = "",
        brief: str = "",
        target_words: int = 200_000,
        chapter_min_words: int = 2_000,
        chapter_max_words: int = 3_500,
    ) -> Project:
        _validate_word_range(chapter_min_words, chapter_max_words)
        if target_words < chapter_min_words:
            raise ValueError("目标总字数不能小于单章最少字数。")
        return self.projects.create(
            title=_required(title, "作品名"),
            author=author.strip(),
            genre=genre.strip(),
            brief=brief.strip(),
            target_words=target_words,
            chapter_min_words=chapter_min_words,
            chapter_max_words=chapter_max_words,
        )

    def update_project(
        self,
        project_id: str,
        *,
        title: str,
        author: str,
        genre: str,
        brief: str,
        target_words: int,
        chapter_min_words: int,
        chapter_max_words: int,
    ) -> Project:
        _validate_word_range(chapter_min_words, chapter_max_words)
        if target_words < chapter_min_words:
            raise ValueError("目标总字数不能小于单章最少字数。")
        return self.projects.update(
            project_id,
            title=_required(title, "作品名"),
            author=author.strip(),
            genre=genre.strip(),
            brief=brief.strip(),
            target_words=target_words,
            chapter_min_words=chapter_min_words,
            chapter_max_words=chapter_max_words,
        )

    def delete_project(self, project_id: str) -> None:
        self.projects.delete(project_id)


class ChapterService:
    def __init__(
        self,
        projects: ProjectRepository,
        chapters: ChapterRepository,
    ) -> None:
        self.projects = projects
        self.chapters = chapters

    def list_chapters(self, project_id: str) -> list[Chapter]:
        return self.chapters.list_for_project(project_id)

    def get_chapter(self, chapter_id: str) -> Chapter:
        chapter = self.chapters.get(chapter_id)
        if chapter is None:
            raise LookupError("章节不存在。")
        return chapter

    def next_sequence_no(self, project_id: str) -> int:
        return self.chapters.next_sequence_no(project_id)

    def save_chapter(
        self,
        *,
        project_id: str,
        chapter_id: str | None,
        sequence_no: int,
        title: str,
        summary: str,
        content: str,
        status: str = "draft",
    ) -> Chapter:
        if self.projects.get(project_id) is None:
            raise LookupError("作品不存在。")
        if sequence_no < 1:
            raise ValueError("章节序号必须大于 0。")
        normalized_title = _required(title, "章节标题")
        try:
            if chapter_id:
                return self.chapters.update(
                    chapter_id,
                    sequence_no=sequence_no,
                    title=normalized_title,
                    summary=summary.strip(),
                    content=content.strip(),
                    status=status,
                )
            return self.chapters.create(
                project_id=project_id,
                sequence_no=sequence_no,
                title=normalized_title,
                summary=summary.strip(),
                content=content.strip(),
                status=status,
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"第 {sequence_no} 章已经存在。") from exc

    def delete_chapter(self, chapter_id: str) -> None:
        self.chapters.delete(chapter_id)

    def export_project_markdown(self, project_id: str, target: Path) -> Path:
        project = self.projects.get(project_id)
        if project is None:
            raise LookupError("作品不存在。")
        chapters = self.chapters.list_for_project(project_id)
        if not chapters:
            raise ValueError("当前作品没有可导出的章节。")
        parts = [f"# {project.title}"]
        if project.author:
            parts.append(f"作者：{project.author}")
        for chapter in chapters:
            parts.extend((f"## 第{chapter.sequence_no}章 {chapter.title}", chapter.content))
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n\n".join(parts).strip() + "\n", encoding="utf-8")
        return target


def safe_export_name(title: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    return name or "NovelForge作品"

