"""Public chat-completion endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services.gateway_service import process_chat_request

router = APIRouter()


class ChatRequest(BaseModel):
    """The single supported inbound gateway request shape."""

    prompt: str


@router.post("/v1/chat")
async def create_chat(
    request: ChatRequest,
    tenant: str | None = Header(default=None, alias="X-Tenant-ID"),
) -> dict[str, Any] | JSONResponse:
    """Pass a tenant-scoped prompt into the gateway request lifecycle."""
    if tenant is None:
        raise HTTPException(status_code=400, detail="Missing X-Tenant-ID")

    try:
        return await process_chat_request(tenant, request.prompt)
    except HTTPException as exc:
        # Path C's specification requires the circuit-open object itself, not
        # FastAPI's normal {"detail": ...} wrapper around HTTPException data.
        if exc.status_code == 503 and isinstance(exc.detail, dict):
            return JSONResponse(status_code=503, content=exc.detail)
        raise
