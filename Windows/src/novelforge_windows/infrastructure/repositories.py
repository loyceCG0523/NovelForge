"""Repositories for local SQLite records."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from novelforge_windows.domain.models import Chapter, Project, SampleDocument
from novelforge_windows.infrastructure.database import Database


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _project(row: object) -> Project:
    return Project(**dict(row))  # type: ignore[arg-type]


def _chapter(row: object) -> Chapter:
    return Chapter(**dict(row))  # type: ignore[arg-type]


def _sample(row: object) -> SampleDocument:
    return SampleDocument(**dict(row))  # type: ignore[arg-type]


class ProjectRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list(self) -> list[Project]:
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC, title"
            ).fetchall()
        return [_project(row) for row in rows]

    def get(self, project_id: str) -> Project | None:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return _project(row) if row is not None else None

    def create(
        self,
        *,
        title: str,
        author: str,
        genre: str,
        brief: str,
        target_words: int,
        chapter_min_words: int,
        chapter_max_words: int,
    ) -> Project:
        project_id = str(uuid4())
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects(
                    id, title, author, genre, brief, story_bible,
                    target_words, chapter_min_words, chapter_max_words,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    project_id,
                    title,
                    author,
                    genre,
                    brief,
                    target_words,
                    chapter_min_words,
                    chapter_max_words,
                    now,
                    now,
                ),
            )
        created = self.get(project_id)
        assert created is not None
        return created

    def update(
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
        with self.database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE projects
                SET title = ?, author = ?, genre = ?, brief = ?, target_words = ?,
                    chapter_min_words = ?, chapter_max_words = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    title,
                    author,
                    genre,
                    brief,
                    target_words,
                    chapter_min_words,
                    chapter_max_words,
                    utc_now(),
                    project_id,
                ),
            )
            if result.rowcount != 1:
                raise LookupError("作品不存在。")
        updated = self.get(project_id)
        assert updated is not None
        return updated

    def update_story_bible(self, project_id: str, story_bible: str) -> Project:
        with self.database.transaction() as connection:
            result = connection.execute(
                """
                UPDATE projects
                SET story_bible = ?, updated_at = ?
                WHERE id = ?
                """,
                (story_bible, utc_now(), project_id),
            )
            if result.rowcount != 1:
                raise LookupError("作品不存在。")
        updated = self.get(project_id)
        assert updated is not None
        return updated

    def delete(self, project_id: str) -> None:
        with self.database.transaction() as connection:
            result = connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            if result.rowcount != 1:
                raise LookupError("作品不存在。")


class ChapterRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list_for_project(self, project_id: str) -> list[Chapter]:
        with self.database.session() as connection:
            rows = connection.execute(
                """
                SELECT * FROM chapters
                WHERE project_id = ?
                ORDER BY sequence_no, created_at
                """,
                (project_id,),
            ).fetchall()
        return [_chapter(row) for row in rows]

    def recent_for_project(self, project_id: str, *, limit: int = 3) -> list[Chapter]:
        with self.database.session() as connection:
            rows = connection.execute(
                """
                SELECT * FROM chapters
                WHERE project_id = ?
                ORDER BY sequence_no DESC
                LIMIT ?
                """,
                (project_id, max(1, limit)),
            ).fetchall()
        return list(reversed([_chapter(row) for row in rows]))

    def get(self, chapter_id: str) -> Chapter | None:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM chapters WHERE id = ?", (chapter_id,)
            ).fetchone()
        return _chapter(row) if row is not None else None

    def next_sequence_no(self, project_id: str) -> int:
        with self.database.session() as connection:
            value = connection.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM chapters WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
        return int(value)

    def create(
        self,
        *,
        project_id: str,
        sequence_no: int,
        title: str,
        summary: str,
        content: str,
        status: str = "draft",
        context_snapshot: str = "",
    ) -> Chapter:
        chapter_id = str(uuid4())
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO chapters(
                    id, project_id, sequence_no, title, summary, content,
                    status, context_snapshot, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chapter_id,
                    project_id,
                    sequence_no,
                    title,
                    summary,
                    content,
                    status,
                    context_snapshot,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id)
            )
        created = self.get(chapter_id)
        assert created is not None
        return created

    def update(
        self,
        chapter_id: str,
        *,
        sequence_no: int,
        title: str,
        summary: str,
        content: str,
        status: str,
    ) -> Chapter:
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT project_id FROM chapters WHERE id = ?", (chapter_id,)
            ).fetchone()
            if row is None:
                raise LookupError("章节不存在。")
            connection.execute(
                """
                UPDATE chapters
                SET sequence_no = ?, title = ?, summary = ?, content = ?,
                    status = ?, updated_at = ?
                WHERE id = ?
                """,
                (sequence_no, title, summary, content, status, now, chapter_id),
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (now, row["project_id"]),
            )
        updated = self.get(chapter_id)
        assert updated is not None
        return updated

    def delete(self, chapter_id: str) -> None:
        with self.database.transaction() as connection:
            result = connection.execute("DELETE FROM chapters WHERE id = ?", (chapter_id,))
            if result.rowcount != 1:
                raise LookupError("章节不存在。")


class SampleRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list(self) -> list[SampleDocument]:
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT * FROM sample_documents ORDER BY created_at DESC"
            ).fetchall()
        return [_sample(row) for row in rows]

    def get(self, sample_id: str) -> SampleDocument | None:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM sample_documents WHERE id = ?", (sample_id,)
            ).fetchone()
        return _sample(row) if row is not None else None

    def create(
        self,
        *,
        title: str,
        source_name: str,
        local_path: Path,
        character_count: int,
    ) -> SampleDocument:
        sample_id = local_path.stem
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sample_documents(
                    id, title, source_name, local_path, character_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sample_id,
                    title,
                    source_name,
                    str(local_path),
                    character_count,
                    utc_now(),
                ),
            )
        created = self.get(sample_id)
        assert created is not None
        return created

    def delete(self, sample_id: str) -> SampleDocument:
        sample = self.get(sample_id)
        if sample is None:
            raise LookupError("样本不存在。")
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM sample_documents WHERE id = ?", (sample_id,))
        return sample


class SettingsRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, key: str, default: str = "") -> str:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row is not None else default

    def set(self, key: str, value: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO app_settings(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, utc_now()),
            )

    def delete(self, key: str) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM app_settings WHERE key = ?", (key,))
