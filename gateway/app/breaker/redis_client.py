"""Redis connection lifecycle used by the gateway."""

from __future__ import annotations

from redis.asyncio import Redis

_redis_client: Redis | None = None


def initialize_redis(redis_url: str) -> Redis:
    global _redis_client
    _redis_client = Redis.from_url(redis_url, decode_responses=True)
    return _redis_client


def get_redis_client() -> Redis | None:
    return _redis_client


async def redis_is_connected() -> bool:
    if _redis_client is None:
        return False

    try:
        return bool(await _redis_client.ping())
    except Exception:
        return False


async def close_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
