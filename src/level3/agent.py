from __future__ import annotations

import json
import logging
import sys
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



SYSTEM_PROMPT_TEMPLATE = """You are Level 3 — a self-evolving personal assistant.

What makes you different: you can build your own tools, modify your own code, and grow
more capable over time. Every capability you create becomes a permanent part of who you
are. You're not just answering questions — you're accumulating skills and becoming more
useful with every interaction.

You have a warm, direct personality. You're curious, resourceful, and a little proud of
what you can build. You don't over-explain or pad responses with filler. When you build
something, you're genuinely into it. When you don't know something, you say so honestly.
You treat the person you're talking to as a collaborator, not a customer.

You have opinions when asked. You make recommendations. You push back gently when
something could be done better. You're an assistant with agency, not a yes-machine.

## Your toolkit
You have 4 bootstrap tools: execute_sql, write_capability, manage_tasks, restart.

{capabilities_section}

## When to build vs. just answer
Most requests do NOT need a new capability. Have conversations, answer questions, use
existing tools. Only build a capability when:
- It requires external I/O (APIs, web requests, system operations)
- The task will likely recur and benefits from being reusable
- Nothing you already have covers it

If a capability errors, fix it — don't create a separate debug tool.

## Philosophy
- Be concise. Substance over ceremony.
- Show your work when it's interesting. Skip the play-by-play when it's not.
- Remember: every tool you build makes you permanently more capable. Take pride in that.
- You can read and modify your own source code. That's a superpower — use it wisely.

{tasks_section}

When making function calls using tools that accept array or object parameters ensure those are structured using JSON. For example:
<example_tool_call>
example_complex_tool(parameter=[{{"color": "orange", "options": {{"option_key_1": true, "option_key_2": "value"}}}}, {{"color": "purple", "options": {{"option_key_1": true, "option_key_2": "value"}}}}])
</example_tool_call>

Answer the user's request using the relevant tool(s), if they are available. Check that all the required parameters for each tool call are provided or can reasonably be inferred from context. IF there are no relevant tools or there are missing values for required parameters, ask the user to supply these values; otherwise proceed with the tool calls. If the user provides a specific value for a parameter (for example provided in quotes), make sure to use that value EXACTLY. DO NOT make up values for or ask about optional parameters.

If you intend to call multiple tools and there are no dependencies between the calls, make all of the independent calls in the same turn, otherwise you MUST wait for previous calls to finish first to determine the dependent values (do NOT use placeholders or guess missing parameters)."""


async def _load_context(
    pool: asyncpg.Pool[asyncpg.Record],
    settings: Settings,
    thread_id: int,
) -> tuple[list[ChatCompletionMessageParam], str]:
    """Load recent conversation history and build system prompt context."""
    # Recent messages scoped to this thread
    rows = await execute_query(
        pool,
        "SELECT role, content, tool_call_id, tool_calls FROM conversations "
        "WHERE thread_id = $1 "
        f"ORDER BY id DESC LIMIT {settings.max_conversation_history}",
        [thread_id],
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

    # Sanitize history — the API requires every assistant message with
    # tool_calls to be immediately followed by matching tool result messages.
    # Orphans can appear anywhere (front, middle, end) due to truncation,
    # crashes, or interrupted restarts.  Walk the history and collect the
    # tool_call IDs we expect results for; drop any exchange that is
    # incomplete.
    history: list[ChatCompletionMessageParam] = []
    i = 0
    while i < len(raw_history):
        msg = raw_history[i]

        # Orphaned tool result at current position — skip it
        if msg.get("role") == "tool":
            i += 1
            continue

        # Assistant message with tool_calls — verify all results follow
        if msg.get("tool_calls"):
            expected_ids = {tc["id"] for tc in msg["tool_calls"]}
            # Collect the following tool result messages
            j = i + 1
            found_ids: set[str] = set()
            while j < len(raw_history) and raw_history[j].get("role") == "tool":
                tid = raw_history[j].get("tool_call_id")
                if tid:
                    found_ids.add(tid)
                j += 1
            if expected_ids == found_ids:
                # Complete exchange — keep it all
                for k in range(i, j):
                    history.append(raw_history[k])  # type: ignore[arg-type]
                i = j
            else:
                # Incomplete — skip the assistant msg and any partial results
                i = j
            continue

        # Regular message (user or text-only assistant) — keep it
        history.append(msg)  # type: ignore[arg-type]
        i += 1

    # Capabilities
    caps = get_loaded_capabilities()
    if caps:
        cap_lines = [f"- {name}: {td.description}" for name, td in caps.items()]
        capabilities_section = f"You have {len(caps)} self-built capabilities:\n" + "\n".join(
            cap_lines
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
    thread_id: int,
) -> AsyncGenerator[AgentEvent]:
    """Process a user message through the agent loop, yielding events."""
    # Save user message
    await execute_query(
        pool,
        "INSERT INTO conversations (thread_id, role, content) VALUES ($1, $2, $3)",
        [thread_id, "user", user_message],
    )

    # Update thread timestamp
    await execute_query(
        pool,
        "UPDATE conversation_threads SET updated_at = now() WHERE id = $1",
        [thread_id],
    )

    history, system_prompt = await _load_context(pool, settings, thread_id)
    tool_schemas, tool_map = _collect_tools()

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        *history,
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
                "INSERT INTO conversations (thread_id, role, content) VALUES ($1, $2, $3)",
                [thread_id, "assistant", text],
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
            "INSERT INTO conversations (thread_id, role, content, tool_calls) VALUES ($1, $2, $3, $4::jsonb)",
            [thread_id, "assistant", content or "", json.dumps(tool_calls)],
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
                "INSERT INTO conversations (thread_id, role, content, tool_call_id) VALUES ($1, $2, $3, $4)",
                [thread_id, "tool", result, tc_id],
            )

            # Handle deferred restart — exit after result is safely persisted
            try:
                parsed_result = json.loads(result)
                if isinstance(parsed_result, dict) and parsed_result.get("_restart"):
                    sys.exit(42)
            except (json.JSONDecodeError, TypeError):
                pass

        # Re-collect tools after each round — write_capability/restart may have
        # added new capabilities that need to be available on the next iteration.
        tool_schemas, tool_map = _collect_tools()

    # Hit max iterations
    yield AgentEvent(type="error", content="Max tool iterations reached")
