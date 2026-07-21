"""Administrative and infrastructure endpoints."""

from fastapi import APIRouter, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ..breaker.redis_client import (
    cooldown_key,
    failures_key,
    get_redis_client,
    half_open_claim_key,
    redis_is_connected,
    state_key,
)
from ..breaker.state_machine import get_breaker_status
from ..services.metrics import set_circuit_breaker_state

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "redis_connected": await redis_is_connected(),
    }


@router.get("/metrics")
async def metrics() -> Response:
    """Expose the Prometheus registry in the standard text format."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/admin/{tenant}/breaker")
async def inspect_breaker(tenant: str) -> dict[str, str | int | None]:
    """Return the current Redis-backed circuit-breaker state for a tenant."""
    if get_redis_client() is None:
        raise HTTPException(status_code=503, detail="Redis has not been initialized")
    return await get_breaker_status(tenant)


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
    set_circuit_breaker_state(tenant, "closed")

    return {
        "tenant": tenant,
        "state": "closed",
        "failure_count": 0,
        "cooldown_remaining_seconds": None,
    }
