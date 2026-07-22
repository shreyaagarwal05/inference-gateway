from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services.gateway_service import process_chat_request

router = APIRouter()

class ChatRequest(BaseModel):
    prompt: str

@router.post("/v1/chat", response_model=None)
async def create_chat(request: ChatRequest, tenant: str | None = Header(None, alias="X-Tenant-ID")) -> dict[str, Any] | JSONResponse:
    if tenant is None:
        raise HTTPException(400, detail="Missing X-Tenant-ID")
    try:
        return await process_chat_request(tenant, request.prompt)
    except HTTPException as exc:
        if exc.status_code == 503 and isinstance(exc.detail, dict):
            return JSONResponse(status_code=503, content=exc.detail)
        raise
