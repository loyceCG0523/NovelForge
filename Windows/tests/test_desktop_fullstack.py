from __future__ import annotations

import time
from uuid import UUID

from fastapi.testclient import TestClient


def test_desktop_fullstack_uses_web_ui_and_local_services(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOVELFORGE_WINDOWS_DATA_DIR", str(tmp_path))

    from novelforge_windows.config import resolve_app_paths
    from novelforge_windows.local_runtime import create_local_app
    from novelforge_windows.resources import resource_path

    paths = resolve_app_paths()
    app = create_local_app(paths)
    brief = {
        "story_era": "2020年代中国",
        "story_location": "上海",
        "chapter_word_min": 2000,
        "chapter_word_max": 3200,
        "event_chapter_count": 5,
        "characters": [
            {
                "key": "protagonist",
                "name": "林夏",
                "gender": "女",
                "age": "26",
                "occupation": "记者",
                "is_protagonist": True,
                "goal": "查明真相",
                "detailed_setting": "",
            }
        ],
    }

    with TestClient(app) as client:
        assert client.get("/workbench/").status_code == 200
        page_rsc = client.get("/chapters/__next.chapters.__PAGE__.txt?_rsc=test")
        expected_page_rsc = resource_path(
            "frontend/out/chapters/__next.chapters/__PAGE__.txt"
        ).read_bytes()
        assert page_rsc.status_code == 200
        assert page_rsc.headers["content-type"].startswith("text/plain")
        assert page_rsc.content == expected_page_rsc
        assert client.get("/chapters/__next.missing.__PAGE__.txt").status_code == 404
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/auth/me").json()["email"] == "local@novelforge.desktop"

        preferences = client.patch(
            "/api/users/me/preferences",
            json={
                "preferences": {
                    "llm": {
                        "base_url": "https://example.test/v1",
                        "model": "local-test-model",
                        "api_key": "desktop-secret",
                    }
                }
            },
        )
        assert preferences.status_code == 200
        assert preferences.json()["preferences"]["llm"]["api_key_configured"] is True
        assert "desktop-secret" not in preferences.text

        # Clear the test key so the automatically queued Story Bible task uses its local fallback.
        from app.db.session import SessionLocal, engine
        from app.models.user import User
        from sqlalchemy import event, select

        with SessionLocal() as db:
            user = db.scalar(select(User))
            assert user.preferences["llm"]["api_key"].startswith("dpapi:")
            user.preferences = {**user.preferences, "llm": {**user.preferences["llm"], "api_key": ""}}
            db.commit()

        novel_response = client.post(
            "/api/novels",
            json={
                "title": "本地全功能测试",
                "genre": "都市悬疑",
                "target_words": 200000,
                "premise": "记者调查一桩旧案",
                "brief": brief,
            },
        )
        assert novel_response.status_code == 201
        novel_id = novel_response.json()["id"]
        story_bible_summary = client.get(
            f"/api/novels/{novel_id}/story-bible/summary"
        )
        assert story_bible_summary.status_code == 200
        assert story_bible_summary.json()["novel_id"] == novel_id
        assert "content" not in story_bible_summary.json()
        assert "locked_fields" not in story_bible_summary.json()
        chapter_response = client.post(
            f"/api/novels/{novel_id}/chapters",
            json={
                "chapter_index": 1,
                "title": "雨夜来信",
                "status": "draft",
                "summary": "收到匿名线索",
                "content": "雨落在窗上。林夏拆开了没有署名的信。",
                "context_snapshot": {},
            },
        )
        assert chapter_response.status_code == 201
        first_chapter_id = chapter_response.json()["id"]
        assert client.post(
            f"/api/novels/{novel_id}/memory",
            json={
                "memory_type": "character",
                "entity_name": "林夏",
                "chapter_index_start": 1,
                "payload": {"current_status": "收到匿名信"},
            },
        ).status_code == 201
        assert client.post(
            "/api/meme-library",
            json={
                "phrase": "这波稳了",
                "meaning": "表达对结果有把握",
                "suitable_scenes": "朋友间轻松确认计划",
                "enabled": True,
            },
        ).status_code == 201

        deadline = time.monotonic() + 12
        tasks = []
        while time.monotonic() < deadline:
            tasks = client.get(f"/api/novels/{novel_id}/tasks").json()
            if tasks and all(item["status"] not in {"queued", "running"} for item in tasks):
                break
            time.sleep(0.1)
        assert tasks and all(item["status"] == "completed" for item in tasks)
        for chapter_index in range(2, 13):
            response = client.post(
                f"/api/novels/{novel_id}/chapters",
                json={
                    "chapter_index": chapter_index,
                    "title": f"Chapter {chapter_index}",
                    "status": "draft",
                    "summary": "Query-count regression fixture",
                    "content": "Local chapter content.",
                    "context_snapshot": {},
                },
            )
            assert response.status_code == 201

        from app.models.event_chapter_plan import EventChapterPlan
        from app.models.story_event import StoryEvent

        with SessionLocal() as db:
            story_event = StoryEvent(
                novel_id=UUID(novel_id),
                title="Regression event",
                start_chapter_index=1,
                end_chapter_index=1,
                planned_chapter_count=1,
            )
            db.add(story_event)
            db.flush()
            db.add(
                EventChapterPlan(
                    story_event_id=story_event.id,
                    novel_id=UUID(novel_id),
                    chapter_id=UUID(first_chapter_id),
                    chapter_index=1,
                    title="Regression plan",
                    function="opening",
                    core_event="Verify the batch-loaded event plan.",
                    ending_hook="Continue.",
                )
            )
            db.commit()

        statements: list[str] = []

        def capture_statement(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture_statement)
        try:
            chapters_response = client.get(f"/api/novels/{novel_id}/chapters")
        finally:
            event.remove(engine, "before_cursor_execute", capture_statement)

        select_statements = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
        ]
        assert chapters_response.status_code == 200
        chapters = chapters_response.json()
        assert len(chapters) == 12
        assert chapters[0]["content"]
        assert chapters[0]["event_plan"]["story_event_title"] == "Regression event"
        assert chapters[0]["event_plan"]["function"] == "opening"
        assert len(select_statements) <= 4

        statements.clear()
        event.listen(engine, "before_cursor_execute", capture_statement)
        try:
            chapter_summaries_response = client.get(
                f"/api/novels/{novel_id}/chapters?include_content=false"
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture_statement)

        summary_select_statements = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
        ]
        chapter_select = next(
            statement
            for statement in summary_select_statements
            if "FROM chapters" in statement
        )
        assert chapter_summaries_response.status_code == 200
        chapter_summaries = chapter_summaries_response.json()
        assert len(chapter_summaries) == 12
        assert all(chapter["content"] == "" for chapter in chapter_summaries)
        assert all(chapter["context_snapshot"] == {} for chapter in chapter_summaries)
        assert "chapters.content" not in chapter_select.lower()
        assert "chapters.context_snapshot" not in chapter_select.lower()
        assert len(summary_select_statements) <= 4
        assert len(client.get(f"/api/novels/{novel_id}/memory").json()) == 1
        assert len(client.get("/api/meme-library").json()) == 1

    assert paths.database_path.is_file()
