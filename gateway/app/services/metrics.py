"""Prometheus metrics for the gateway request lifecycle."""

from prometheus_client import Counter, Gauge, Histogram

CACHE_HITS = Counter(
    "cache_hits_total",
    "Requests served from the semantic cache.",
    ["tenant"],
)
CACHE_MISSES = Counter(
    "cache_misses_total",
    "Requests not served from the semantic cache.",
    ["tenant"],
)
LLM_CALLS = Counter(
    "llm_calls_total",
    "Outbound OpenAI calls by outcome.",
    ["tenant", "outcome"],
)
CIRCUIT_TRIPS = Counter(
    "circuit_trips_total",
    "Closed-to-open circuit-breaker transitions.",
    ["tenant"],
)
CIRCUIT_REJECTIONS = Counter(
    "circuit_rejections_total",
    "Requests rejected without an upstream call by the circuit breaker.",
    ["tenant"],
)
CIRCUIT_BREAKER_STATE = Gauge(
    "circuit_breaker_state",
    "Circuit-breaker state: closed=0, half_open=1, open=2.",
    ["tenant"],
)
REQUEST_LATENCY_MS = Histogram(
    "request_latency_ms",
    "End-to-end request latency in milliseconds.",
    ["tenant", "path"],
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000),
)
EMBEDDING_DURATION_MS = Histogram(
    "embedding_duration_ms",
    "Embedding generation duration in milliseconds.",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1_000),
)

_STATE_VALUES = {"closed": 0, "half_open": 1, "open": 2}


def set_circuit_breaker_state(tenant: str, state: str) -> None:
    """Record the state visible to the request lifecycle for ``tenant``."""
    CIRCUIT_BREAKER_STATE.labels(tenant=tenant).set(_STATE_VALUES[state])
