"""Orchestrate cache, circuit-breaker, and OpenAI request paths."""

from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException

from ..breaker.lua_scripts import record_failure, resolve_half_open_test
from ..breaker.redis_client import cooldown_key, get_redis_client
from ..breaker.state_machine import decide_breaker_path
from ..cache.embedding_service import generate_embedding_async
from ..cache.vector_store import query_cache, write_cache_entry
from ..tenants.registry import TenantConfig, get_tenant_config
from ..upstream.openai_client import OpenAIUpstreamError, create_chat_completion


def _get_tenant_config(tenant: str) -> TenantConfig:
    config = get_tenant_config(tenant)
    if config is None:
        raise HTTPException(status_code=400, detail="Unknown X-Tenant-ID")
    return config


async def _retry_after_seconds(tenant: str) -> int:
    redis = get_redis_client()
    if redis is None:
        raise RuntimeError("Redis client has not been initialized")

    cooldown_until = await redis.get(cooldown_key(tenant))
    if cooldown_until is None:
        return 0
    return max(0, int(float(cooldown_until) - time.time()))


async def _circuit_open_response(tenant: str) -> None:
    raise HTTPException(
        status_code=503,
        detail={
            "error": "circuit_open",
            "tenant": tenant,
            "retry_after_seconds": await _retry_after_seconds(tenant),
        },
    )


async def process_chat_request(tenant: str, prompt: str) -> dict[str, Any]:
    """Execute exactly one of the request lifecycle's paths A, B, C, or D.

    Successful cache hits and upstream forwards return the documented response
    envelopes.  Path C and failed half-open tests raise a 503; closed-breaker
    upstream failures that do not trip the breaker raise a 502.
    """
    config = _get_tenant_config(tenant)
    embedding = await generate_embedding_async(prompt)
    cached_response = await query_cache(
        tenant,
        embedding,
        config.similarity_threshold,
        config.model_version,
    )
    if cached_response is not None:
        return {"response": cached_response, "cache_hit": True}

    breaker_path = await decide_breaker_path(tenant)
    if breaker_path == "C":
        await _circuit_open_response(tenant)

    try:
        response = await create_chat_completion(
            prompt,
            config.model_version,
            config.timeout_seconds,
        )
    except OpenAIUpstreamError as exc:
        if breaker_path == "D":
            await resolve_half_open_test(tenant, "failure", config.cooldown_seconds)
            await _circuit_open_response(tenant)

        outcome = await record_failure(
            tenant,
            config.window_seconds,
            config.failure_threshold,
            config.cooldown_seconds,
        )
        if outcome == "tripped":
            await _circuit_open_response(tenant)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if breaker_path == "D":
        await resolve_half_open_test(tenant, "success", config.cooldown_seconds)

    await write_cache_entry(
        tenant,
        prompt,
        response,
        embedding,
        config.model_version,
        config.ttl,
    )
    return {"response": response, "cache_hit": False}
