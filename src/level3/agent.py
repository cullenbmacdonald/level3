from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

import asyncpg
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

from level3.bootstrap_tools import BOOTSTRAP_TOOLS, ToolDefinition
from level3.capability_loader import get_loaded_capabilities
from level3.config import Settings
from level3.db import execute_query, rows_to_json
from level3.llm import chat

logger = logging.getLogger(__name__)


@dataclass
class AgentEvent:
    type: str  # "assistant", "tool_call", "tool_result", "error"
    content: str
    name: str | None = None
    arguments: dict[str, Any] | None = None


SYSTEM_PROMPT_TEMPLATE = """You are a personal assistant that can build its own capabilities.

You have 4 bootstrap tools that are always available:
execute_sql, write_capability, manage_tasks, restart.

{capabilities_section}

If a user asks you to do something you can't do yet, you can build a new capability
using write_capability. Write the Python code, define the parameter schema, and
register it. It will be immediately available.

When building capabilities, you MUST follow these rules:
- The function MUST be async: `async def name(params: dict[str, Any]) -> str:`
- The function MUST return a string (use json.dumps for structured data)
- The function name MUST match the capability name exactly
- Available packages: httpx (for HTTP requests), json, asyncio, and the Python stdlib
- Do NOT use `requests` — use `httpx` instead (it's already installed)
- If you need a package that isn't installed, tell the user to run `uv add <package>`
- Use the execute_sql tool if you need to create new tables or query data

{tasks_section}"""


async def _load_context(
    pool: asyncpg.Pool[asyncpg.Record],
    settings: Settings,
) -> tuple[list[ChatCompletionMessageParam], str]:
    """Load recent conversation history and build system prompt context."""
    # Recent messages
    rows = await execute_query(
        pool,
        "SELECT role, content, tool_call_id, tool_calls FROM conversations "
        f"ORDER BY id DESC LIMIT {settings.max_conversation_history}",
    )
    raw_history: list[dict[str, Any]] = []
    if isinstance(rows, list):
        for row in reversed(rows):
            # Parse tool_calls — asyncpg returns JSONB as strings
            raw_tc = row.get("tool_calls")
            tool_calls_list: list[dict[str, Any]] | None = None
            if raw_tc:
                tool_calls_list = json.loads(raw_tc) if isinstance(raw_tc, str) else raw_tc

            msg: dict[str, Any] = {"role": row["role"]}

            # For assistant messages with tool_calls and no text, omit content
            # entirely — some providers reject null, others reject empty string.
            if tool_calls_list and not row["content"]:
                pass
            else:
                msg["content"] = row["content"]

            if row.get("tool_call_id"):
                msg["tool_call_id"] = row["tool_call_id"]
            if tool_calls_list:
                msg["tool_calls"] = tool_calls_list
            raw_history.append(msg)

    # Trim from the front to a clean boundary — the API requires that every
    # assistant message with tool_calls is followed by its tool result messages.
    # If our LIMIT truncated mid-exchange, drop messages until we hit a user
    # or text-only assistant message.
    while raw_history and (
        raw_history[0].get("role") == "tool"
        or raw_history[0].get("tool_calls") is not None
    ):
        raw_history.pop(0)

    history: list[ChatCompletionMessageParam] = []
    for msg in raw_history:
        history.append(msg)  # type: ignore[arg-type]

    # Capabilities
    caps = get_loaded_capabilities()
    if caps:
        cap_lines = [f"- {name}: {td.description}" for name, td in caps.items()]
        capabilities_section = (
            f"You have {len(caps)} self-built capabilities:\n" + "\n".join(cap_lines)
        )
    else:
        capabilities_section = "You have no self-built capabilities yet."

    # Due tasks
    task_rows = await execute_query(
        pool,
        "SELECT id, title, details, status, due_at FROM tasks "
        "WHERE status = 'pending' AND (due_at IS NULL OR due_at <= now() + interval '1 hour') "
        "ORDER BY due_at NULLS LAST LIMIT 10",
    )
    if isinstance(task_rows, list) and task_rows:
        tasks_section = "Current tasks due soon:\n" + rows_to_json(task_rows)
    else:
        tasks_section = ""

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        capabilities_section=capabilities_section,
        tasks_section=tasks_section,
    )

    return history, system_prompt


def _collect_tools() -> tuple[list[ChatCompletionToolParam], dict[str, ToolDefinition]]:
    """Collect all tool definitions (bootstrap + capabilities)."""
    tool_map: dict[str, ToolDefinition] = {}
    schemas: list[ChatCompletionToolParam] = []

    for td in BOOTSTRAP_TOOLS:
        tool_map[td.name] = td
        schemas.append(td.schema)  # type: ignore[arg-type]

    for name, td in get_loaded_capabilities().items():
        tool_map[name] = td
        schemas.append(td.schema)  # type: ignore[arg-type]

    return schemas, tool_map


async def handle_message(
    user_message: str,
    pool: asyncpg.Pool[asyncpg.Record],
    client: AsyncOpenAI,
    settings: Settings,
) -> AsyncGenerator[AgentEvent]:
    """Process a user message through the agent loop, yielding events."""
    # Save user message
    await execute_query(
        pool,
        "INSERT INTO conversations (role, content) VALUES ($1, $2)",
        ["user", user_message],
    )

    history, system_prompt = await _load_context(pool, settings)
    tool_schemas, tool_map = _collect_tools()

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": user_message},
    ]

    for _iteration in range(settings.max_tool_iterations):
        try:
            response = await chat(client, settings.llm_model, messages, tool_schemas)
        except Exception as e:
            logger.exception("LLM API error")
            yield AgentEvent(type="error", content=f"LLM error: {e}")
            return

        tool_calls: list[dict[str, Any]] | None = response.get("tool_calls")
        content: str | None = response.get("content")

        if not tool_calls:
            # Final response
            text = content or ""
            await execute_query(
                pool,
                "INSERT INTO conversations (role, content) VALUES ($1, $2)",
                ["assistant", text],
            )
            yield AgentEvent(type="assistant", content=text)
            return

        # Process tool calls — omit content if empty for provider compatibility
        assistant_msg: dict[str, Any] = {"role": "assistant", "tool_calls": tool_calls}
        if content:
            assistant_msg["content"] = content
        messages.append(assistant_msg)  # type: ignore[arg-type]

        # Save assistant message with tool calls
        await execute_query(
            pool,
            "INSERT INTO conversations (role, content, tool_calls) "
            "VALUES ($1, $2, $3::jsonb)",
            ["assistant", content or "", json.dumps(tool_calls)],
        )

        for tc in tool_calls:
            fn_name: str = tc["function"]["name"]
            fn_args_str: str = tc["function"]["arguments"]
            tc_id: str = tc["id"]

            yield AgentEvent(
                type="tool_call",
                content=fn_args_str,
                name=fn_name,
                arguments=json.loads(fn_args_str),
            )

            tool_def = tool_map.get(fn_name)
            if not tool_def:
                result = json.dumps({"error": f"unknown tool: {fn_name}"})
            else:
                try:
                    fn_args = json.loads(fn_args_str)
                    result = await tool_def.execute(fn_args, pool)
                except Exception as e:
                    logger.exception("Tool execution error: %s", fn_name)
                    result = json.dumps({"error": str(e)})

            yield AgentEvent(type="tool_result", content=result, name=fn_name)

            messages.append(
                {"role": "tool", "content": result, "tool_call_id": tc_id},  # type: ignore[typeddict-unknown-key]
            )

            # Save tool result
            await execute_query(
                pool,
                "INSERT INTO conversations (role, content, tool_call_id) "
                "VALUES ($1, $2, $3)",
                ["tool", result, tc_id],
            )

        # Re-collect tools after each round — write_capability/restart may have
        # added new capabilities that need to be available on the next iteration.
        tool_schemas, tool_map = _collect_tools()

    # Hit max iterations
    yield AgentEvent(type="error", content="Max tool iterations reached")
