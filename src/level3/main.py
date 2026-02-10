from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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


app.mount("/", StaticFiles(directory="static", html=True), name="static")
