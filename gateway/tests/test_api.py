"""HTTP-level tests for the public chat gateway route."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEPENDENCY_IMPORT_ERROR: ModuleNotFoundError | None = None

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from app.api import chat_routes
except ModuleNotFoundError as exc:
    DEPENDENCY_IMPORT_ERROR = exc


class ChatRouteTests(unittest.TestCase):
    """Exercise the route boundary without requiring Redis or OpenAI."""

    def setUp(self) -> None:
        if DEPENDENCY_IMPORT_ERROR is not None:
            self.skipTest(f"Missing test dependency: {DEPENDENCY_IMPORT_ERROR.name}")

        app = FastAPI()
        app.include_router(chat_routes.router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if hasattr(self, "client"):
            self.client.close()

    def test_chat_request_delegates_to_gateway_service(self) -> None:
        service_result = {"response": "A cached answer", "cache_hit": True}
        service = AsyncMock(return_value=service_result)

        with patch.object(chat_routes, "process_chat_request", service):
            response = self.client.post(
                "/v1/chat",
                headers={"X-Tenant-ID": "tenant_a"},
                json={"prompt": "What is semantic caching?"},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual(service_result, response.json())
        service.assert_awaited_once_with("tenant_a", "What is semantic caching?")

    def test_missing_tenant_header_returns_400_without_calling_service(self) -> None:
        service = AsyncMock()

        with patch.object(chat_routes, "process_chat_request", service):
            response = self.client.post("/v1/chat", json={"prompt": "Hello"})

        self.assertEqual(400, response.status_code)
        self.assertEqual({"detail": "Missing X-Tenant-ID"}, response.json())
        service.assert_not_awaited()

    def test_circuit_open_response_is_not_wrapped_in_detail(self) -> None:
        circuit_open_body = {
            "error": "circuit_open",
            "tenant": "tenant_a",
            "retry_after_seconds": 42,
        }
        service = AsyncMock(
            side_effect=HTTPException(status_code=503, detail=circuit_open_body)
        )

        with patch.object(chat_routes, "process_chat_request", service):
            response = self.client.post(
                "/v1/chat",
                headers={"X-Tenant-ID": "tenant_a"},
                json={"prompt": "A cache miss while open"},
            )

        self.assertEqual(503, response.status_code)
        self.assertEqual(circuit_open_body, response.json())
        service.assert_awaited_once_with("tenant_a", "A cache miss while open")
