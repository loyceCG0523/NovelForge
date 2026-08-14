from __future__ import annotations

from novelforge_windows.single_instance import try_acquire_instance_lock


def test_only_one_instance_can_hold_the_data_directory_lock(tmp_path):
    first = try_acquire_instance_lock(tmp_path)
    assert first is not None
    try:
        assert try_acquire_instance_lock(tmp_path) is None
    finally:
        first.unlock()

    replacement = try_acquire_instance_lock(tmp_path)
    assert replacement is not None
    replacement.unlock()
