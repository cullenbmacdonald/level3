from __future__ import annotations

import importlib
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import asyncpg
from pydantic import BaseModel, Field

from level3.db import execute_query, rows_to_json


@dataclass
class ToolDefinition:
    name: str
    description: str
    schema: dict[str, Any]
    execute: Any  # async callable, typed loosely to avoid circular deps


# --- execute_sql ---


class ExecuteSqlParams(BaseModel):
    query: str = Field(description="SQL query to execute")


async def execute_sql(
    params: dict[str, Any],
    pool: asyncpg.Pool[asyncpg.Record],
) -> str:
    parsed = ExecuteSqlParams(**params)
    result = await execute_query(pool, parsed.query)
    if isinstance(result, list):
        return rows_to_json(result)
    return json.dumps({"rows_affected": result})


EXECUTE_SQL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "execute_sql",
        "description": (
            "Execute an arbitrary SQL query against the database. "
            "Returns rows as JSON for SELECT, or row count for mutations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "SQL query to execute"},
            },
            "required": ["query"],
        },
    },
}


# --- write_capability ---


class WriteCapabilityParams(BaseModel):
    name: str = Field(
        description="Snake_case name for the capability, becomes the function and file name"
    )
    description: str = Field(
        description="What this capability does, shown to the LLM as tool description"
    )
    code: str = Field(description="Full Python source code for the capability module")
    parameters_schema: dict[str, Any] = Field(
        description="JSON Schema for the tool parameters (OpenAI function calling format)"
    )


async def write_capability(
    params: dict[str, Any],
    pool: asyncpg.Pool[asyncpg.Record],
) -> str:
    from level3.capability_loader import reload_capabilities

    parsed = WriteCapabilityParams(**params)

    # Validate syntax before writing
    try:
        compile(parsed.code, f"{parsed.name}.py", "exec")
    except SyntaxError as e:
        return json.dumps({
            "error": "syntax_error",
            "message": str(e),
            "line": e.lineno,
            "offset": e.offset,
        })

    # Write the file
    import level3.capabilities as cap_pkg

    cap_dir = cap_pkg.__path__[0]
    file_path = f"{cap_dir}/{parsed.name}.py"

    with open(file_path, "w") as f:
        f.write(parsed.code)

    # Verify it imports correctly
    try:
        module_name = f"level3.capabilities.{parsed.name}"
        if module_name in sys.modules:
            importlib.reload(sys.modules[module_name])
        else:
            importlib.import_module(module_name)
        # Verify the expected function exists
        mod = sys.modules[module_name]
        if not hasattr(mod, parsed.name):
            os.remove(file_path)
            return json.dumps({
                "error": "missing_function",
                "message": (
                    f"Module must define an async function named '{parsed.name}'"
                ),
            })
    except Exception as e:
        os.remove(file_path)
        return json.dumps({
            "error": "import_error",
            "message": str(e),
        })

    # Register in DB
    tool_schema: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": parsed.name,
            "description": parsed.description,
            "parameters": parsed.parameters_schema,
        },
    }

    await execute_query(
        pool,
        "INSERT INTO capabilities (name, description, file_path, tool_schema) "
        "VALUES ($1, $2, $3, $4::jsonb) "
        "ON CONFLICT (name) DO UPDATE SET "
        "description = EXCLUDED.description, "
        "file_path = EXCLUDED.file_path, "
        "tool_schema = EXCLUDED.tool_schema, "
        "updated_at = now()",
        [parsed.name, parsed.description, file_path, json.dumps(tool_schema)],
    )

    # Hot-reload
    await reload_capabilities(pool)

    return json.dumps({"status": "ok", "capability": parsed.name, "file": file_path})


WRITE_CAPABILITY_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "write_capability",
        "description": (
            "Write a new capability as a Python file in capabilities/, register it in the DB, "
            "and hot-reload it. The code must define an async function with the same name as "
            "the capability that accepts a single dict argument and returns a string."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Snake_case name for the capability",
                },
                "description": {
                    "type": "string",
                    "description": "What this capability does",
                },
                "code": {
                    "type": "string",
                    "description": "Full Python source code for the capability module",
                },
                "parameters_schema": {
                    "type": "object",
                    "description": "JSON Schema for the tool parameters",
                },
            },
            "required": ["name", "description", "code", "parameters_schema"],
        },
    },
}


# --- manage_tasks ---


class ManageTasksParams(BaseModel):
    action: str = Field(description="One of: create, list, get, update, complete, delete")
    id: int | None = Field(default=None, description="Task ID")
    title: str | None = Field(default=None, description="Task title")
    details: str | None = Field(default=None, description="Task details")
    status: str | None = Field(default=None, description="New status")
    due_at: str | None = Field(default=None, description="Due date as ISO 8601 string")


async def manage_tasks(
    params: dict[str, Any],
    pool: asyncpg.Pool[asyncpg.Record],
) -> str:
    parsed = ManageTasksParams(**params)

    if parsed.action == "create":
        if not parsed.title:
            return json.dumps({"error": "title is required for create"})
        result = await execute_query(
            pool,
            "INSERT INTO tasks (title, details, due_at) "
            "VALUES ($1, $2, $3::timestamptz) "
            "RETURNING id, title, status, due_at, created_at",
            [parsed.title, parsed.details or "", parsed.due_at],
        )
        return rows_to_json(result) if isinstance(result, list) else json.dumps({"id": result})

    elif parsed.action == "list":
        rows = await execute_query(
            pool,
            "SELECT id, title, status, due_at, created_at FROM tasks "
            "WHERE status NOT IN ('done', 'cancelled') "
            "ORDER BY due_at NULLS LAST, id",
        )
        return rows_to_json(rows) if isinstance(rows, list) else "[]"

    elif parsed.action == "get":
        if parsed.id is None:
            return json.dumps({"error": "id is required for get"})
        rows = await execute_query(
            pool,
            "SELECT * FROM tasks WHERE id = $1",
            [parsed.id],
        )
        return rows_to_json(rows) if isinstance(rows, list) else "[]"

    elif parsed.action == "update":
        if parsed.id is None:
            return json.dumps({"error": "id is required for update"})
        sets: list[str] = []
        params: list[Any] = []
        idx = 1
        if parsed.title is not None:
            sets.append(f"title = ${idx}")
            params.append(parsed.title)
            idx += 1
        if parsed.details is not None:
            sets.append(f"details = ${idx}")
            params.append(parsed.details)
            idx += 1
        if parsed.status is not None:
            sets.append(f"status = ${idx}")
            params.append(parsed.status)
            idx += 1
        if parsed.due_at is not None:
            sets.append(f"due_at = ${idx}::timestamptz")
            params.append(parsed.due_at)
            idx += 1
        if not sets:
            return json.dumps({"error": "nothing to update"})
        sets.append("updated_at = now()")
        params.append(parsed.id)
        result = await execute_query(
            pool,
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ${idx} "
            "RETURNING id, title, status, due_at",
            params,
        )
        return rows_to_json(result) if isinstance(result, list) else json.dumps({"updated": 0})

    elif parsed.action == "complete":
        if parsed.id is None:
            return json.dumps({"error": "id is required for complete"})
        result = await execute_query(
            pool,
            "UPDATE tasks SET status = 'done', updated_at = now() "
            "WHERE id = $1 RETURNING id, title, status",
            [parsed.id],
        )
        return rows_to_json(result) if isinstance(result, list) else json.dumps({"updated": 0})

    elif parsed.action == "delete":
        if parsed.id is None:
            return json.dumps({"error": "id is required for delete"})
        result = await execute_query(
            pool,
            "DELETE FROM tasks WHERE id = $1",
            [parsed.id],
        )
        count = result if isinstance(result, int) else 0
        return json.dumps({"deleted": count})

    else:
        return json.dumps({"error": f"unknown action: {parsed.action}"})


MANAGE_TASKS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "manage_tasks",
        "description": (
            "Create, list, update, complete, or delete tasks. Returns task data as JSON."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "get", "update", "complete", "delete"],
                    "description": "The action to perform",
                },
                "id": {"type": "integer", "description": "Task ID"},
                "title": {"type": "string", "description": "Task title"},
                "details": {"type": "string", "description": "Task details"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done", "cancelled"],
                    "description": "New status",
                },
                "due_at": {
                    "type": "string",
                    "description": "Due date as ISO 8601 string",
                },
            },
            "required": ["action"],
        },
    },
}


# --- restart ---


class RestartParams(BaseModel):
    mode: str = Field(
        default="reload",
        description="'reload' to hot-reload capabilities, 'full' to restart the process",
    )


async def restart(
    params: dict[str, Any],
    pool: asyncpg.Pool[asyncpg.Record],
) -> str:
    parsed = RestartParams(**params)
    if parsed.mode == "full":
        sys.exit(42)
    elif parsed.mode == "reload":
        from level3.capability_loader import reload_capabilities

        await reload_capabilities(pool)
        return json.dumps({"status": "reloaded"})
    else:
        return json.dumps({"error": f"unknown mode: {parsed.mode}"})


RESTART_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "restart",
        "description": (
            "Reload capabilities from disk (mode='reload') or restart the entire process "
            "(mode='full', exits with code 42)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["reload", "full"],
                    "description": "'reload' to hot-reload capabilities, 'full' to restart",
                },
            },
        },
    },
}


# --- Registry ---

TOOL_EXECUTORS: dict[str, Any] = {
    "execute_sql": execute_sql,
    "write_capability": write_capability,
    "manage_tasks": manage_tasks,
    "restart": restart,
}

BOOTSTRAP_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="execute_sql",
        description=EXECUTE_SQL_SCHEMA["function"]["description"],
        schema=EXECUTE_SQL_SCHEMA,
        execute=execute_sql,
    ),
    ToolDefinition(
        name="write_capability",
        description=WRITE_CAPABILITY_SCHEMA["function"]["description"],
        schema=WRITE_CAPABILITY_SCHEMA,
        execute=write_capability,
    ),
    ToolDefinition(
        name="manage_tasks",
        description=MANAGE_TASKS_SCHEMA["function"]["description"],
        schema=MANAGE_TASKS_SCHEMA,
        execute=manage_tasks,
    ),
    ToolDefinition(
        name="restart",
        description=RESTART_SCHEMA["function"]["description"],
        schema=RESTART_SCHEMA,
        execute=restart,
    ),
]
