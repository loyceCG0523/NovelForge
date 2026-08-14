"""SQLite compatibility used by the standalone Windows application."""

from __future__ import annotations

import math
from collections import Counter

from pgvector.sqlalchemy import Vector
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.engine import Engine
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


@compiles(PgUUID, "sqlite")
def compile_uuid_sqlite(_type, _compiler, **_kwargs) -> str:
    return "CHAR(36)"


@compiles(Vector, "sqlite")
def compile_vector_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


def text_similarity(left: str | None, right: str | None) -> float:
    """Small local fallback for PostgreSQL pg_trgm similarity."""
    a = str(left or "").strip().lower()
    b = str(right or "").strip().lower()
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b))
    left_pairs = Counter(a[index:index + 2] for index in range(max(1, len(a) - 1)))
    right_pairs = Counter(b[index:index + 2] for index in range(max(1, len(b) - 1)))
    overlap = sum((left_pairs & right_pairs).values())
    total = sum(left_pairs.values()) + sum(right_pairs.values())
    return (2.0 * overlap / total) if total else 0.0


def configure_sqlite_engine(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()
        dbapi_connection.create_function("similarity", 2, text_similarity)


def cosine_similarity(left, right) -> float:
    try:
        a = [float(value) for value in left]
        b = [float(value) for value in right]
    except (TypeError, ValueError):
        return 0.0
    if not a or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0
