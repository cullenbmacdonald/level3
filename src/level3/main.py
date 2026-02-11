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
    await load_capabilities(state.pool)
    state.client = create_client(settings)
    logger.info("Level 3 ready. Provider: %s, Model: %s", settings.llm_provider, settings.llm_model)
    yield
    # Shutdown
    await state.pool.close()


app = FastAPI(lifespan=lifespan)


@app.websocket("/chat")
async def chat_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "content": "Invalid JSON"})
                continue

            content = msg.get("content", "")
            if not content:
                continue

            event: AgentEvent
            async for event in handle_message(content, state.pool, state.client, settings):
                payload: dict[str, Any] = {"type": event.type, "content": event.content}
                if event.name:
                    payload["name"] = event.name
                if event.arguments:
                    payload["arguments"] = event.arguments
                await websocket.send_json(payload)

    except WebSocketDisconnect:
        logger.info("Client disconnected")


@app.get("/api/history")
async def get_history() -> JSONResponse:
    rows = await state.pool.fetch(
        "SELECT role, content, tool_call_id, tool_calls, created_at "
        "FROM conversations ORDER BY id DESC LIMIT 200"
    )
    rows = list(reversed(rows))

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

    return JSONResponse(events)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
