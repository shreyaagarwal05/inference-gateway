"""Orchestrate cache, circuit-breaker, and OpenAI request paths."""

from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException

from ..breaker.lua_scripts import record_failure, resolve_half_open_test
from ..breaker.redis_client import cooldown_key, get_redis_client
from ..breaker.state_machine import decide_breaker_path
from ..cache.embedding_service import generate_embedding_async
from ..cache.vector_store import query_cache_with_similarity, write_cache_entry
from ..tenants.registry import TenantConfig, get_tenant_config
from ..upstream.openai_client import OpenAIUpstreamError, create_chat_completion
from .metrics import (
    CACHE_HITS,
    CACHE_MISSES,
    CIRCUIT_REJECTIONS,
    CIRCUIT_TRIPS,
    EMBEDDING_DURATION_MS,
    LLM_CALLS,
    REQUEST_LATENCY_MS,
    set_circuit_breaker_state,
)


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


def _observe_request_latency(tenant: str, path: str, started_at: float) -> None:
    REQUEST_LATENCY_MS.labels(tenant=tenant, path=path).observe(
        (time.perf_counter() - started_at) * 1_000
    )


async def process_chat_request(tenant: str, prompt: str) -> dict[str, Any]:
    """Execute exactly one of the request lifecycle's paths A, B, C, or D.

    Successful cache hits and upstream forwards return the documented response
    envelopes.  Path C and failed half-open tests raise a 503; closed-breaker
    upstream failures that do not trip the breaker raise a 502.
    """
    started_at = time.perf_counter()
    config = _get_tenant_config(tenant)
    embedding_started_at = time.perf_counter()
    embedding = await generate_embedding_async(prompt)
    EMBEDDING_DURATION_MS.observe((time.perf_counter() - embedding_started_at) * 1_000)
    cached_entry = await query_cache_with_similarity(
        tenant,
        embedding,
        config.similarity_threshold,
        config.model_version,
    )
    if cached_entry is not None:
        cached_response, similarity_score = cached_entry
        CACHE_HITS.labels(tenant=tenant).inc()
        _observe_request_latency(tenant, "hit", started_at)
        return {
            "response": cached_response,
            "cache_hit": True,
            "similarity_score": similarity_score,
        }

    CACHE_MISSES.labels(tenant=tenant).inc()
    breaker_path = await decide_breaker_path(tenant)
    if breaker_path == "C":
        CIRCUIT_REJECTIONS.labels(tenant=tenant).inc()
        _observe_request_latency(tenant, "circuit_open", started_at)
        await _circuit_open_response(tenant)

    try:
        response = await create_chat_completion(
            prompt,
            config.model_version,
            config.timeout_seconds,
        )
    except OpenAIUpstreamError as exc:
        LLM_CALLS.labels(tenant=tenant, outcome="failure").inc()
        if breaker_path == "D":
            await resolve_half_open_test(tenant, "failure", config.cooldown_seconds)
            set_circuit_breaker_state(tenant, "open")
            _observe_request_latency(tenant, "miss_forward", started_at)
            await _circuit_open_response(tenant)

        outcome = await record_failure(
            tenant,
            config.window_seconds,
            config.failure_threshold,
            config.cooldown_seconds,
        )
        if outcome == "tripped":
            CIRCUIT_TRIPS.labels(tenant=tenant).inc()
            set_circuit_breaker_state(tenant, "open")
            _observe_request_latency(tenant, "miss_forward", started_at)
            await _circuit_open_response(tenant)
        _observe_request_latency(tenant, "miss_forward", started_at)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if breaker_path == "D":
        await resolve_half_open_test(tenant, "success", config.cooldown_seconds)
        set_circuit_breaker_state(tenant, "closed")

    LLM_CALLS.labels(tenant=tenant, outcome="success").inc()
    await write_cache_entry(
        tenant,
        prompt,
        response,
        embedding,
        config.model_version,
        config.ttl,
    )
    _observe_request_latency(tenant, "miss_forward", started_at)
    return {"response": response, "cache_hit": False}
