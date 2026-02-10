# Level 3: Self-Building Personal Assistant

## Vision

A personal assistant that starts with almost nothing and builds its own capabilities through conversation. The user talks to a barebones chatbot. That chatbot has one superpower: it can write code, register new tools for itself, and persist state. Over time, through natural use, the agent accumulates a growing set of self-built capabilities tailored exactly to the user's life.

This is "Level 3" of agent-native architecture: the agent doesn't just use software or access code — it IS the developer, building and modifying its own software in response to what the user needs.

## Core Idea

The agent ships with a minimal set of bootstrap primitives:

1. **Chat interface** — a way for the user to talk to it
2. **Create capabilities** — write code + register it as a callable tool
3. **Persist state** — store and query data (Postgres)
4. **Manage tasks** — a task list with details and due dates
5. **Restart/reload** — hot-reload new capabilities, full restart for core changes

Everything else — email integration, calendar, recipe parsing, grocery lists, notifications, RAG, a better UI — the agent builds itself through conversation.

## What This Is NOT

- Not a product with features. It's a seed with the capacity to grow features.
- Not an assistant with pre-built integrations. It builds its own integrations.
- Not tied to one LLM provider. The model is configurable (Ollama, LM Studio, Anthropic) via OpenAI-compatible API spec. The agent can even choose which model to use per-task.

## Bootstrap Primitives

### Tools (hardcoded, always available)

| Tool | Purpose |
|------|---------|
| `execute_sql` | Run arbitrary SQL against Postgres |
| `write_capability` | Write a Python file to capabilities/ + register in DB + hot-reload |
| `manage_tasks` | CRUD on the tasks table (create, list, update, complete, delete) |
| `restart` | Signal the process to reload capabilities or full restart |

### Schema (default tables)

| Table | Purpose |
|-------|---------|
| `capabilities` | Registry of self-built tools (name, description, file path, tool schema) |
| `conversations` | Chat history for continuity across restarts |
| `tasks` | Task list with status, details, due dates |

### Infrastructure

- Python process with FastAPI chat server
- OpenAI-compatible SDK for LLM calls (swappable provider/model)
- Postgres for persistent state
- Process supervisor for restart support
- Heartbeat timer to check tasks on interval

## Interaction Model

The user talks to the agent. If the agent doesn't have a capability it needs, it builds one. Example:

```
User: "Do I have any important emails today?"

Agent: I don't have an email tool yet. Let me build one.
  → writes capabilities/check_email.py
  → registers it in the capabilities table
  → asks user for IMAP credentials
  → uses the new tool
  → responds with email summary
```

Next time the user asks about email, the tool already exists.

## Growth Path

1. **Week 1**: Agent has bootstrap tools only. User teaches it what they need through conversation.
2. **Week 2**: Agent has email, maybe a calendar integration, a few custom tools.
3. **Month 1**: Agent has built a personal dashboard of capabilities. It proactively checks tasks, surfaces relevant info.
4. **Beyond**: Agent optimizes its own tools, builds a notification system, maybe improves its own UI.

## Key Design Principles

- **Start dumb, grow smart.** Don't over-build the bootstrap. The agent will build what it needs.
- **Persistence over intelligence.** A mediocre model with good state management beats a brilliant model with amnesia.
- **User-defined behavior.** The user tells the agent what to do and how to do it. The agent builds the implementation.
- **Hot reload over restart.** New capabilities should be available immediately. Only restart for core changes.
- **Model flexibility.** Use strong models (Claude) for capability-building, cheap/local models (Ollama) for routine tasks.
