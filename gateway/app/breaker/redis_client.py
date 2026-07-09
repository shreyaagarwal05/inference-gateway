"""Redis connection lifecycle and canonical key builders for the gateway.

Redis keys must be constructed here rather than inline in cache or breaker
modules.  Keeping the naming scheme in one place makes tenant isolation
explicit and prevents small spelling differences from creating orphaned state.
"""

from __future__ import annotations

from redis.asyncio import Redis

_redis_client: Redis | None = None


def cache_key_prefix(tenant: str) -> str:
    """Return the prefix shared by all cache entries for ``tenant``."""
    return f"{tenant}:cache:"


def cache_vec_key(tenant: str, entry_uuid: str) -> str:
    """Return the RediSearch-indexed embedding hash key."""
    return f"{cache_key_prefix(tenant)}{entry_uuid}:vec"


def cache_meta_key(tenant: str, entry_uuid: str) -> str:
    """Return the metadata hash key paired with a vector key."""
    return f"{cache_key_prefix(tenant)}{entry_uuid}:meta"


def meta_key_for_vec_key(vec_key: str) -> str:
    """Return the metadata key paired with an existing vector key."""
    suffix = ":vec"
    if not vec_key.endswith(suffix):
        raise ValueError("Vector cache key must end with ':vec'")
    return f"{vec_key[:-len(suffix)]}:meta"


def index_name(tenant: str) -> str:
    """Return the tenant-isolated RediSearch index name."""
    return f"idx:{tenant}"


def breaker_key_prefix(tenant: str) -> str:
    """Return the prefix shared by all circuit-breaker keys for ``tenant``."""
    return f"breaker:{tenant}:"


def state_key(tenant: str) -> str:
    return f"{breaker_key_prefix(tenant)}state"


def failures_key(tenant: str) -> str:
    return f"{breaker_key_prefix(tenant)}failures"


def half_open_claim_key(tenant: str) -> str:
    return f"{breaker_key_prefix(tenant)}half_open_claim"


def cooldown_key(tenant: str) -> str:
    return f"{breaker_key_prefix(tenant)}cooldown_until"


def initialize_redis(redis_url: str) -> Redis:
    """Create and retain the process-wide asynchronous Redis client.

    ``Redis.from_url`` creates a lazy connection pool; the first command (the
    startup ping in ``main.py``) establishes the actual network connection.
    """
    global _redis_client
    _redis_client = Redis.from_url(redis_url, decode_responses=True)
    return _redis_client


def get_redis_client() -> Redis | None:
    """Return the initialized client, or ``None`` before application startup."""
    return _redis_client


async def redis_is_connected() -> bool:
    """Report whether Redis responds to a ping without leaking connection errors."""
    if _redis_client is None:
        return False

    try:
        return bool(await _redis_client.ping())
    except Exception:
        return False


async def close_redis() -> None:
    """Close the shared connection pool and clear the retained client."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
