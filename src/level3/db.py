from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import asyncpg

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schema.sql"


async def create_pool(database_url: str) -> asyncpg.Pool[asyncpg.Record]:
    pool: asyncpg.Pool[asyncpg.Record] = await asyncpg.create_pool(dsn=database_url)
    return pool


async def run_schema(pool: asyncpg.Pool[asyncpg.Record]) -> None:
    schema = SCHEMA_PATH.read_text()
    async with pool.acquire() as conn:
        await conn.execute(schema)


async def execute_query(
    pool: asyncpg.Pool[asyncpg.Record],
    query: str,
    params: list[Any] | None = None,
) -> list[dict[str, Any]] | int:
    """Execute a SQL query. Returns list of dicts for SELECT, row count string for mutations.

    Use $1, $2, etc. for parameter placeholders and pass values via params list.
    """
    args = params or []
    async with pool.acquire() as conn:
        stmt = query.strip().upper()
        if stmt.startswith("SELECT") or stmt.startswith("WITH"):
            rows = await conn.fetch(query, *args)
            return [dict(row) for row in rows]
        else:
            result = await conn.execute(query, *args)
            # asyncpg returns e.g. "INSERT 0 1" — extract the count
            parts = result.split()
            try:
                return int(parts[-1])
            except (ValueError, IndexError):
                return 0


def rows_to_json(rows: list[dict[str, Any]]) -> str:
    """Serialize query result rows to JSON, handling non-serializable types."""

    def _default(obj: object) -> str:
        return str(obj)

    return json.dumps(rows, default=_default)
