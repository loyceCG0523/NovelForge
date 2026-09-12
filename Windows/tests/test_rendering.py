from __future__ import annotations

import os

import pytest

from novelforge_windows.rendering import (
    CHROMIUM_FLAGS_ENV,
    EFFECTIVE_RENDERING_ENV,
    RECOVERY_RENDERING_ENV,
    SAFE_RENDERING_ARGUMENT,
    SOFTWARE_MODE,
    configure_webengine_rendering,
    consume_gpu_recovery_request,
    has_gpu_recovery_request,
    recovery_rendering_active,
    request_safe_rendering_once,
    software_rendering_active,
)


@pytest.fixture(autouse=True)
def clear_rendering_environment(monkeypatch):
    for name in (
        CHROMIUM_FLAGS_ENV,
        EFFECTIVE_RENDERING_ENV,
        RECOVERY_RENDERING_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def test_software_rendering_is_the_default_on_every_machine(tmp_path):
    arguments = ["NovelForge.exe"]

    mode = configure_webengine_rendering(arguments, data_dir=tmp_path)

    assert mode == SOFTWARE_MODE
    assert os.getenv(CHROMIUM_FLAGS_ENV) == "--disable-gpu"
    assert arguments == ["NovelForge.exe"]
    assert software_rendering_active()


def test_existing_chromium_flags_are_preserved_when_software_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv(CHROMIUM_FLAGS_ENV, "--disable-logging")

    mode = configure_webengine_rendering(["NovelForge.exe"], data_dir=tmp_path)

    assert mode == SOFTWARE_MODE
    assert os.getenv(CHROMIUM_FLAGS_ENV) == "--disable-logging --disable-gpu"
    assert software_rendering_active()


def test_safe_rendering_argument_is_consumed_before_qapplication(tmp_path):
    arguments = ["NovelForge.exe", SAFE_RENDERING_ARGUMENT, "--another-option"]

    mode = configure_webengine_rendering(arguments, data_dir=tmp_path)

    assert mode == SOFTWARE_MODE
    assert arguments == ["NovelForge.exe", "--another-option"]
    assert os.getenv(CHROMIUM_FLAGS_ENV) == "--disable-gpu"


def test_existing_disable_gpu_flag_is_not_duplicated(tmp_path, monkeypatch):
    monkeypatch.setenv(CHROMIUM_FLAGS_ENV, "--disable-logging --disable-gpu")

    configure_webengine_rendering(["NovelForge.exe"], data_dir=tmp_path)

    assert os.getenv(CHROMIUM_FLAGS_ENV) == "--disable-logging --disable-gpu"


def test_renderer_recovery_is_one_shot_and_not_consumed_during_configuration(tmp_path):
    assert request_safe_rendering_once(
        tmp_path,
        reason="CrashedTerminationStatus",
        exit_code=123,
    )

    mode = configure_webengine_rendering(["NovelForge.exe"], data_dir=tmp_path)

    assert mode == SOFTWARE_MODE
    assert recovery_rendering_active()
    assert has_gpu_recovery_request(tmp_path)
    assert consume_gpu_recovery_request(tmp_path)
    assert not has_gpu_recovery_request(tmp_path)


def test_acceleration_hints_cannot_reenable_gpu_rendering(tmp_path, monkeypatch):
    monkeypatch.setenv(CHROMIUM_FLAGS_ENV, "--use-angle=d3d11")

    mode = configure_webengine_rendering(["NovelForge.exe"], data_dir=tmp_path)

    assert mode == SOFTWARE_MODE
    assert not recovery_rendering_active()
    assert os.getenv(CHROMIUM_FLAGS_ENV) == "--use-angle=d3d11 --disable-gpu"
