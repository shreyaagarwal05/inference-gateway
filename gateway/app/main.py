"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from .api.admin_routes import router as admin_router
from .breaker.redis_client import (
    close_redis,
    initialize_redis,
    redis_is_connected,
)
from .config import get_settings
from .tenants.registry import load_tenant_registry

LOGGER = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    load_tenant_registry(settings.tenants_config_path)
    initialize_redis(settings.redis_url)
    LOGGER.info("Redis connection available: %s", await redis_is_connected())

    yield

    await close_redis()


app = FastAPI(title="Multi-Tenant AI Inference Gateway", lifespan=lifespan)
app.include_router(admin_router)
