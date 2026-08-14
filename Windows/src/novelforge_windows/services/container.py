"""Composition root for the standalone application."""

from __future__ import annotations

from dataclasses import dataclass

from novelforge_windows.config import AppPaths, resolve_app_paths
from novelforge_windows.infrastructure.database import Database
from novelforge_windows.infrastructure.repositories import (
    ChapterRepository,
    ProjectRepository,
    SampleRepository,
    SettingsRepository,
)
from novelforge_windows.services.generation_service import GenerationService
from novelforge_windows.services.llm_client import OpenAICompatibleClient
from novelforge_windows.services.model_settings_service import ModelSettingsService
from novelforge_windows.services.project_service import ChapterService, ProjectService
from novelforge_windows.services.sample_service import SampleService


@dataclass(slots=True)
class ServiceContainer:
    paths: AppPaths
    database: Database
    projects: ProjectService
    chapters: ChapterService
    samples: SampleService
    model_settings: ModelSettingsService
    generation: GenerationService
    llm: OpenAICompatibleClient


def build_container(paths: AppPaths | None = None) -> ServiceContainer:
    resolved_paths = paths or resolve_app_paths()
    database = Database(resolved_paths.database_path)
    database.initialize()
    project_repository = ProjectRepository(database)
    chapter_repository = ChapterRepository(database)
    sample_repository = SampleRepository(database)
    settings_repository = SettingsRepository(database)
    project_service = ProjectService(project_repository)
    chapter_service = ChapterService(project_repository, chapter_repository)
    sample_service = SampleService(sample_repository, resolved_paths.samples_dir)
    model_settings = ModelSettingsService(settings_repository)
    llm = OpenAICompatibleClient()
    generation = GenerationService(
        project_repository,
        chapter_repository,
        model_settings,
        llm,
    )
    return ServiceContainer(
        paths=resolved_paths,
        database=database,
        projects=project_service,
        chapters=chapter_service,
        samples=sample_service,
        model_settings=model_settings,
        generation=generation,
        llm=llm,
    )

