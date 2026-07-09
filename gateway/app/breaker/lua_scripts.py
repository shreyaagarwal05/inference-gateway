"""Atomic Redis Lua operations for circuit-breaker transitions."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis

from .redis_client import cooldown_key, failures_key, half_open_claim_key, state_key

Script = Callable[..., Awaitable[Any]]

RECORD_FAILURE_LUA = """
local new_count = redis.call('INCR', KEYS[1])

if new_count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end

if new_count >= tonumber(ARGV[2]) then
    redis.call('SET', KEYS[2], 'open')
    redis.call('SET', KEYS[3], tonumber(ARGV[4]) + tonumber(ARGV[3]))
    return 'tripped'
end

return 'still_closed'
"""

RESOLVE_HALF_OPEN_TEST_LUA = """
redis.call('DEL', KEYS[1])

if ARGV[1] == 'success' then
    redis.call('SET', KEYS[2], 'closed')
    redis.call('DEL', KEYS[3])
    return 'closed'
end

redis.call('SET', KEYS[2], 'open')
redis.call('SET', KEYS[4], tonumber(ARGV[3]) + tonumber(ARGV[2]))
return 'reopened'
"""

_record_failure_script: Script | None = None
_resolve_half_open_test_script: Script | None = None


def initialize_lua_scripts(redis: Redis) -> None:
    """Register breaker scripts once against the shared Redis client."""
    global _record_failure_script, _resolve_half_open_test_script
    _record_failure_script = redis.register_script(RECORD_FAILURE_LUA)
    _resolve_half_open_test_script = redis.register_script(RESOLVE_HALF_OPEN_TEST_LUA)


async def record_failure(
    tenant: str,
    window_seconds: int,
    failure_threshold: int,
    cooldown_seconds: int,
) -> str:
    """Atomically record a failure and open the tenant breaker at threshold."""
    if _record_failure_script is None:
        raise RuntimeError("Lua scripts have not been initialized")

    result = await _record_failure_script(
        keys=[failures_key(tenant), state_key(tenant), cooldown_key(tenant)],
        args=[
            window_seconds,
            failure_threshold,
            cooldown_seconds,
            time.time(),
        ],
    )
    if result not in {"tripped", "still_closed"}:
        raise RuntimeError(f"Unexpected record_failure result: {result!r}")
    return result


async def resolve_half_open_test(
    tenant: str,
    outcome: str,
    cooldown_seconds: int,
) -> str:
    """Atomically close or reopen a half-open breaker after its test request."""
    if outcome not in {"success", "failure"}:
        raise ValueError("outcome must be 'success' or 'failure'")
    if _resolve_half_open_test_script is None:
        raise RuntimeError("Lua scripts have not been initialized")

    result = await _resolve_half_open_test_script(
        keys=[
            half_open_claim_key(tenant),
            state_key(tenant),
            failures_key(tenant),
            cooldown_key(tenant),
        ],
        args=[
            outcome,
            cooldown_seconds,
            time.time(),
        ],
    )
    if result not in {"closed", "reopened"}:
        raise RuntimeError(f"Unexpected resolve_half_open_test result: {result!r}")
    return result
