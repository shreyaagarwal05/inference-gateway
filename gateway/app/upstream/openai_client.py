"""Async OpenAI chat-completion client used on semantic-cache misses."""

from __future__ import annotations

import httpx

from ..config import get_settings

OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIUpstreamError(RuntimeError):
    """An OpenAI request failed before a usable completion was returned."""


async def create_chat_completion(
    prompt: str,
    model: str,
    timeout_seconds: float,
) -> str:
    """Send ``prompt`` to OpenAI and return the first completion's text.

    Network, timeout, non-2xx, and malformed-response failures are exposed as
    one application-level error for the gateway's circuit-breaker handling.
    """
    api_key = get_settings().openai_api_key
    if not api_key:
        raise OpenAIUpstreamError("OPENAI_API_KEY is not configured")

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                OPENAI_CHAT_COMPLETIONS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OpenAIUpstreamError(str(exc)) from exc

    try:
        content = payload["choices"][0]["message"]["content"]
    except (IndexError, KeyError, TypeError) as exc:
        raise OpenAIUpstreamError("OpenAI returned an invalid chat completion") from exc

    if not isinstance(content, str):
        raise OpenAIUpstreamError("OpenAI returned a non-text chat completion")
    return content
