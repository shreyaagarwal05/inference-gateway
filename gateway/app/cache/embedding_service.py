"""Asynchronous access to the shared sentence-transformers embedding model."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from sentence_transformers import SentenceTransformer

from ..config import get_settings

_model: SentenceTransformer | None = None
_executor: ThreadPoolExecutor | None = None


def initialize_embedding_service() -> None:
    """Load the embedding model and create its worker pool once per process."""
    global _executor, _model

    if _model is not None and _executor is not None:
        return

    settings = get_settings()
    if _model is None:
        _model = SentenceTransformer(settings.embedding_model_name)
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=settings.thread_pool_workers)


def shutdown_embedding_service() -> None:
    """Release the executor's worker threads during application shutdown."""
    global _executor

    if _executor is not None:
        _executor.shutdown(wait=True)
        _executor = None


async def generate_embedding_async(text: str) -> list[float]:
    """Generate an embedding without blocking the FastAPI event loop."""
    if _model is None or _executor is None:
        raise RuntimeError("Embedding service has not been initialized")

    loop = asyncio.get_running_loop()
    embedding = await loop.run_in_executor(_executor, _model.encode, text)
    return embedding.tolist()
