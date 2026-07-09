"""Administrative and infrastructure endpoints."""

from fastapi import APIRouter, HTTPException

from ..breaker.redis_client import (
    cooldown_key,
    failures_key,
    get_redis_client,
    half_open_claim_key,
    redis_is_connected,
    state_key,
)

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "redis_connected": await redis_is_connected(),
    }


@router.post("/admin/{tenant}/breaker/reset")
async def reset_breaker(tenant: str) -> dict[str, str | int | None]:
    redis = get_redis_client()
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis has not been initialized")

    await redis.delete(
        failures_key(tenant),
        cooldown_key(tenant),
        half_open_claim_key(tenant),
    )
    await redis.set(state_key(tenant), "closed")

    return {
        "tenant": tenant,
        "state": "closed",
        "failure_count": 0,
        "cooldown_remaining_seconds": None,
    }
