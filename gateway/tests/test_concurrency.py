from __future__ import annotations

import asyncio
import os
import sys
import unittest
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
    from app.breaker.state_machine import try_claim_half_open_test
except ModuleNotFoundError as exc:
    DEPENDENCY_IMPORT_ERROR = exc

TEST_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


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
