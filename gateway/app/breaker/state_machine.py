"""Circuit-breaker state transitions for cache-miss routing decisions."""

from __future__ import annotations

import time
from math import ceil
from typing import Literal

from .redis_client import (
    cooldown_key,
    failures_key,
    get_redis_client,
    half_open_claim_key,
    state_key,
)
from ..services.metrics import set_circuit_breaker_state

BreakerPath = Literal["B", "C", "D"]
HALF_OPEN_CLAIM_TTL_SECONDS = 5


def _require_redis():
    redis = get_redis_client()
    if redis is None:
        raise RuntimeError("Redis has not been initialized")
    return redis


async def try_claim_half_open_test(tenant: str) -> bool:
    """Atomically claim the single in-flight half-open test slot."""
    redis = _require_redis()
    result = await redis.set(
        half_open_claim_key(tenant),
        "1",
        ex=HALF_OPEN_CLAIM_TTL_SECONDS,
        nx=True,
    )
    return bool(result)


async def get_breaker_status(tenant: str) -> dict[str, str | int | None]:
    """Return the inspectable Redis-backed breaker status for ``tenant``."""
    redis = _require_redis()
    state, failure_count, cooldown_until = await redis.mget(
        [state_key(tenant), failures_key(tenant), cooldown_key(tenant)]
    )
    resolved_state = state or "closed"
    set_circuit_breaker_state(tenant, resolved_state)

    cooldown_remaining: int | None = None
    if cooldown_until is not None:
        cooldown_remaining = max(0, ceil(float(cooldown_until) - time.time()))

    return {
        "tenant": tenant,
        "state": resolved_state,
        "failure_count": int(failure_count or 0),
        "cooldown_remaining_seconds": cooldown_remaining,
    }


async def decide_breaker_path(tenant: str) -> BreakerPath:
    """Return the cache-miss breaker path: B=forward, C=reject, D=test."""
    redis = _require_redis()
    state = await redis.get(state_key(tenant))

    if state is None or state == "closed":
        set_circuit_breaker_state(tenant, "closed")
        return "B"

    if state == "half_open":
        set_circuit_breaker_state(tenant, "half_open")
        return "D" if await try_claim_half_open_test(tenant) else "C"

    if state == "open":
        cooldown_until = await redis.get(cooldown_key(tenant))
        if cooldown_until is not None and float(cooldown_until) > time.time():
            set_circuit_breaker_state(tenant, "open")
            return "C"

        await redis.set(state_key(tenant), "half_open")
        set_circuit_breaker_state(tenant, "half_open")
        return "D" if await try_claim_half_open_test(tenant) else "C"

    raise RuntimeError(f"Unexpected breaker state for tenant {tenant!r}: {state!r}")
