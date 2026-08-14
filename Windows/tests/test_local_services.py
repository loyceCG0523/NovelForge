from __future__ import annotations

from pathlib import Path

import pytest

from novelforge_windows.config import AppPaths
from novelforge_windows.domain.models import ModelSettings
from novelforge_windows.infrastructure.database import Database
from novelforge_windows.infrastructure.repositories import (
    ChapterRepository,
    ProjectRepository,
    SampleRepository,
)
from novelforge_windows.infrastructure.secrets import protect_secret, unprotect_secret
from novelforge_windows.services.generation_service import GenerationService
from novelforge_windows.services.llm_client import chat_completions_url
from novelforge_windows.services.project_service import ChapterService, ProjectService
from novelforge_windows.services.sample_service import SampleService


@pytest.fixture()
def repositories(tmp_path: Path):
    database = Database(tmp_path / "novelforge.db")
    database.initialize()
    return (
        ProjectRepository(database),
        ChapterRepository(database),
        SampleRepository(database),
    )


def test_project_and_chapter_are_persisted_locally(repositories) -> None:
    project_repository, chapter_repository, _ = repositories
    projects = ProjectService(project_repository)
    chapters = ChapterService(project_repository, chapter_repository)
    project = projects.create_project(
        title="本地长篇",
        genre="都市",
        brief="主角回到故乡重新开始。",
    )

    chapter = chapters.save_chapter(
        project_id=project.id,
        chapter_id=None,
        sequence_no=1,
        title="归来",
        summary="主角抵达故乡。",
        content="列车驶入旧城。",
    )

    assert projects.get_project(project.id).title == "本地长篇"
    assert chapters.list_chapters(project.id) == [chapter]
    assert chapter.character_count == 7


def test_project_delete_cascades_to_chapters(repositories) -> None:
    project_repository, chapter_repository, _ = repositories
    project = ProjectService(project_repository).create_project(title="待删除")
    chapter_repository.create(
        project_id=project.id,
        sequence_no=1,
        title="第一章",
        summary="",
        content="正文",
    )

    project_repository.delete(project.id)

    assert chapter_repository.list_for_project(project.id) == []


def test_sample_import_creates_managed_utf8_copy(tmp_path: Path, repositories) -> None:
    _, _, sample_repository = repositories
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    source = tmp_path / "样本.md"
    source.write_text("第一段。\n\n第二段。", encoding="utf-8")
    service = SampleService(sample_repository, samples_dir)

    sample = service.import_document(source)

    assert sample.source_name == "样本.md"
    assert Path(sample.local_path).parent == samples_dir.resolve()
    assert service.preview(sample.id) == "第一段。\n\n第二段。"


def test_chat_completions_url_accepts_common_base_urls() -> None:
    assert (
        chat_completions_url("https://api.example.com/v1")
        == "https://api.example.com/v1/chat/completions"
    )
    assert (
        chat_completions_url("http://127.0.0.1:11434")
        == "http://127.0.0.1:11434/v1/chat/completions"
    )
    complete = "https://api.example.com/custom/chat/completions"
    assert chat_completions_url(complete) == complete


class _FakeSettings:
    def load(self) -> ModelSettings:
        return ModelSettings(base_url="http://local/v1", model="test-model")


class _FakeLlm:
    def complete(self, settings, messages, *, max_tokens=None) -> str:
        if "作品圣经" in messages[0]["content"]:
            return "# 作品圣经\n\n主线明确。"
        return "清晨，旧城的钟声穿过薄雾。"


def test_generation_service_writes_story_bible_and_chapter(repositories) -> None:
    project_repository, chapter_repository, _ = repositories
    project = ProjectService(project_repository).create_project(
        title="雾城",
        brief="围绕一封旧信展开。",
    )
    generation = GenerationService(
        project_repository,
        chapter_repository,
        _FakeSettings(),  # type: ignore[arg-type]
        _FakeLlm(),  # type: ignore[arg-type]
    )

    bible = generation.generate_story_bible(project.id)
    chapter = generation.generate_next_chapter(project.id, "旧信")

    assert bible.startswith("# 作品圣经")
    assert project_repository.get(project.id).story_bible == bible
    assert chapter.sequence_no == 1
    assert chapter.title == "旧信"
    assert chapter.status == "generated"


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows DPAPI only")
def test_dpapi_secret_round_trip() -> None:
    encrypted = protect_secret("local-secret")
    assert encrypted != "local-secret"
    assert unprotect_secret(encrypted) == "local-secret"

