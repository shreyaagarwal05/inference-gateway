"""Administrative and infrastructure endpoints."""

from fastapi import APIRouter

from ..breaker.redis_client import redis_is_connected

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "redis_connected": await redis_is_connected(),
    }
