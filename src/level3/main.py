from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from level3.agent import AgentEvent, handle_message
from level3.capability_loader import load_capabilities
from level3.config import Settings
from level3.db import create_pool, run_schema
from level3.llm import create_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = Settings()


class AppState:
    pool: asyncpg.Pool[asyncpg.Record]
    client: Any


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201, ARG001
    # Startup
    logger.info("Starting Level 3...")
    state.pool = await create_pool(settings.database_url)
    await run_schema(state.pool)
    # Run migration for existing installs
    await _migrate(state.pool)
    await load_capabilities(state.pool)
    state.client = create_client(settings)
    logger.info("Level 3 ready. Provider: %s, Model: %s", settings.llm_provider, settings.llm_model)
    yield
    # Shutdown
    await state.pool.close()


async def _migrate(pool: asyncpg.Pool[asyncpg.Record]) -> None:
    """Run migrations for conversation_threads support."""
    async with pool.acquire() as conn:
        # Create threads table if missing
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_threads (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New conversation',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # Add thread_id column if missing
        col = await conn.fetchval("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'conversations' AND column_name = 'thread_id'
        """)
        if not col:
            await conn.execute("""
                ALTER TABLE conversations ADD COLUMN thread_id INTEGER REFERENCES conversation_threads(id)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversations_thread_id ON conversations(thread_id)
            """)
            # Migrate existing messages into a default thread
            existing = await conn.fetchval("SELECT COUNT(*) FROM conversations WHERE thread_id IS NULL")
            if existing and existing > 0:
                tid = await conn.fetchval("""
                    INSERT INTO conversation_threads (title) VALUES ('Initial conversation') RETURNING id
                """)
                await conn.execute("UPDATE conversations SET thread_id = $1 WHERE thread_id IS NULL", tid)


app = FastAPI(lifespan=lifespan)


# ── Thread management APIs ──

@app.get("/api/threads")
async def list_threads() -> JSONResponse:
    rows = await state.pool.fetch(
        "SELECT t.id, t.title, t.created_at, t.updated_at, "
        "  (SELECT content FROM conversations WHERE thread_id = t.id AND role = 'user' ORDER BY id LIMIT 1) AS first_message "
        "FROM conversation_threads t "
        "ORDER BY t.updated_at DESC "
        "LIMIT 100"
    )
    threads = []
    for row in rows:
        threads.append({
            "id": row["id"],
            "title": row["title"],
            "first_message": row["first_message"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        })
    return JSONResponse(threads)


@app.post("/api/threads")
async def create_thread() -> JSONResponse:
    row = await state.pool.fetchrow(
        "INSERT INTO conversation_threads (title) VALUES ('New conversation') RETURNING id, title, created_at, updated_at"
    )
    return JSONResponse({
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    })


@app.patch("/api/threads/{thread_id}")
async def update_thread(thread_id: int, body: dict[str, Any]) -> JSONResponse:
    title = body.get("title")
    if title:
        await state.pool.execute(
            "UPDATE conversation_threads SET title = $1, updated_at = now() WHERE id = $2",
            title, thread_id,
        )
    row = await state.pool.fetchrow(
        "SELECT id, title, created_at, updated_at FROM conversation_threads WHERE id = $1", thread_id
    )
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    })


@app.delete("/api/threads/{thread_id}")
async def delete_thread(thread_id: int) -> JSONResponse:
    await state.pool.execute("DELETE FROM conversations WHERE thread_id = $1", thread_id)
    await state.pool.execute("DELETE FROM conversation_threads WHERE id = $1", thread_id)
    return JSONResponse({"ok": True})


# ── Chat history API (scoped to thread) ──

@app.get("/api/threads/{thread_id}/history")
async def get_thread_history(thread_id: int) -> JSONResponse:
    rows = await state.pool.fetch(
        "SELECT role, content, tool_call_id, tool_calls, created_at "
        "FROM conversations WHERE thread_id = $1 ORDER BY id DESC LIMIT 200",
        thread_id,
    )
    rows = list(reversed(rows))
    events = _rows_to_events(rows)
    return JSONResponse(events)


@app.get("/api/history")
async def get_history() -> JSONResponse:
    """Legacy endpoint — returns most recent thread's history."""
    row = await state.pool.fetchrow(
        "SELECT id FROM conversation_threads ORDER BY updated_at DESC LIMIT 1"
    )
    if not row:
        return JSONResponse([])
    rows = await state.pool.fetch(
        "SELECT role, content, tool_call_id, tool_calls, created_at "
        "FROM conversations WHERE thread_id = $1 ORDER BY id DESC LIMIT 200",
        row["id"],
    )
    rows = list(reversed(rows))
    events = _rows_to_events(rows)
    return JSONResponse(events)


def _rows_to_events(rows: list[asyncpg.Record]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in rows:
        role = row["role"]
        content = row["content"] or ""
        tool_calls_raw = row["tool_calls"]

        if role == "user":
            events.append({"type": "user", "content": content})
        elif role == "assistant":
            if tool_calls_raw:
                tc_list = (
                    json.loads(tool_calls_raw)
                    if isinstance(tool_calls_raw, str)
                    else tool_calls_raw
                )
                for tc in tc_list:
                    fn_name = tc["function"]["name"]
                    fn_args_str = tc["function"]["arguments"]
                    try:
                        fn_args = json.loads(fn_args_str)
                    except (json.JSONDecodeError, TypeError):
                        fn_args = {}
                    events.append({
                        "type": "tool_call",
                        "name": fn_name,
                        "content": fn_args_str,
                        "arguments": fn_args,
                    })
            else:
                events.append({"type": "assistant", "content": content})
        elif role == "tool":
            tool_name = ""
            for prev in reversed(events):
                if prev.get("type") == "tool_call":
                    tool_name = prev.get("name", "")
                    break
            events.append({"type": "tool_result", "name": tool_name, "content": content})

    return events


# ── WebSocket chat (thread-aware) ──

@app.websocket("/chat")
async def chat_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    thread_id: int | None = None

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "content": "Invalid JSON"})
                continue

            # Handle thread selection
            if msg.get("type") == "set_thread":
                thread_id = msg.get("thread_id")
                await websocket.send_json({"type": "thread_set", "thread_id": thread_id})
                continue

            content = msg.get("content", "")
            if not content:
                continue

            # Auto-create thread if none set
            if thread_id is None:
                row = await state.pool.fetchrow(
                    "INSERT INTO conversation_threads (title) VALUES ('New conversation') RETURNING id"
                )
                thread_id = row["id"]
                await websocket.send_json({"type": "thread_created", "thread_id": thread_id})

            # Auto-title: use first user message as title
            msg_count = await state.pool.fetchval(
                "SELECT COUNT(*) FROM conversations WHERE thread_id = $1 AND role = 'user'",
                thread_id,
            )
            if msg_count == 0:
                title = content[:80] + ("..." if len(content) > 80 else "")
                await state.pool.execute(
                    "UPDATE conversation_threads SET title = $1 WHERE id = $2",
                    title, thread_id,
                )
                await websocket.send_json({"type": "thread_updated", "thread_id": thread_id, "title": title})

            event: AgentEvent
            async for event in handle_message(content, state.pool, state.client, settings, thread_id):
                payload: dict[str, Any] = {"type": event.type, "content": event.content}
                if event.name:
                    payload["name"] = event.name
                if event.arguments:
                    payload["arguments"] = event.arguments
                await websocket.send_json(payload)

    except WebSocketDisconnect:
        logger.info("Client disconnected")


app.mount("/", StaticFiles(directory="static", html=True), name="static")
