from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEPENDENCY_IMPORT_ERROR: ModuleNotFoundError | None = None

try:
    from redis.exceptions import RedisError

    from app.breaker.lua_scripts import initialize_lua_scripts, record_failure
    from app.breaker.redis_client import (
        close_redis,
        cooldown_key,
        failures_key,
        half_open_claim_key,
        initialize_redis,
        state_key,
    )
    from app.breaker.state_machine import (
        decide_breaker_path,
        try_claim_half_open_test,
    )
except ModuleNotFoundError as exc:
    DEPENDENCY_IMPORT_ERROR = exc

TEST_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
HTTP_CONCURRENCY_TESTS_ENABLED = os.getenv("RUN_HTTP_CONCURRENCY_TESTS") == "1"
GATEWAY_BASE_URL = os.getenv("GATEWAY_BASE_URL", "http://localhost:8000").rstrip("/")
HTTP_TEST_TENANT = os.getenv("HTTP_TEST_TENANT", "tenant_a")


def _http_request(method: str, path: str, body: dict[str, str] | None = None) -> tuple[int, str]:
    """Perform one real HTTP request without introducing a test-only client."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if path == "/v1/chat":
        headers["X-Tenant-ID"] = HTTP_TEST_TENANT

    request = Request(f"{GATEWAY_BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")
    except URLError as exc:
        raise RuntimeError(f"Gateway is unavailable at {GATEWAY_BASE_URL}: {exc}") from exc


def _metric_value(
    exposition: str,
    metric: str,
    tenant: str,
    default: float | None = None,
) -> float:
    """Return the labelled Prometheus counter value for one tenant."""
    prefix = f"{metric}{{"
    tenant_label = f'tenant="{tenant}"'
    values = [
        float(line.rsplit(" ", 1)[1])
        for line in exposition.splitlines()
        if line.startswith(prefix) and tenant_label in line
    ]
    if not values:
        if default is not None:
            return default
        raise AssertionError(f"Missing {metric} metric for tenant {tenant!r}")
    return sum(values)


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if DEPENDENCY_IMPORT_ERROR is not None:
            self.skipTest(f"Missing test dependency: {DEPENDENCY_IMPORT_ERROR.name}")

        self.redis = initialize_redis(TEST_REDIS_URL)
        initialize_lua_scripts(self.redis)
        try:
            await self.redis.ping()
        except RedisError as exc:
            self.skipTest(f"Redis is not available at {TEST_REDIS_URL}: {exc}")

    async def asyncTearDown(self) -> None:
        await close_redis()

    async def _clear_breaker_keys(self, tenant: str) -> None:
        await self.redis.delete(
            failures_key(tenant),
            state_key(tenant),
            cooldown_key(tenant),
            half_open_claim_key(tenant),
        )

    async def test_concurrent_record_failure_counts_every_failure(self) -> None:
        tenant = f"test_concurrency_failure_{uuid4().hex}"
        request_count = 25
        await self._clear_breaker_keys(tenant)

        try:
            results = await asyncio.gather(
                *(
                    record_failure(
                        tenant,
                        window_seconds=30,
                        failure_threshold=request_count + 1,
                        cooldown_seconds=60,
                    )
                    for _ in range(request_count)
                )
            )

            failure_count = await self.redis.get(failures_key(tenant))

            self.assertEqual(["still_closed"] * request_count, results)
            self.assertEqual(str(request_count), failure_count)
        finally:
            await self._clear_breaker_keys(tenant)

    async def test_concurrent_half_open_claim_allows_exactly_one_winner(self) -> None:
        tenant = f"test_concurrency_claim_{uuid4().hex}"
        request_count = 25
        await self._clear_breaker_keys(tenant)

        try:
            results = await asyncio.gather(
                *(try_claim_half_open_test(tenant) for _ in range(request_count))
            )

            self.assertEqual(1, sum(results))
            self.assertEqual(request_count - 1, results.count(False))
            self.assertEqual("1", await self.redis.get(half_open_claim_key(tenant)))
            self.assertGreater(await self.redis.ttl(half_open_claim_key(tenant)), 0)
        finally:
            await self._clear_breaker_keys(tenant)

    async def test_expired_open_breaker_allows_exactly_one_path_d(self) -> None:
        tenant = f"test_concurrency_expired_open_{uuid4().hex}"
        request_count = 25
        await self._clear_breaker_keys(tenant)

        try:
            await self.redis.set(state_key(tenant), "open")
            await self.redis.set(cooldown_key(tenant), time.time() - 1)

            paths = await asyncio.gather(
                *(decide_breaker_path(tenant) for _ in range(request_count))
            )

            self.assertEqual(1, paths.count("D"))
            self.assertEqual(request_count - 1, paths.count("C"))
            self.assertEqual("half_open", await self.redis.get(state_key(tenant)))
            self.assertEqual("1", await self.redis.get(half_open_claim_key(tenant)))
        finally:
            await self._clear_breaker_keys(tenant)

    async def test_expired_open_breaker_allows_one_upstream_http_request(self) -> None:
        """Prove the half-open claim under 20 concurrent real HTTP requests.

        This is opt-in because it intentionally changes the live state for
        ``HTTP_TEST_TENANT`` and sends traffic to the configured gateway.
        """
        if not HTTP_CONCURRENCY_TESTS_ENABLED:
            self.skipTest("Set RUN_HTTP_CONCURRENCY_TESTS=1 to run real HTTP traffic")

        try:
            metrics_status, metrics_before = await asyncio.to_thread(
                _http_request, "GET", "/metrics"
            )
        except RuntimeError as exc:
            self.skipTest(str(exc))
        if metrics_status != 200:
            self.fail(f"GET /metrics returned {metrics_status}")

        llm_before = _metric_value(
            metrics_before, "llm_calls_total", HTTP_TEST_TENANT, default=0
        )
        rejections_before = _metric_value(
            metrics_before, "circuit_rejections_total", HTTP_TEST_TENANT, default=0
        )
        await self._clear_breaker_keys(HTTP_TEST_TENANT)
        await self.redis.set(state_key(HTTP_TEST_TENANT), "open")
        await self.redis.set(cooldown_key(HTTP_TEST_TENANT), time.time() - 1)

        try:
            request_count = 20
            responses = await asyncio.gather(
                *(
                    asyncio.to_thread(
                        _http_request,
                        "POST",
                        "/v1/chat",
                        {"prompt": f"phase-5-half-open-{uuid4().hex}"},
                    )
                    for _ in range(request_count)
                )
            )
            metrics_status, metrics_after = await asyncio.to_thread(
                _http_request, "GET", "/metrics"
            )

            self.assertEqual(200, metrics_status)
            self.assertTrue(all(status in {200, 503} for status, _ in responses))
            self.assertEqual(
                1,
                _metric_value(metrics_after, "llm_calls_total", HTTP_TEST_TENANT)
                - llm_before,
            )
            self.assertEqual(
                request_count - 1,
                _metric_value(
                    metrics_after, "circuit_rejections_total", HTTP_TEST_TENANT
                )
                - rejections_before,
            )
        finally:
            await self._clear_breaker_keys(HTTP_TEST_TENANT)
