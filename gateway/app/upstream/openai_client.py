from __future__ import annotations

import httpx
from ..config import get_settings


class OpenAIUpstreamError(RuntimeError):
    pass


def _get_openai_url() -> str:
    base = get_settings().openai_base_url.rstrip("/")
    return f"{base}/chat/completions"


async def create_chat_completion(
    prompt: str, model: str, timeout_seconds: float
) -> str:
    settings = get_settings()
    key = settings.openai_api_key
    if not key:
        raise OpenAIUpstreamError("OPENAI_API_KEY is not configured")

    url = _get_openai_url()
    upstream_model = settings.openai_model or model

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": upstream_model,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise OpenAIUpstreamError(str(exc)) from exc

    if not isinstance(content, str):
        raise OpenAIUpstreamError("OpenAI returned a non-text completion")
    return content
