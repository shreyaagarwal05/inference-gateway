"""Tenant-isolated RediSearch vector cache operations."""

from __future__ import annotations

import struct
import time
import uuid

from redis.exceptions import ResponseError

from ..breaker.redis_client import (
    cache_key_prefix,
    cache_meta_key,
    cache_vec_key,
    get_redis_client,
    index_name,
)


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


async def write_cache_entry(
    tenant: str,
    prompt: str,
    response: str,
    embedding: list[float],
    model_version: str,
    ttl: int,
) -> None:
    """Store a vector and its metadata as a paired, TTL-bound cache entry."""
    redis = get_redis_client()
    if redis is None:
        raise RuntimeError("Redis client has not been initialized")

    entry_uuid = str(uuid.uuid4())
    vec_key = cache_vec_key(tenant, entry_uuid)
    meta_key = cache_meta_key(tenant, entry_uuid)
    embedding_bytes = struct.pack(f"{len(embedding)}f", *embedding)

    await redis.hset(vec_key, "embedding", embedding_bytes)
    await redis.expire(vec_key, ttl)
    await redis.hset(
        meta_key,
        mapping={
            "prompt": prompt,
            "response": response,
            "model_version": model_version,
            "tenant_id": tenant,
            "timestamp": str(time.time()),
        },
    )
    await redis.expire(meta_key, ttl)
