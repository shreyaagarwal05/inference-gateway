from __future__ import annotations

import asyncio
import os
import sys
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The runtime dependency is installed from requirements.txt. This fallback keeps
# the executor test runnable in minimal test environments without loading a model.
try:
    import sentence_transformers  # noqa: F401
except ModuleNotFoundError:
    stub = types.ModuleType("sentence_transformers")
    stub.SentenceTransformer = object
    sys.modules["sentence_transformers"] = stub

CACHE_DEPENDENCY_IMPORT_ERROR: ModuleNotFoundError | None = None
EMBEDDING_DEPENDENCY_IMPORT_ERROR: ModuleNotFoundError | None = None

try:
    from redis.exceptions import RedisError, ResponseError

    from app.breaker.redis_client import close_redis, index_name, initialize_redis
    from app.cache.vector_store import (
        create_tenant_index,
        query_cache,
        query_cache_with_similarity,
        write_cache_entry,
    )
except ModuleNotFoundError as exc:
    CACHE_DEPENDENCY_IMPORT_ERROR = exc

try:
    from app.cache import embedding_service
except ModuleNotFoundError as exc:
    EMBEDDING_DEPENDENCY_IMPORT_ERROR = exc

TEST_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


def _embedding(first_value: float = 1.0) -> list[float]:
    return [first_value] + [0.0] * 383


class CacheVectorStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if CACHE_DEPENDENCY_IMPORT_ERROR is not None:
            self.skipTest(
                f"Missing test dependency: {CACHE_DEPENDENCY_IMPORT_ERROR.name}"
            )

        self.redis = initialize_redis(TEST_REDIS_URL)
        try:
            await self.redis.ping()
        except RedisError as exc:
            self.skipTest(f"Redis is not available at {TEST_REDIS_URL}: {exc}")

        self.tenant_a = f"test_cache_a_{uuid4().hex}"
        self.tenant_b = f"test_cache_b_{uuid4().hex}"
        await create_tenant_index(self.tenant_a)
        await create_tenant_index(self.tenant_b)

    async def asyncTearDown(self) -> None:
        if hasattr(self, "redis"):
            for tenant in (getattr(self, "tenant_a", None), getattr(self, "tenant_b", None)):
                if tenant is None:
                    continue
                try:
                    await self.redis.execute_command("FT.DROPINDEX", index_name(tenant), "DD")
                except ResponseError as exc:
                    if "Unknown Index name" not in str(exc):
                        raise
            await close_redis()

    async def test_near_identical_embedding_returns_cache_hit(self) -> None:
        await write_cache_entry(
            self.tenant_a,
            prompt="What is semantic caching?",
            response="Semantic caching reuses responses for similar prompts.",
            embedding=_embedding(),
            model_version="gpt-4",
            ttl=60,
        )

        near_identical = [0.999] + [0.001] + [0.0] * 382
        result = await query_cache(
            self.tenant_a,
            near_identical,
            threshold=0.95,
            model_version="gpt-4",
        )

        self.assertEqual("Semantic caching reuses responses for similar prompts.", result)

    async def test_cache_hit_includes_cosine_similarity_for_api_envelope(self) -> None:
        await write_cache_entry(
            self.tenant_a,
            prompt="What is semantic caching?",
            response="Semantic caching reuses responses for similar prompts.",
            embedding=_embedding(),
            model_version="gpt-4",
            ttl=60,
        )

        result = await query_cache_with_similarity(
            self.tenant_a,
            _embedding(),
            threshold=0.95,
            model_version="gpt-4",
        )

        self.assertIsNotNone(result)
        response, similarity = result
        self.assertEqual("Semantic caching reuses responses for similar prompts.", response)
        self.assertAlmostEqual(1.0, similarity)

    async def test_tenant_indexes_are_structurally_isolated(self) -> None:
        await write_cache_entry(
            self.tenant_a,
            prompt="An isolated prompt",
            response="tenant_a-only response",
            embedding=_embedding(),
            model_version="gpt-4",
            ttl=60,
        )

        result = await query_cache(
            self.tenant_b,
            _embedding(),
            threshold=0.95,
            model_version="gpt-4",
        )

        self.assertIsNone(result)

    async def test_model_version_mismatch_rejects_cache_hit(self) -> None:
        await write_cache_entry(
            self.tenant_a,
            prompt="Which model produced this?",
            response="A gpt-4 response.",
            embedding=_embedding(),
            model_version="gpt-4",
            ttl=60,
        )

        result = await query_cache(
            self.tenant_a,
            _embedding(),
            threshold=0.95,
            model_version="gpt-3.5-turbo",
        )

        self.assertIsNone(result)


class _FakeEmbedding:
    def tolist(self) -> list[float]:
        return _embedding()


class _SlowEmbeddingModel:
    def encode(self, _: str) -> _FakeEmbedding:
        time.sleep(0.01)
        return _FakeEmbedding()


class EmbeddingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedding_generation_does_not_block_event_loop(self) -> None:
        if EMBEDDING_DEPENDENCY_IMPORT_ERROR is not None:
            self.skipTest(
                f"Missing test dependency: {EMBEDDING_DEPENDENCY_IMPORT_ERROR.name}"
            )

        original_model = embedding_service._model
        original_executor = embedding_service._executor
        test_executor = ThreadPoolExecutor(max_workers=4)
        loop = asyncio.get_running_loop()
        original_debug = loop.get_debug()
        loop.set_debug(False)
        embedding_service._model = _SlowEmbeddingModel()
        embedding_service._executor = test_executor

        try:
            embeddings = asyncio.gather(
                *(embedding_service.generate_embedding_async("test prompt") for _ in range(500))
            )
            started_at = time.perf_counter()
            await asyncio.sleep(0.05)
            sleep_duration = time.perf_counter() - started_at
            results = await embeddings

            self.assertLess(sleep_duration, 0.5)
            self.assertEqual(500, len(results))
            self.assertTrue(all(len(result) == 384 for result in results))
        finally:
            embedding_service._model = original_model
            embedding_service._executor = original_executor
            test_executor.shutdown(wait=True)
            loop.set_debug(original_debug)
