"""Tenant-isolated RediSearch vector cache operations."""

from __future__ import annotations

from redis.exceptions import ResponseError

from ..breaker.redis_client import cache_key_prefix, get_redis_client, index_name


async def create_tenant_index(tenant: str) -> None:
    """Create the tenant's 384-dimensional cosine-similarity vector index."""
    redis = get_redis_client()
    if redis is None:
        raise RuntimeError("Redis client has not been initialized")

    try:
        await redis.execute_command(
            "FT.CREATE",
            index_name(tenant),
            "ON",
            "HASH",
            "PREFIX",
            1,
            cache_key_prefix(tenant),
            "SCHEMA",
            "embedding",
            "VECTOR",
            "FLAT",
            6,
            "TYPE",
            "FLOAT32",
            "DIM",
            384,
            "DISTANCE_METRIC",
            "COSINE",
        )
    except ResponseError as exc:
        if "Index already exists" not in str(exc):
            raise
