"""Protect API keys with Windows DPAPI before writing preferences to SQLite."""

from __future__ import annotations

from novelforge_windows.infrastructure.secrets import protect_secret, unprotect_secret


PREFIX = "dpapi:"


def protect_preference_secret(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or normalized.startswith(PREFIX):
        return normalized
    return PREFIX + protect_secret(normalized)


def reveal_preference_secret(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized.startswith(PREFIX):
        return normalized
    try:
        return unprotect_secret(normalized[len(PREFIX):])
    except Exception:
        return ""
