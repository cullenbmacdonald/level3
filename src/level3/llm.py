from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)

from level3.config import Settings


def create_client(settings: Settings) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.get_base_url(),
        api_key=settings.get_api_key(),
    )


async def chat(
    client: AsyncOpenAI,
    model: str,
    messages: list[ChatCompletionMessageParam],
    tools: list[ChatCompletionToolParam] | None = None,
) -> dict[str, Any]:
    """Call the LLM and return the parsed response as a dict.

    Returns a dict with keys:
        - content: str | None (the assistant's text response)
        - tool_calls: list[dict] | None (any tool calls requested)
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools

    response = await client.chat.completions.create(**kwargs)
    message = response.choices[0].message

    tool_calls: list[dict[str, Any]] | None = None
    if message.tool_calls:
        tool_calls = [
            {
                "id": tc.id,
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in message.tool_calls
        ]

    return {
        "content": message.content,
        "tool_calls": tool_calls,
    }
