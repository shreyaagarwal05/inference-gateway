from prometheus_client import Counter, Gauge, Histogram

CACHE_HITS = Counter("cache_hits_total", "Cache hits", ["tenant"])
CACHE_MISSES = Counter("cache_misses_total", "Cache misses", ["tenant"])
LLM_CALLS = Counter("llm_calls_total", "LLM calls", ["tenant", "outcome"])
CIRCUIT_TRIPS = Counter("circuit_trips_total", "Circuit trips", ["tenant"])
CIRCUIT_REJECTIONS = Counter("circuit_rejections_total", "Circuit rejections", ["tenant"])
CIRCUIT_BREAKER_STATE = Gauge("circuit_breaker_state", "Breaker state", ["tenant"])
REQUEST_LATENCY_MS = Histogram("request_latency_ms", "Request latency", ["tenant", "path"], buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000))
EMBEDDING_DURATION_MS = Histogram("embedding_duration_ms", "Embedding duration", buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000))

def set_circuit_breaker_state(tenant: str, state: str) -> None:
    CIRCUIT_BREAKER_STATE.labels(tenant=tenant).set({"closed": 0, "half_open": 1, "open": 2}[state])

set_breaker_state = set_circuit_breaker_state
