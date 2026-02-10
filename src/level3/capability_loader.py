from __future__ import annotations

import importlib
import json
import logging
import sys
from typing import Any

import asyncpg

from level3.bootstrap_tools import ToolDefinition

logger = logging.getLogger(__name__)

_loaded_capabilities: dict[str, ToolDefinition] = {}


async def load_capabilities(
    pool: asyncpg.Pool[asyncpg.Record],
) -> dict[str, ToolDefinition]:
    """Load all capabilities from the database and import their modules."""
    global _loaded_capabilities  # noqa: PLW0603

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, description, file_path, tool_schema FROM capabilities"
        )

    capabilities: dict[str, ToolDefinition] = {}

    for row in rows:
        name: str = row["name"]
        try:
            module_name = f"level3.capabilities.{name}"
            if module_name in sys.modules:
                module = importlib.reload(sys.modules[module_name])
            else:
                module = importlib.import_module(module_name)

            fn = getattr(module, name)
            raw_schema = row["tool_schema"]
            if isinstance(raw_schema, str):
                schema: dict[str, Any] = json.loads(raw_schema)
            elif isinstance(raw_schema, dict):
                schema = raw_schema
            else:
                schema = {}

            capabilities[name] = ToolDefinition(
                name=name,
                description=row["description"],
                schema=schema,
                execute=fn,
            )
            logger.info("Loaded capability: %s", name)

        except Exception:
            logger.exception("Failed to load capability: %s", name)
            continue

    _loaded_capabilities = capabilities
    return capabilities


async def reload_capabilities(
    pool: asyncpg.Pool[asyncpg.Record],
) -> dict[str, ToolDefinition]:
    """Reload all capabilities (hot-reload)."""
    return await load_capabilities(pool)


def get_loaded_capabilities() -> dict[str, ToolDefinition]:
    """Return currently loaded capabilities without hitting the DB."""
    return _loaded_capabilities
