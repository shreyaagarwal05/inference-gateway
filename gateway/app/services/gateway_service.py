from __future__ import annotations

import time
from typing import Any
from fastapi import HTTPException
from ..breaker.lua_scripts import record_failure, resolve_half_open_test
from ..breaker.redis_client import cooldown_key, get_redis_client
from ..breaker.state_machine import decide_breaker_path
from ..cache.embedding_service import generate_embedding_async
from ..cache.vector_store import query_cache_with_similarity, write_cache_entry
from ..tenants.registry import get_tenant_config
from ..upstream.openai_client import OpenAIUpstreamError, create_chat_completion
from .metrics import CACHE_HITS, CACHE_MISSES, CIRCUIT_REJECTIONS, CIRCUIT_TRIPS, EMBEDDING_DURATION_MS, LLM_CALLS, REQUEST_LATENCY_MS, set_circuit_breaker_state

def _latency(tenant: str, path: str, started: float) -> None:
    REQUEST_LATENCY_MS.labels(tenant, path).observe((time.perf_counter() - started) * 1000)

async def _reject_open(tenant: str) -> None:
    redis = get_redis_client()
    cooldown = await redis.get(cooldown_key(tenant)) if redis else None
    remaining = max(0, int(float(cooldown) - time.time())) if cooldown else 0
    raise HTTPException(503, detail={"error": "circuit_open", "tenant": tenant, "retry_after_seconds": remaining})

async def process_chat_request(tenant: str, prompt: str) -> dict[str, Any]:
    config = get_tenant_config(tenant)
    if config is None:
        raise HTTPException(400, detail="Unknown X-Tenant-ID")
    started = time.perf_counter()
    embed_started = time.perf_counter()
    embedding = await generate_embedding_async(prompt)
    EMBEDDING_DURATION_MS.observe((time.perf_counter() - embed_started) * 1000)
    cached = await query_cache_with_similarity(tenant, embedding, config.similarity_threshold, config.model_version)
    if cached is not None:
        CACHE_HITS.labels(tenant).inc(); _latency(tenant, "hit", started)
        return {"response": cached[0], "cache_hit": True, "similarity_score": cached[1]}
    CACHE_MISSES.labels(tenant).inc()
    path = await decide_breaker_path(tenant)
    if path == "C":
        CIRCUIT_REJECTIONS.labels(tenant).inc(); _latency(tenant, "circuit_open", started); await _reject_open(tenant)
    try:
        response = await create_chat_completion(prompt, config.model_version, config.timeout_seconds)
    except OpenAIUpstreamError as exc:
        LLM_CALLS.labels(tenant, "failure").inc()
        if path == "D":
            await resolve_half_open_test(tenant, "failure", config.cooldown_seconds); set_circuit_breaker_state(tenant, "open"); _latency(tenant, "miss_forward", started); await _reject_open(tenant)
        result = await record_failure(tenant, config.window_seconds, config.failure_threshold, config.cooldown_seconds)
        if result == "tripped":
            CIRCUIT_TRIPS.labels(tenant).inc(); set_circuit_breaker_state(tenant, "open"); _latency(tenant, "miss_forward", started); await _reject_open(tenant)
        _latency(tenant, "miss_forward", started)
        raise HTTPException(502, detail=str(exc)) from exc
    if path == "D":
        await resolve_half_open_test(tenant, "success", config.cooldown_seconds); set_circuit_breaker_state(tenant, "closed")
    LLM_CALLS.labels(tenant, "success").inc()
    await write_cache_entry(tenant, prompt, response, embedding, config.model_version, config.ttl)
    _latency(tenant, "miss_forward", started)
    return {"response": response, "cache_hit": False}
