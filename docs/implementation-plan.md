# Implementation Plan

## Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Language | Python 3.14 | Hot reload via importlib, fast iteration |
| Package manager | uv | Fast, modern, replaces pip/venv/pip-tools |
| Linter/formatter | ruff | Fast, replaces black/isort/flake8 |
| Type checking | mypy (strict) | Everything typed, no exceptions |
| Web framework | FastAPI | Async, typed, websocket support |
| LLM client | openai SDK | OpenAI-compatible spec works with Ollama, LM Studio, Anthropic |
| Database | PostgreSQL | Reliable, pgvector available later if needed |
| DB driver | asyncpg | Async Postgres, typed |
| Migrations | Raw SQL files | Simple, agent can write its own later |
| Process supervisor | Simple bash wrapper | Restarts on exit code |

## Python Practices

- **All code fully typed.** No `Any` unless absolutely unavoidable. Use `typing` generics, `TypedDict`, `dataclass`, `Protocol` where appropriate.
- **mypy strict mode.** `--strict` flag, no ignores without justification.
- **ruff for linting and formatting.** Single tool, fast, replaces the black/isort/flake8 stack.
- **uv for environment and dependencies.** `uv init`, `uv add`, `uv run`. No pip, no venv, no requirements.txt.
- **Async throughout.** FastAPI is async, asyncpg is async, the heartbeat loop is async. No blocking calls in the main loop.
- **Pydantic for validation.** Tool schemas, config, API models — all Pydantic BaseModel.
- **No classes where functions suffice.** Keep it simple. Use dataclasses/Pydantic for data, plain functions for logic.

## Project Structure

```
level3/
├── docs/
│   ├── product-brief.md
│   └── implementation-plan.md
├── src/
│   └── level3/
│       ├── __init__.py
│       ├── main.py              # FastAPI app, chat endpoint, heartbeat loop
│       ├── config.py            # Pydantic settings: provider, model, db url, etc.
│       ├── llm.py               # OpenAI client factory, chat completion wrapper
│       ├── db.py                # asyncpg connection pool, execute_sql helper
│       ├── bootstrap_tools.py   # The 4 hardcoded tools: execute_sql, write_capability, manage_tasks, restart
│       ├── capability_loader.py # Discover and import capabilities, build tool schemas
│       ├── agent.py             # Core agent loop: system prompt + tools + conversation -> LLM -> execute
│       └── capabilities/        # Agent-written tools go here
│           └── .gitkeep
├── static/
│   └── index.html               # Barebones chat UI
├── schema.sql                    # Bootstrap tables
├── pyproject.toml
└── .python-version
```

## How to Run

```bash
# Setup
uv sync
docker run -d --name level3-db -e POSTGRES_PASSWORD=level3 -e POSTGRES_DB=level3 -p 5432:5432 postgres:17

# Run (development)
uv run uvicorn level3.main:app --reload --host 0.0.0.0 --port 8000

# Run (production, with auto-restart)
# The run.sh wrapper restarts the process when the agent exits with code 42 (restart signal)
./run.sh
```

### run.sh (process supervisor)

```bash
#!/usr/bin/env bash
while true; do
    uv run uvicorn level3.main:app --host 0.0.0.0 --port 8000
    EXIT_CODE=$?
    if [ $EXIT_CODE -ne 42 ]; then
        echo "Process exited with code $EXIT_CODE, stopping."
        exit $EXIT_CODE
    fi
    echo "Restart requested, reloading..."
done
```

## Environment Variables

```bash
# Required
DATABASE_URL=postgresql://postgres:level3@localhost:5432/level3

# LLM provider (pick one)
LLM_PROVIDER=anthropic   # or "ollama" or "lmstudio"
LLM_MODEL=claude-sonnet-4-5-20250929
LLM_API_KEY=sk-...        # not needed for ollama/lmstudio

# Optional
LLM_BASE_URL=             # auto-set from provider, but can override
HEARTBEAT_INTERVAL=300    # seconds between task checks, default 300 (5 min)
```

## Configuration (config.py)

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str
    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-4-5-20250929"
    llm_api_key: str = ""
    llm_base_url: str = ""  # derived from provider if empty
    heartbeat_interval: int = 300

    model_config = {"env_file": ".env"}
```

Provider-to-base-url mapping:

| Provider | Default base_url |
|----------|-----------------|
| `ollama` | `http://localhost:11434/v1` |
| `lmstudio` | `http://localhost:1234/v1` |
| `anthropic` | `https://api.anthropic.com/v1/` |

## Bootstrap Schema (schema.sql)

```sql
CREATE TABLE IF NOT EXISTS capabilities (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    file_path TEXT NOT NULL,
    tool_schema JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS conversations (
    id SERIAL PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content TEXT NOT NULL,
    tool_call_id TEXT,
    tool_calls JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'done', 'cancelled')),
    due_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Bootstrap Tool Definitions

Each bootstrap tool is defined as a Python function with a Pydantic model for its parameters. The tool schema for the OpenAI API is generated from the Pydantic model using a helper function.

### execute_sql

```python
class ExecuteSqlParams(BaseModel):
    query: str = Field(description="SQL query to execute")

async def execute_sql(params: ExecuteSqlParams, pool: Pool) -> str:
    """Execute an arbitrary SQL query against the database. Returns rows as JSON for SELECT, or row count for mutations."""
```

Tool schema:
```json
{
    "type": "function",
    "function": {
        "name": "execute_sql",
        "description": "Execute an arbitrary SQL query against the database. Returns rows as JSON for SELECT, or row count for mutations.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "SQL query to execute"}
            },
            "required": ["query"]
        }
    }
}
```

### write_capability

```python
class WriteCapabilityParams(BaseModel):
    name: str = Field(description="Snake_case name for the capability, becomes the function and file name")
    description: str = Field(description="What this capability does, shown to the LLM as tool description")
    code: str = Field(description="Full Python source code for the capability module")
    parameters_schema: dict[str, Any] = Field(description="JSON Schema for the tool parameters (OpenAI function calling format)")

async def write_capability(params: WriteCapabilityParams, pool: Pool) -> str:
    """Write a new capability as a Python file in capabilities/, register it in the DB, and hot-reload it. The code must define an async function with the same name as the capability that accepts a single dict argument and returns a string."""
```

### manage_tasks

```python
class ManageTasksParams(BaseModel):
    action: str = Field(description="One of: create, list, get, update, complete, delete")
    id: int | None = Field(default=None, description="Task ID (required for get, update, complete, delete)")
    title: str | None = Field(default=None, description="Task title (required for create)")
    details: str | None = Field(default=None, description="Task details")
    status: str | None = Field(default=None, description="New status (for update)")
    due_at: str | None = Field(default=None, description="Due date as ISO 8601 string")

async def manage_tasks(params: ManageTasksParams, pool: Pool) -> str:
    """Create, list, update, complete, or delete tasks. Returns task data as JSON."""
```

### restart

```python
class RestartParams(BaseModel):
    mode: str = Field(default="reload", description="'reload' to hot-reload capabilities, 'full' to restart the process")

async def restart(params: RestartParams, pool: Pool) -> str:
    """Reload capabilities from disk (mode='reload') or restart the entire process (mode='full', exits with code 42)."""
```

## Capability File Convention

When the agent writes a capability, the file must follow this structure:

```python
"""One-line description of what this capability does."""

async def capability_name(params: dict[str, Any]) -> str:
    """Detailed description. This docstring is not used for the tool schema —
    the schema comes from the parameters_schema passed to write_capability."""
    # Implementation here
    # Can import anything available in the environment
    # Has no direct access to the DB pool — use execute_sql via a nested tool call,
    # or import asyncpg directly if needed
    return "result string"
```

Key rules:
- File name matches capability name: `capabilities/{name}.py`
- Must define one async function with the same name as the capability
- Function takes `params: dict[str, Any]` and returns `str`
- The tool schema (parameter names, types, descriptions) is stored in the `capabilities` DB table, not derived from the code
- Capabilities can install their own dependencies by writing a requirements note in the DB (the agent can then run `uv add <package>`)

## Capability Loader (capability_loader.py)

On startup (and on reload):
1. Query `capabilities` table for all registered capabilities
2. For each row, `importlib.import_module(f"level3.capabilities.{name}")` (or `importlib.reload` if already loaded)
3. Extract the function with `getattr(module, name)`
4. Build the OpenAI tool definition from the `tool_schema` JSONB column
5. Return a `dict[str, ToolDefinition]` mapping name -> (function, schema)

```python
@dataclass
class ToolDefinition:
    name: str
    description: str
    function: Callable[..., Awaitable[str]]
    schema: dict[str, Any]  # OpenAI function calling format
```

## Agent Loop (agent.py)

The core loop that handles a single user message:

```
1. Load recent conversation history from DB (last 50 messages, configurable)
2. Load any tasks due in the next hour
3. Build system prompt:
   - Who you are (a self-building assistant)
   - What bootstrap tools you have
   - What capabilities you've built (list from DB)
   - Any due tasks as context
4. Collect all tool definitions (bootstrap + loaded capabilities)
5. Call LLM with: system prompt + conversation history + user message + tools
6. If response contains tool calls:
   a. Execute each tool call
   b. Append tool results to messages
   c. Call LLM again with updated messages (loop until no more tool calls)
7. Save all messages (user, assistant, tool) to conversations table
8. Return final assistant message
```

Max tool call iterations per turn: 10 (prevent runaway loops).

### System Prompt

```
You are a personal assistant that can build its own capabilities.

You have {n} bootstrap tools that are always available: execute_sql, write_capability, manage_tasks, restart.

You have {m} self-built capabilities: {list of capability names and descriptions}.

If a user asks you to do something you can't do yet, you can build a new capability using write_capability. Write the Python code, define the parameter schema, and register it. It will be immediately available.

When building capabilities:
- Use the execute_sql tool if you need to create new tables or query data
- Capabilities are async Python functions that take a params dict and return a string
- You can install new packages by noting them — the user will run `uv add <package>`

Current tasks due soon:
{tasks}
```

## Websocket Protocol

Client and server communicate over a single websocket at `ws://localhost:8000/chat`.

### Client -> Server

```json
{"type": "message", "content": "user's message text"}
```

### Server -> Client

```json
{"type": "assistant", "content": "assistant's response text"}
{"type": "tool_call", "name": "execute_sql", "arguments": {"query": "SELECT ..."}}
{"type": "tool_result", "name": "execute_sql", "result": "[{\"id\": 1, ...}]"}
{"type": "error", "content": "error description"}
```

The server streams these events as the agent loop runs so the UI can show tool calls in progress. The final `assistant` message is the response to display.

## Chat UI (static/index.html)

Minimal single-page HTML:
- A `<div id="messages">` container for chat history
- An `<input>` with a send button
- Websocket connection to `/chat`
- Renders `assistant` messages as chat bubbles
- Renders `tool_call` and `tool_result` messages as collapsible debug info (grey, smaller text)
- Renders `error` messages in red
- No framework, no build step. Plain HTML + vanilla JS + minimal inline CSS.

## Error Handling

- **Tool execution failures**: catch all exceptions, return error string to the LLM as the tool result (so it can reason about the failure and retry or try a different approach)
- **LLM API failures**: retry up to 3 times with exponential backoff, then return an error message to the user via websocket
- **Capability import failures**: log the error, skip the capability, continue loading others. The agent can see the error in logs and fix the capability.
- **DB connection failures**: retry on startup, crash if Postgres is unreachable (the process supervisor will restart)

## pyproject.toml

```toml
[project]
name = "level3"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = [
    "fastapi",
    "uvicorn[standard]",
    "openai",
    "asyncpg",
    "pydantic",
    "pydantic-settings",
]

[dependency-groups]
dev = [
    "mypy",
    "ruff",
    "asyncpg-stubs",
]

[tool.ruff]
target-version = "py314"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "ANN", "B", "A", "SIM", "TCH"]

[tool.mypy]
strict = true
python_version = "3.14"
plugins = ["pydantic.mypy"]
```

## Build Order

### Phase 1: Core loop (get something running)

1. **Project setup** — `uv init`, write pyproject.toml, `uv sync`, create directory structure
2. **config.py** — Pydantic Settings class with provider/model/db config, .env support
3. **db.py** — asyncpg pool init/teardown, `execute_query()` helper that returns list[dict] or row count
4. **schema.sql** — the 3 bootstrap tables (above), applied on startup via `db.py`
5. **llm.py** — `create_client(settings)` returns OpenAI client with correct base_url. `chat(client, model, messages, tools)` calls `client.chat.completions.create()` and returns the parsed response.
6. **bootstrap_tools.py** — implement 4 tools. Each tool is a Pydantic params model + async function + OpenAI tool schema dict. Export a `BOOTSTRAP_TOOLS: list[ToolDefinition]` with all four.
7. **agent.py** — `handle_message(user_message, pool, client, settings) -> AsyncGenerator[AgentEvent]`. Loads history, builds prompt, calls LLM, executes tool calls in a loop, yields events (tool_call, tool_result, assistant), saves to DB.
8. **main.py** — FastAPI app. `lifespan` creates asyncpg pool + runs schema.sql + creates OpenAI client. Websocket `/chat` endpoint reads messages, calls `handle_message`, sends events to client. Serves `static/` directory.
9. **static/index.html** — text input, message list, websocket connection, renders events.

**Checkpoint**: you can talk to the agent in a browser, it can run SQL queries and manage tasks.

### Phase 2: Self-building

10. **capability_loader.py** — `load_capabilities(pool) -> dict[str, ToolDefinition]`. Queries DB, imports modules, returns tool map. `reload_capabilities()` re-imports changed modules.
11. **Update agent.py** — merge loaded capabilities into the tool list alongside bootstrap tools.
12. **Implement write_capability** — writes .py file to `src/level3/capabilities/`, inserts into DB, calls `reload_capabilities()`.
13. **Implement restart** — `mode="reload"` calls `reload_capabilities()`. `mode="full"` calls `sys.exit(42)`.

**Checkpoint**: the agent can build new tools for itself and use them immediately.

### Phase 3: Proactive behavior

14. **Heartbeat loop** — asyncio background task in `main.py` lifespan. Every `heartbeat_interval` seconds, queries tasks due within the next interval. If any found, runs a synthetic agent turn with context about due tasks.
15. **Conversation context** — update `agent.py` to inject due tasks into system prompt each turn.

**Checkpoint**: the agent acts on scheduled tasks without user prompting.

### Phase 4: Polish (agent-driven)

At this point, the agent should be capable enough to build the rest itself:
- Notification channels (Telegram, ntfy, email)
- Better UI
- RAG / memory search (pgvector)
- Model routing (pick model per task)
- Anything else the user asks for
