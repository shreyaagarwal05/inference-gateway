from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEPENDENCY_IMPORT_ERROR: ModuleNotFoundError | None = None

try:
    from redis.exceptions import RedisError

    from app.api.admin_routes import inspect_breaker, reset_breaker, router
    from app.breaker.lua_scripts import (
        initialize_lua_scripts,
        record_failure,
        resolve_half_open_test,
    )
    from app.breaker.redis_client import (
        close_redis,
        cooldown_key,
        failures_key,
        half_open_claim_key,
        initialize_redis,
        state_key,
    )
    from app.breaker.state_machine import decide_breaker_path, get_breaker_status
except ModuleNotFoundError as exc:
    DEPENDENCY_IMPORT_ERROR = exc

TEST_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


class StateMachineTests(unittest.IsolatedAsyncioTestCase):
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
        if hasattr(self, "redis"):
            await close_redis()

    async def _clear_breaker_keys(self, tenant: str) -> None:
        await self.redis.delete(
            failures_key(tenant),
            state_key(tenant),
            cooldown_key(tenant),
            half_open_claim_key(tenant),
        )

    def _tenant(self, suffix: str) -> str:
        return f"test_state_machine_{suffix}_{uuid4().hex}"

    async def test_closed_to_open_on_threshold_breach(self) -> None:
        tenant = self._tenant("trip")
        await self._clear_breaker_keys(tenant)

        try:
            first = await record_failure(
                tenant,
                window_seconds=30,
                failure_threshold=2,
                cooldown_seconds=60,
            )
            second = await record_failure(
                tenant,
                window_seconds=30,
                failure_threshold=2,
                cooldown_seconds=60,
            )

            self.assertEqual("still_closed", first)
            self.assertEqual("tripped", second)
            self.assertEqual("open", await self.redis.get(state_key(tenant)))
            self.assertEqual("2", await self.redis.get(failures_key(tenant)))
            self.assertGreater(
                float(await self.redis.get(cooldown_key(tenant))),
                time.time(),
            )
        finally:
            await self._clear_breaker_keys(tenant)

    async def test_open_to_half_open_after_cooldown(self) -> None:
        tenant = self._tenant("cooldown")
        await self._clear_breaker_keys(tenant)

        try:
            await self.redis.set(state_key(tenant), "open")
            await self.redis.set(cooldown_key(tenant), time.time() - 1)

            path = await decide_breaker_path(tenant)

            self.assertEqual("D", path)
            self.assertEqual("half_open", await self.redis.get(state_key(tenant)))
            self.assertEqual("1", await self.redis.get(half_open_claim_key(tenant)))
        finally:
            await self._clear_breaker_keys(tenant)

    async def test_half_open_to_closed_on_test_success(self) -> None:
        tenant = self._tenant("success")
        await self._clear_breaker_keys(tenant)

        try:
            await self.redis.set(state_key(tenant), "half_open")
            await self.redis.set(failures_key(tenant), "5")
            await self.redis.set(half_open_claim_key(tenant), "1", ex=5)

            result = await resolve_half_open_test(
                tenant,
                outcome="success",
                cooldown_seconds=60,
            )

            self.assertEqual("closed", result)
            self.assertEqual("closed", await self.redis.get(state_key(tenant)))
            self.assertIsNone(await self.redis.get(failures_key(tenant)))
            self.assertIsNone(await self.redis.get(half_open_claim_key(tenant)))
        finally:
            await self._clear_breaker_keys(tenant)

    async def test_half_open_to_open_on_test_failure(self) -> None:
        tenant = self._tenant("failure")
        await self._clear_breaker_keys(tenant)

        try:
            await self.redis.set(state_key(tenant), "half_open")
            await self.redis.set(half_open_claim_key(tenant), "1", ex=5)

            result = await resolve_half_open_test(
                tenant,
                outcome="failure",
                cooldown_seconds=60,
            )

            self.assertEqual("reopened", result)
            self.assertEqual("open", await self.redis.get(state_key(tenant)))
            self.assertIsNone(await self.redis.get(half_open_claim_key(tenant)))
            self.assertGreater(
                float(await self.redis.get(cooldown_key(tenant))),
                time.time(),
            )
        finally:
            await self._clear_breaker_keys(tenant)

    async def test_manual_reset_path_closes_breaker_and_clears_counters(self) -> None:
        tenant = self._tenant("reset")
        await self._clear_breaker_keys(tenant)

        try:
            route = next(
                route
                for route in router.routes
                if route.path == "/admin/{tenant}/breaker/reset"
            )
            self.assertIn("POST", route.methods)

            await self.redis.set(state_key(tenant), "open")
            await self.redis.set(failures_key(tenant), "5")
            await self.redis.set(cooldown_key(tenant), time.time() + 60)
            await self.redis.set(half_open_claim_key(tenant), "1", ex=5)

            response = await reset_breaker(tenant)

            self.assertEqual(
                {
                    "tenant": tenant,
                    "state": "closed",
                    "failure_count": 0,
                    "cooldown_remaining_seconds": None,
                },
                response,
            )
            self.assertEqual("closed", await self.redis.get(state_key(tenant)))
            self.assertIsNone(await self.redis.get(failures_key(tenant)))
            self.assertIsNone(await self.redis.get(cooldown_key(tenant)))
            self.assertIsNone(await self.redis.get(half_open_claim_key(tenant)))
        finally:
            await self._clear_breaker_keys(tenant)

    async def test_breaker_inspection_reports_redis_state(self) -> None:
        tenant = self._tenant("inspect")
        await self._clear_breaker_keys(tenant)

        try:
            route = next(
                route for route in router.routes if route.path == "/admin/{tenant}/breaker"
            )
            self.assertIn("GET", route.methods)

            await self.redis.set(state_key(tenant), "open")
            await self.redis.set(failures_key(tenant), "3")
            await self.redis.set(cooldown_key(tenant), time.time() + 30)

            direct_status = await get_breaker_status(tenant)
            route_status = await inspect_breaker(tenant)
            self.assertEqual(route_status, direct_status)
            self.assertEqual(tenant, route_status["tenant"])
            self.assertEqual("open", route_status["state"])
            self.assertEqual(3, route_status["failure_count"])
            self.assertIsInstance(route_status["cooldown_remaining_seconds"], int)
            self.assertGreater(route_status["cooldown_remaining_seconds"], 0)
        finally:
            await self._clear_breaker_keys(tenant)
