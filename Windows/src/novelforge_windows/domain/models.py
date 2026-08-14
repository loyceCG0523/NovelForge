"""Small immutable records shared by repositories, services and the UI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    title: str
    author: str
    genre: str
    brief: str
    story_bible: str
    target_words: int
    chapter_min_words: int
    chapter_max_words: int
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Chapter:
    id: str
    project_id: str
    sequence_no: int
    title: str
    summary: str
    content: str
    status: str
    context_snapshot: str
    created_at: str
    updated_at: str

    @property
    def character_count(self) -> int:
        return len("".join(self.content.split()))


@dataclass(frozen=True, slots=True)
class SampleDocument:
    id: str
    title: str
    source_name: str
    local_path: str
    character_count: int
    created_at: str


@dataclass(frozen=True, slots=True)
class ModelSettings:
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    api_key: str = ""
    temperature: float = 0.8

