# PROJECT_CONTEXT.md — Multi-Tenant AI Inference Gateway with Semantic Caching
## Single Source of Truth — Read This Entirely Before Writing Any Code

> **Instruction to whoever (or whatever) is building this:** This file is the complete and only specification for this project. Do not invent requirements, endpoints, schemas, or behaviors not described here. If something genuinely seems ambiguous after reading this fully, stop and ask rather than guessing — but the goal of this document is that you should never need to. If you notice this document contradicting itself anywhere, treat that as a bug in the document and flag it rather than silently picking one interpretation.

---

# PART 1 — WHAT AND WHY

## 1.1 One-paragraph summary

Build a **multi-tenant AI inference gateway with semantic caching**: a FastAPI reverse-proxy that sits between application clients and the OpenAI API, converts every incoming prompt into a 384-dimensional embedding using a locally-hosted `all-MiniLM-L6-v2` model, and uses Redis Stack's RediSearch module to perform a K-Nearest Neighbor cosine-similarity search against previously cached prompts. When a semantically equivalent prompt has been seen before (similarity ≥ a per-tenant configurable threshold, default `0.95`), the stored response is returned in under 10ms — the upstream LLM is never contacted and no cost is incurred. On a cache miss, the gateway checks a Redis-backed circuit breaker before forwarding to OpenAI: if the breaker is `open` (tripped after 5 consecutive upstream failures), the request fails fast in milliseconds instead of waiting out a real timeout. The system is multi-tenant — each tenant has an isolated Redis key namespace, an isolated RediSearch index, and independently configurable similarity thresholds — horizontally scalable because all state lives in Redis rather than in-process, and fully observable through a Prometheus + Grafana stack.

**One-sentence pitch:** *Don't ask the LLM the same question twice — and if it's struggling to answer at all, stop asking it and fail fast.*

## 1.2 Motivation and goals

- **Learning goal:** implement two named, interview-relevant distributed systems patterns correctly and in combination — semantic caching (vector similarity search, not exact-match) and the circuit breaker resilience pattern — applied specifically to the LLM infrastructure cost, latency, and reliability problems that every AI-product engineering team faces.
- **Portfolio goal:** demonstrate AI infrastructure engineering, not just AI application building. This is a cost/latency/reliability optimization layer in front of an LLM — the kind of system a platform or infra team owns, not an app team.
- **Relevance to builder's background:** deliberately cross-cutting — combines vector search and embeddings (AI/ML side) with backend systems design (caching, proxying, distributed state, fault tolerance), making it relevant in interviews spanning both AI/ML and backend/systems roles.
- **Timebox:** flexible — currently in active build/iteration.

## 1.3 Explicit non-goals

- This is **not** a general-purpose API gateway. No routing rules, no arbitrary backend registration, no load balancing. The only upstream this gateway ever forwards to is a single OpenAI-compatible chat completion endpoint.
- This is **not** a feature-complete LLM orchestration framework. No prompt chaining, no agentic workflows, no RAG pipeline. Purely a caching and resilience layer in front of one LLM call shape.
- **No authentication/authorization** anywhere in this system. Tenant identity comes from a trusted `X-Tenant-ID` header, assumed to be set by an internal trusted caller (a service mesh or internal load balancer) — not validated against end-user credentials. This is a backend infra demo, not a multi-user product.
- **No** automatic or adaptive similarity threshold tuning. Thresholds are fixed, per-tenant config values loaded from `config/tenants.yaml` (see Part 6 for how those values were chosen).
- **No** fine-tuning or training of the embedding model. `all-MiniLM-L6-v2` is used as-is, off the shelf, with no modification.
- **No** distributed tracing / OpenTelemetry integration in this build.
- **No** Redis Cluster — single Redis Stack instance only. Clustering is the documented future scaling path, not part of this build.
- **No** retry-on-the-caller's-behalf logic inside the gateway. If the circuit breaker is `open` or the upstream call fails, the gateway returns an error to the caller immediately. Retrying is exclusively the calling client's decision and responsibility (see Part 3.6 for the full reasoning).
- **No** semantic cache invalidation beyond TTL expiry and model-version-aware keys. There is no endpoint or mechanism to manually flush cache entries in this build.

## 1.4 Definition of done

All of the following must be true and independently demonstrable when the build is complete:

1. Sending the same semantic question worded two different ways to the same tenant results in a cache miss on the first call (OpenAI is contacted, the response is stored) and a cache hit on the second call (Redis returns the stored response in single-digit milliseconds, OpenAI is never contacted).
2. Two different tenants asking the identical prompt text never share a cache entry — tenant_a's cache hit/miss behavior is fully independent of tenant_b's, including when both have cached responses for semantically similar prompts.
3. Each tenant's similarity threshold is independently configurable via `config/tenants.yaml`, and changing one tenant's threshold does not affect another tenant's cache behavior.
4. Forcing 5 consecutive upstream OpenAI failures for one tenant trips that tenant's circuit breaker `closed -> open`; every following cache-miss request for that tenant returns in single-digit milliseconds with a `circuit_open` fallback response — OpenAI is never contacted while the breaker is open.
5. After the configured cooldown elapses, the breaker transitions to `half_open` and allows **exactly one** test request through, regardless of how many requests arrive concurrently during that window. A successful test resets the breaker to `closed`. A failed test returns it to `open` and restarts the cooldown.
6. Running 2+ replicas of the FastAPI gateway behind a load balancer demonstrably shares one consistent cache and one consistent circuit breaker state — tripping the breaker on Replica 1 is immediately visible to Replica 2 on its next Redis read, because state lives in Redis, not in any process's memory.
7. The embedding generation step never blocks the FastAPI async event loop — demonstrated by sending concurrent requests and confirming that other in-flight requests are not stalled while one embedding computation is running on a background thread.
8. Prometheus exposes `/metrics` with at minimum: cache hit/miss counters per tenant, request latency histogram, circuit breaker state gauge per tenant, and embedding generation duration histogram. Grafana renders these as live dashboards.
9. The full system deploys via `docker compose up` with all four component types (gateway, Redis Stack, Prometheus, Grafana) on one Docker network.
10. A `README.md` documents setup steps and a demo script (Part 10 of this document is the source for that script).

---

# PART 2 — SYSTEM ARCHITECTURE

## 2.1 Component diagram

```
                              +----------------------------------+
                              |   Application Clients             |
                              |   (internal service teams calling  |
                              |    the gateway instead of OpenAI)  |
                              +----------------+-------------------+
                                               | POST /v1/chat
                                               | header: X-Tenant-ID
                                               v
                              +----------------------------------+
                              |  AI Inference Gateway (FastAPI)   |
                              |                                    |
                              |  /v1/chat                          |
                              |  /health                           |
                              |  /metrics                          |
                              |  /admin/{tenant}/breaker           |
                              +--+--------------+-----------------+
                                 |              |
              reads/writes       |              |  scraped every 15s
              vectors +          |              |
              breaker state      v              v
                          +-----------+    +-----------+
                          |  Redis    |    | Prometheus|
                          |  Stack    |    |  (9090)   |
                          | (6379)    |    +-----+-----+
                          +-----------+          |
                                                 | feeds
                                                 v
                                           +-----------+
                                           |  Grafana  |
                                           |  (3000)   |
                                           +-----------+

                                 |
                                 | forwarded only on cache miss
                                 | AND breaker closed/half_open-claimed
                                 v
                  +---------------------------------------+
                  |        OpenAI API  (external)          |
                  +---------------------------------------+
```

**Exactly four logical component types exist in this system: gateway, Redis Stack, Prometheus, Grafana.** OpenAI is an external upstream, not a component this project deploys. Do not introduce a fifth deployed component (a separate vector database, a Postgres audit store, a React dashboard, etc.) without first updating Part 1.3.

## 2.2 Component responsibility boundaries

### 2.2.1 AI Inference Gateway (FastAPI)
**Responsible for:**
- Receiving every inbound request via `POST /v1/chat` and validating the `X-Tenant-ID` header
- Loading that tenant's config (similarity threshold, TTL, model version, breaker thresholds) from the in-memory registry
- Generating the prompt's embedding via the local `all-MiniLM-L6-v2` model, always offloaded to a `ThreadPoolExecutor` — never called synchronously on the async event loop
- Querying Redis (RediSearch) for the nearest cached vector within that tenant's isolated index
- Deciding the request's path: cache-hit / cache-miss-forward / cache-miss-breaker-open (see Part 3 for the full decision tree — there are exactly four valid paths)
- On a cache miss, checking and enforcing that tenant's circuit breaker state before contacting OpenAI
- Writing new cache entries (`:vec` + `:meta` key pairs) back to Redis after a successful OpenAI response
- Recording the outcome of every OpenAI call into that tenant's circuit breaker state in Redis
- Exposing `/metrics` for Prometheus scraping and `/health` for liveness checks
- Exposing `/admin/{tenant}/breaker` for inspecting and manually resetting a tenant's breaker state

**Explicitly NOT responsible for:**
- Authenticating or authorizing the calling application — trusted internal caller assumption (see Part 1.3)
- Retrying failed OpenAI calls on the caller's behalf
- Any prompt engineering, transformation, or response post-processing — the prompt is forwarded unchanged and the response is returned unchanged
- Long-term audit history — no Postgres, no permanent request log in this build

### 2.2.2 Redis Stack
**Responsible for:**
- Holding every cached prompt's embedding vector, stored as a Hash indexed by RediSearch for KNN lookup (`{tenant}:cache:{uuid}:vec`)
- Holding the corresponding cached response payload for every cached prompt (`{tenant}:cache:{uuid}:meta`)
- Holding one RediSearch index per tenant (`idx:{tenant}`), scoped via key prefix, so a KNN search for one tenant can never return a vector belonging to another
- Holding each tenant's circuit breaker state, failure counter, half-open claim flag, and cooldown timestamp (`breaker:{tenant}:*`)
- Being the single shared source of truth for both cache contents and breaker state across all gateway replicas

**Explicitly NOT responsible for:**
- Long-term audit history of every request — cache entries have TTLs, breaker keys have explicit lifecycles (see Part 4.1), nothing in Redis is permanent
- Being talked to directly by anything other than the gateway — Prometheus, Grafana, and callers never touch Redis directly

### 2.2.3 Prometheus
**Responsible for:**
- Scraping the `/metrics` endpoint on the gateway every 15 seconds
- Storing time-series data for all metrics defined in Part 7.3
- Serving as the data source for Grafana

**Explicitly NOT responsible for:** any decision-making or request-path logic. Purely a passive metrics collector.

### 2.2.4 Grafana
**Responsible for:**
- Rendering live dashboards reading from Prometheus: cache hit rate, request latency percentiles, circuit breaker state per tenant, per-tenant request volume
- Being pre-configured via provisioning files so no manual setup is required after `docker compose up`

**Explicitly NOT responsible for:** any breaker logic, cache logic, or direct interaction with Redis. It is a display layer only.

---

# PART 3 — REQUEST LIFECYCLE AND DECISION LOGIC

## 3.1 The complete decision tree

Every request to `POST /v1/chat` resolves to exactly one of four paths. There is no fifth path. If an implementation has a code path that doesn't map to A, B, C, or D below, that is a bug.

```
Request arrives at POST /v1/chat
{"prompt": str, header: X-Tenant-ID: str}
            |
            v
   Validate X-Tenant-ID present and known in registry
   (missing or unknown -> 400 immediately, stop here)
            |
            v
   Load tenant config (threshold, ttl, model_version, breaker params)
            |
            v
   Generate embedding via all-MiniLM-L6-v2
   (ThreadPoolExecutor -- never on the event loop)
            |
            v
   RediSearch KNN search (K=1, cosine) on idx:{tenant}
            |
   +--------+----------------------------+
   v                                     v
similarity >= threshold             similarity < threshold
AND model_version matches               OR no entry in cache yet
   |                                     |
   v                                     v
[PATH A]                          GET breaker:{tenant}:state from Redis
CACHE HIT                                |
return cached response            +------+----------+------------------+
in < 10ms                         v                 v                  v
                              missing OR          "open"           "half_open"
                              "closed"               |                  |
                                 |                   |     Attempt atomic claim:
                                 |                   |     SET breaker:{tenant}:half_open_claim
                                 |                   |     "1" NX EX 5
                                 |                   |                  |
                                 |                   |         +--------+---------+
                                 |                   |         v                   v
                                 v                   v      claimed            not claimed
                             [PATH B]            [PATH C]      v                   v
                             forward to          reject      [PATH D]           [PATH C]
                             OpenAI normally     instantly   forward as         reject
                                                             THE test request   instantly
```

A missing Redis breaker-state key (first-ever request for a tenant, or Redis was just restarted) is treated identically to `"closed"`. This is a deliberate safe default — see Part 3.7.

## 3.2 Path A — Cache hit

Step-by-step, in order:

1. RediSearch returns the nearest stored vector and its cosine similarity score. Convert from cosine distance to similarity: `similarity = 1 - score`. If `similarity >= tenant_config.similarity_threshold`: this is a candidate hit.
2. Fetch the corresponding `:meta` hash from Redis using the matched key (strip `:vec`, append `:meta`).
3. Verify `meta.model_version == tenant_config.model_version`. If the versions do not match, this is **not** a valid hit — fall through to the cache miss path (Part 3.3). Stale model-version responses must never be served as if generated by the current model.
4. Return the `response` field from the metadata hash to the caller. Response envelope: `{"response": str, "cache_hit": true, "similarity_score": float}`.
5. Increment `cache_hits_total{tenant}` Prometheus counter. No write to Redis on a hit — TTL is never refreshed on read (see Part 8, Decision Log).

## 3.3 Path B — Cache miss, breaker closed (or missing key) — forward normally

Step-by-step, in order:

1. Forward the prompt to OpenAI's chat completion endpoint, using the per-tenant `timeout_seconds` from config (default `10.0`) as a hard timeout.
2. Branch on outcome:
   - **Success** (HTTP 2xx received within timeout): write the new cache entry to Redis — both `:vec` and `:meta` keys with the tenant's configured TTL (see Part 4.4 for the exact write flow). Return OpenAI's response to the caller: `{"response": str, "cache_hit": false}`. No change to the failure counter. Increment `cache_misses_total{tenant}` and `llm_calls_total{tenant, outcome="success"}`.
   - **Failure** (non-2xx, timeout, or connection error): run the `record_failure` Lua script (Part 4.3, Script 1) for this tenant.
     - If the script reports `"tripped"`: this request gets the same fallback response as Path C (`503`, `circuit_open` body — see Part 3.4 step 2 for the exact shape). Increment `circuit_trips_total{tenant}`.
     - If the script reports `"still_closed"`: return `502 Bad Gateway` with the upstream error detail to the caller. Increment `llm_calls_total{tenant, outcome="failure"}`.

## 3.4 Path C — Reject immediately (breaker open, or half-open claim not won)

1. Do not contact OpenAI. No network call is made.
2. Return immediately:
   - Status: `503`
   - Body: `{"error": "circuit_open", "tenant": "{tenant}", "retry_after_seconds": <seconds remaining until cooldown_until>}`
3. This entire path must complete in under 10ms — this is the latency number quoted in Part 1.4 item 4 and in any demo or interview context.
4. Increment `cache_misses_total{tenant}` and `circuit_rejections_total{tenant}`.

## 3.5 Path D — Half-open test (claim won)

This request is forwarded exactly like Path B steps 1–2, but its outcome has special consequences:

- **Success:** run the `resolve_half_open_test` Lua script (Part 4.3, Script 2) with outcome `"success"`. This sets state to `closed` and clears the failure counter. Write the new cache entry as in Path B success. Return OpenAI's real response to the caller: `{"response": str, "cache_hit": false}`.
- **Failure:** run the `resolve_half_open_test` Lua script with outcome `"failure"`. This sets state back to `open` and restarts the cooldown. Return the fallback response (same shape as Path C step 2) to the caller — even though this was a real attempt, the caller experiences it identically to a normal rejection.

## 3.6 Retry responsibility boundary (read this before implementing anything resembling a queue)

**The gateway never retries, queues, or replays a rejected or failed request.** A request that receives a `503` or `502` response has reached the end of its lifecycle the instant that response is sent. It is gone. Nothing about it is held in memory, in Redis, or anywhere else, waiting for the breaker to close.

If the breaker later closes and the original caller wants the work done, that requires the caller to send a brand-new, independent request, evaluated completely fresh against whatever the breaker's state is at that later moment. This is deliberate: (1) callers are already waiting synchronously and cannot be held open indefinitely; (2) queueing would silently reintroduce the slow, unpredictable waiting that the circuit breaker pattern exists to eliminate. If retry behavior is wanted, it belongs in the calling client as a client-side retry-with-backoff wrapper — explicitly out of scope for the gateway.

## 3.7 Behavior on Redis restart / missing keys

If Redis restarts and all keys are lost, every tenant is treated as `closed` by default (a missing `breaker:{tenant}:state` key reads identically to `"closed"`). Cache entries simply disappear — every request becomes a cache miss until the cache repopulates naturally through real traffic. This is a deliberate safe default: assuming health and re-discovering real failures within one `window_seconds` period is preferable to assuming failure and requiring manual intervention to recover.

---

# PART 4 — DATA DESIGN

## 4.1 Redis key schema

| Key pattern | Type | Purpose | Lifecycle / TTL |
|---|---|---|---|
| `{tenant}:cache:{uuid}:vec` | Hash (indexed by RediSearch) | The 384-dim float32 embedding blob, used for KNN search | TTL = tenant's configured `ttl` seconds (e.g. `3600`). Set on write. Never refreshed on cache hit. |
| `{tenant}:cache:{uuid}:meta` | Hash | `{prompt, response, model_version, tenant_id, timestamp}` | Same TTL as its paired `:vec` key, set at the same time. |
| `breaker:{tenant}:state` | String | Current state: `"closed"`, `"open"`, or `"half_open"` | No TTL. Persists until explicitly changed by a Lua script transition. |
| `breaker:{tenant}:failures` | String (integer via `INCR`) | Count of failures within the current sliding window | TTL = `window_seconds`, set on first increment of a fresh window. Natural expiry resets the window. |
| `breaker:{tenant}:half_open_claim` | String | Existence = a half-open test request is currently in flight | TTL = `5` seconds (safety net if the test request hangs or the process crashes without resolving it). |
| `breaker:{tenant}:cooldown_until` | String (unix timestamp float) | The timestamp after which a half-open attempt is allowed | Set on entering `open`. Overwritten on each new `open` transition. |

Cache keys are prefixed `{tenant}:cache:` and breaker keys `breaker:{tenant}:`, for easy debugging (`KEYS tenant_a:cache:*` in local dev only — never run `KEYS` in any environment resembling production).

## 4.2 RediSearch index definition (one per tenant)

```
FT.CREATE idx:{tenant}
  ON HASH
  PREFIX 1 {tenant}:cache:
  SCHEMA
    embedding VECTOR FLAT 6
      TYPE FLOAT32
      DIM 384
      DISTANCE_METRIC COSINE
```

One index is created per tenant at gateway startup, for every tenant defined in `config/tenants.yaml`. This guarantees that a KNN query against `idx:tenant_a` physically cannot return a vector belonging to `tenant_b` — tenant isolation is structural, not filter-based (see Part 8, Decision Log).

## 4.3 The two required Lua scripts

These must be implemented as actual Redis Lua scripts (loaded via `EVAL`/`EVALSHA` or a client library's scripting helper), not as multiple sequential Python calls to Redis. The entire point is atomicity — see Part 5 for why.

**Script 1 — `record_failure(tenant_key_prefix, window_seconds, failure_threshold, cooldown_seconds, now_timestamp)`**

```
1. new_count = INCR breaker:{tenant}:failures
2. IF this was the first increment of a fresh window (new_count == 1):
       EXPIRE breaker:{tenant}:failures window_seconds
3. IF new_count >= failure_threshold:
       SET breaker:{tenant}:state "open"
       SET breaker:{tenant}:cooldown_until (now_timestamp + cooldown_seconds)
       RETURN "tripped"
4. ELSE:
       RETURN "still_closed"
```

**Script 2 — `resolve_half_open_test(tenant_key_prefix, outcome, cooldown_seconds, now_timestamp)`**

```
1. DEL breaker:{tenant}:half_open_claim
2. IF outcome == "success":
       SET breaker:{tenant}:state "closed"
       DEL breaker:{tenant}:failures
       RETURN "closed"
3. ELSE:
       SET breaker:{tenant}:state "open"
       SET breaker:{tenant}:cooldown_until (now_timestamp + cooldown_seconds)
       RETURN "reopened"
```

A third operation — claiming the half-open slot — does **not** need a custom Lua script, because Redis's native `SET key value NX EX seconds` command is already atomic on its own (see Part 5.3).

## 4.4 Cache write flow (on successful OpenAI response)

```python
import uuid, struct, time

entry_uuid = str(uuid.uuid4())
vec_key  = f"{tenant_id}:cache:{entry_uuid}:vec"
meta_key = f"{tenant_id}:cache:{entry_uuid}:meta"

# Convert list[float] -> bytes (little-endian float32 array for RediSearch)
embedding_bytes = struct.pack(f"{len(embedding)}f", *embedding)

redis.hset(vec_key, "embedding", embedding_bytes)
redis.expire(vec_key, ttl)

redis.hset(meta_key, mapping={
    "prompt":        original_prompt,
    "response":      llm_response,
    "model_version": model_version,
    "tenant_id":     tenant_id,
    "timestamp":     str(time.time()),
})
redis.expire(meta_key, ttl)
```

## 4.5 Cache read flow (KNN query)

```python
from redis.commands.search.query import Query

results = redis.ft(f"idx:{tenant_id}").search(
    Query("*=>[KNN 1 @embedding $vec AS score]")
        .sort_by("score")
        .return_fields("score", "__key")
        .dialect(2),
    query_params={"vec": embedding_bytes},
)

if results.docs:
    doc = results.docs[0]
    similarity = 1 - float(doc.score)        # cosine distance -> similarity
    if similarity >= tenant_config.similarity_threshold:
        meta_key = doc.id.replace(":vec", ":meta")
        meta = redis.hgetall(meta_key)
        if meta.get("model_version") == tenant_config.model_version:
            return meta["response"]           # PATH A — valid cache hit
# fall through to cache miss (PATH B / C / D)
```

---

# PART 5 — CONCURRENCY CORRECTNESS (READ CAREFULLY — THIS IS THE CORE TECHNICAL DIFFICULTY OF THE PROJECT)

## 5.1 The general rule

**Never implement a "read a value, then decide, then write a new value" sequence as separate Redis calls from application code, if more than one request could be running that sequence at the same time.** This applies to every single piece of mutable breaker state in Part 4.1. The fix is always one of:
- A single built-in atomic Redis command (`INCR`, `SET ... NX`), or
- A Lua script, when the operation needs more than one Redis command to happen as one indivisible unit.

## 5.2 Race condition #1 — the failure counter

**The bug if done wrong:** if failure-counting were implemented as `count = GET(...)` in Python, then `count += 1`, then `SET(..., count)`, two requests failing at nearly the same instant could both read the same starting value, both compute the same incremented value, and both write back that same value — silently losing one failure from the count. Under real concurrent load this could mean the breaker never trips even though OpenAI is clearly failing repeatedly.

**The fix:** `INCR` is atomic in Redis by design — the read, the add, and the write happen as one indivisible step inside Redis itself. This is folded into Script 1 (Part 4.3) alongside the threshold check, because the threshold check and the possible state transition must also happen atomically relative to the increment (two concurrent failures must not both independently conclude "still below threshold" when their combined effect crosses it).

## 5.3 Race condition #2 — the half-open test claim

**The bug if done wrong:** if the half-open logic were "if state is half_open and no test is currently running, then I am the test," implemented as a separate check-then-act sequence, multiple requests arriving in the same instant could all see "no test running yet" and all simultaneously decide they are the test — sending a burst of traffic to a possibly-still-fragile OpenAI dependency, which is exactly what half-open testing exists to prevent.

**The fix:** `SET breaker:{tenant}:half_open_claim "1" NX EX 5` is atomic. If 100 requests call this in the same microsecond, Redis guarantees exactly one gets a success response and all 99 others get a failure response, with zero ambiguity or partial states possible. Whichever request's claim succeeds is unambiguously "the" test request; every other request takes Path C.

## 5.4 Embedding generation — blocking the event loop

This is not a Redis concurrency issue but a Python async concurrency issue. Embedding generation calls `model.encode()` on a neural network — this is CPU-bound work that takes ~10ms. Running it synchronously inside an `async def` FastAPI handler blocks the entire event loop for those 10ms. No other request can be parsed, no health check can respond, no Redis reply can be processed. Under concurrent load, requests queue up and latency spikes proportionally.

**The fix:** always offload embedding generation to a `ThreadPoolExecutor` via `loop.run_in_executor()`. Because `sentence-transformers` uses NumPy and native C code under the hood, the Python GIL is released during the actual computation, allowing multiple embeddings to run in parallel across threads even within one process.

```python
from concurrent.futures import ThreadPoolExecutor
import asyncio

_executor = ThreadPoolExecutor(max_workers=4)  # size from THREAD_POOL_WORKERS env var

async def generate_embedding_async(text: str) -> list[float]:
    loop = asyncio.get_event_loop()
    embedding = await loop.run_in_executor(_executor, _model.encode, text)
    return embedding.tolist()
```

This function is the **only** place in the codebase that calls `_model.encode`. It must never be called any other way.

---

# PART 6 — CONFIGURATION

## 6.1 How config values are determined and loaded

Per-tenant thresholds (`similarity_threshold`, `ttl`, `model_version`, `failure_threshold`, `window_seconds`, `cooldown_seconds`, `timeout_seconds`) live in `config/tenants.yaml` and are loaded into an in-memory registry at gateway startup. The gateway reads this file once at startup only — no hot-reload. Recommended starting values and how to reason about them:

- `similarity_threshold`: `0.95` is the general-purpose default. It corresponds to roughly ≤18° angular difference between prompt vectors — close enough to be semantically equivalent. Use `0.98+` for high-precision domains (legal, compliance) where slightly different phrasings may mean genuinely different things. Use `0.90` for casual FAQ-style tenants where approximate matching is acceptable.
- `ttl`: `3600` seconds (1 hour) for general content. Lower for time-sensitive content (news, financial data). Higher for stable reference content (product docs, FAQs).
- `failure_threshold`: `5` — breaker trips after 5 consecutive failures within `window_seconds`.
- `window_seconds`: `10` — failures older than 10 seconds don't count toward the trip threshold.
- `cooldown_seconds`: `60` — long enough to avoid flapping under repeated transient failures, short enough for a live demo to not require a long wait.
- `timeout_seconds`: `10.0` for the OpenAI call — must be long enough for a legitimate slow completion, short enough that a hung connection doesn't hold a thread indefinitely.

## 6.2 Example `config/tenants.yaml`

```yaml
tenants:
  tenant_a:
    similarity_threshold: 0.95
    ttl: 3600
    model_version: "gpt-4"
    failure_threshold: 5
    window_seconds: 10
    cooldown_seconds: 60
    timeout_seconds: 10.0

  tenant_b:
    similarity_threshold: 0.90
    ttl: 1800
    model_version: "gpt-3.5-turbo"
    failure_threshold: 5
    window_seconds: 10
    cooldown_seconds: 60
    timeout_seconds: 10.0
```

## 6.3 Environment variables (exhaustive list)

| Variable | Used by | Purpose |
|---|---|---|
| `REDIS_URL` | gateway | Redis Stack connection string (e.g. `redis://redis-stack:6379`) |
| `OPENAI_API_KEY` | gateway | Credential for upstream OpenAI calls — loaded from environment only, never hardcoded in any file |
| `TENANTS_CONFIG_PATH` | gateway | Path to `config/tenants.yaml`, default `/app/config/tenants.yaml` |
| `GATEWAY_PORT` | gateway | Port the FastAPI app listens on, default `8000` |
| `EMBEDDING_MODEL_NAME` | gateway | Sentence-transformers model identifier, default `all-MiniLM-L6-v2` |
| `THREAD_POOL_WORKERS` | gateway | `ThreadPoolExecutor` size for embedding generation, default `4` |

No other environment variables exist in this system. Do not invent additional config surface without updating this section first.

---

# PART 7 — API SURFACE (every endpoint, exhaustively)

## 7.1 Gateway endpoint (real traffic)

| Method | Path | Headers | Request body | Response |
|---|---|---|---|---|
| `POST` | `/v1/chat` | `X-Tenant-ID: <tenant>` (required) | `{"prompt": str}` | See Part 3 for exact response body per path. Always includes `cache_hit: bool` in the envelope. Missing or unknown `X-Tenant-ID` returns `400` immediately. |

## 7.2 Operational / admin endpoints

| Method | Path | Request body | Response |
|---|---|---|---|
| `GET` | `/health` | — | `{"status": "ok" \| "degraded", "redis_connected": bool}` |
| `GET` | `/metrics` | — | Prometheus text exposition format (see Part 7.3) |
| `GET` | `/admin/{tenant}/breaker` | — | `{"tenant": str, "state": str, "failure_count": int, "cooldown_remaining_seconds": int \| null}` |
| `POST` | `/admin/{tenant}/breaker/reset` | — | Forces state to `"closed"`, clears the failure counter. Returns updated state. |

## 7.3 Prometheus metrics (exhaustive list)

| Metric name | Type | Labels | What it measures |
|---|---|---|---|
| `cache_hits_total` | Counter | `tenant` | Count of requests served from cache (Path A) |
| `cache_misses_total` | Counter | `tenant` | Count of requests not served from cache (Paths B, C, D) |
| `llm_calls_total` | Counter | `tenant`, `outcome` (`success` / `failure`) | Count of actual outbound OpenAI calls and their outcome |
| `circuit_trips_total` | Counter | `tenant` | Count of `closed -> open` breaker transitions |
| `circuit_rejections_total` | Counter | `tenant` | Count of fast-fail rejections while breaker is open (Path C) |
| `circuit_breaker_state` | Gauge | `tenant` | `0` = closed, `1` = half_open, `2` = open |
| `request_latency_ms` | Histogram | `tenant`, `path` (`hit` / `miss_forward` / `circuit_open`) | End-to-end request latency in milliseconds |
| `embedding_duration_ms` | Histogram | — | Time spent in `generate_embedding_async`, including thread scheduling overhead |

No other endpoints or metrics exist in this system. Do not add routes or metric names without updating this section first.

---

# PART 8 — DECISION LOG (every alternative considered, and why the chosen option won)

| Decision point | Alternatives considered | Chosen | Why |
|---|---|---|---|
| Cache matching strategy | Exact-match string key (standard Redis GET/SET) / semantic vector similarity | **Semantic vector similarity** | Natural language has infinite surface variation for the same intent — exact-match would produce near-zero hit rates for real LLM traffic |
| Embedding model | Local `all-MiniLM-L6-v2` / OpenAI `text-embedding-ada-002` / OpenAI `text-embedding-3-small` / `all-mpnet-base-v2` / `bge-small-en-v1.5` | **Local `all-MiniLM-L6-v2`** | Free, ~10ms on CPU, no dependency on the very upstream being protected against; `bge-small-en-v1.5` was a close alternative but `all-MiniLM-L6-v2` has stronger ecosystem support and documentation |
| Vector store | Redis Stack (RediSearch) / Pinecone / Qdrant / Weaviate | **Redis Stack** | Consolidates vector store and breaker state store into one infrastructure component with sub-millisecond latency; no additional network hop to a separate vector DB service |
| RediSearch index type | FLAT / HNSW | **FLAT** | Cache entries collapse semantically-similar prompts rather than growing unboundedly, so vector count stays bounded and an exact O(N·D) scan remains fast at this scale; HNSW is the documented upgrade path at much larger scale |
| Where breaker state lives | In-process memory / Redis | **Redis** | Must be sub-millisecond and shared consistently across all gateway replicas; in-process memory would let replicas disagree on whether OpenAI is healthy |
| Failure counting strategy | Lifetime counter / fixed window / sliding window | **Sliding window** (via TTL-based reset) | Avoids permanent trips on stale old failures; avoids under/over-sensitivity from a counter that never resets |
| Number of concurrent half-open test requests | Allow N / allow exactly 1 | **Exactly 1** | Minimizes risk to a possibly-still-fragile recovering upstream |
| Concurrency safety mechanism | Application-level distributed locks / Redis Lua scripts + atomic commands | **Redis Lua scripts + atomic commands** | Atomicity guaranteed at the Redis layer itself; no separate lock service needed |
| Breaker granularity | One global breaker for all tenants / one breaker per tenant | **Per-tenant** | One tenant's upstream failure pattern must not trip the breaker for unrelated tenants sharing the same gateway |
| Tenant isolation mechanism | Shared RediSearch index with tenant-id filter / separate index per tenant | **Separate index per tenant** | Structural isolation — a KNN query against `idx:tenant_a` physically cannot return `tenant_b` data, rather than relying on a filter that could be misapplied or bypassed |
| Embedding generation concurrency | Synchronous call inside `async def` / `ThreadPoolExecutor` via `run_in_executor` | **`ThreadPoolExecutor`** | CPU-bound work called synchronously blocks the entire FastAPI event loop; offloading preserves responsiveness under concurrent load (see Part 5.4) |
| Cache hit TTL behavior | Refresh TTL on every read-hit / leave TTL untouched on read-hit | **Leave TTL untouched** | Refreshing on every hit risks unbounded effective lifetime for popular entries and adds an unnecessary write on the hot read path |
| Model-version cache validity | Serve cached response regardless of model version / verify model version before serving | **Verify before serving** | Prevents a response generated by an older model version from being served as if it came from the currently configured model — relevant when a tenant upgrades models |
| Retry responsibility | Gateway queues and retries internally / caller's responsibility | **Caller's responsibility** | Queueing reintroduces the slow, unpredictable waiting the breaker exists to eliminate — see Part 3.6 |
| What happens on Redis data loss | Assume worst (treat missing breaker state as `open`) / assume best (treat missing state as `closed`) | **Assume `closed`** | Safer default — re-discovers real failures quickly rather than requiring manual intervention to recover from an assumed-bad state that may not be accurate |
| Observability stack | Custom structured logging only / Prometheus + Grafana | **Prometheus + Grafana** | Industry-standard for this class of infrastructure metric; demonstrates observability-first design as a first-class concern rather than an afterthought |
| Long-term request audit | Postgres audit tables / Redis TTL-only / none | **None (Redis TTL-only)** | Kept scope honest — no Postgres in this build (see Part 1.3); full audit history is a valid future extension |

---

# PART 9 — FOLDER STRUCTURE

```
inference-gateway/
|-- gateway/
|   |-- app/
|   |   |-- main.py                      # FastAPI app entrypoint, lifespan startup (index creation, model load), route registration
|   |   |-- config.py                    # Pydantic settings — reads env vars from Part 6.3
|   |   |-- tenants/
|   |   |   |-- registry.py              # Loads config/tenants.yaml at startup, exposes get_tenant_config(tenant_id)
|   |   |-- cache/
|   |   |   |-- embedding_service.py     # Loads all-MiniLM-L6-v2 once; exposes generate_embedding_async() (Part 5.4)
|   |   |   |-- vector_store.py          # create_tenant_index(), write_cache_entry(), query_cache() (Parts 4.2, 4.4, 4.5)
|   |   |-- breaker/
|   |   |   |-- state_machine.py         # decide_breaker_path() — the only place that reads breaker:{tenant}:state (Part 3.1)
|   |   |   |-- lua_scripts.py           # The two Lua scripts from Part 4.3, loaded once at startup
|   |   |   |-- redis_client.py          # Redis connection; key-builder helpers for every pattern in Part 4.1
|   |   |-- upstream/
|   |   |   |-- openai_client.py         # Outbound call to OpenAI with per-tenant timeout; returns response text or raises
|   |   |-- api/
|   |   |   |-- chat_routes.py           # POST /v1/chat
|   |   |   |-- admin_routes.py          # GET+POST /admin/{tenant}/breaker, GET /health, GET /metrics
|   |   |-- services/
|   |       |-- gateway_service.py       # Orchestrates the full request lifecycle (Part 3) — calls embedding, cache, breaker, upstream in order
|   |       |-- metrics.py               # Defines and instruments all 8 Prometheus metrics from Part 7.3
|   |-- config/
|   |   |-- tenants.yaml                 # Per-tenant config, seeded at startup (Part 6.2)
|   |-- tests/
|   |   |-- test_state_machine.py        # Unit tests for every breaker transition in Part 8's decision log
|   |   |-- test_concurrency.py          # Tests for the two race conditions in Part 5.2 and Part 5.3, and event-loop blocking in Part 5.4
|   |   |-- test_cache.py                # Embedding dimensions, KNN hit/miss, tenant isolation, model-version rejection
|   |   |-- test_api.py                  # Integration tests against the running POST /v1/chat endpoint
|   |-- Dockerfile
|   |-- requirements.txt
|
|-- observability/
|   |-- prometheus/
|   |   |-- prometheus.yml               # Scrape config: target gateway:8000/metrics, interval 15s
|   |-- grafana/
|       |-- dashboards/
|       |   |-- inference_gateway.json   # Pre-built dashboard JSON (cache hit rate, latency, breaker state, per-tenant volume)
|       |-- provisioning/
|           |-- datasources.yml          # Auto-provisions Prometheus as Grafana datasource — no manual setup after docker compose up
|
|-- docker-compose.yml                   # Local dev — gateway, Redis Stack, Prometheus, Grafana on one Docker network
|-- README.md                            # Setup steps + demo script (Part 10 is the source)
|-- PROJECT_CONTEXT.md                   # This file
```

**Naming consistency rule:** tenant identifiers used in (a) `config/tenants.yaml` keys, (b) the `X-Tenant-ID` header value sent by callers, (c) Redis key prefixes (`{tenant}:cache:*`, `breaker:{tenant}:*`), and (d) RediSearch index names (`idx:{tenant}`) must be used identically and exactly in all four places. Any mismatch between these breaks tenant isolation and cache lookups silently, with no error — just wrong behavior.

---

# PART 10 — DEMO SCRIPT (the literal sequence to perform once built)

1. Start the stack: `docker compose up`. Confirm `GET /health` returns `{"status": "ok", "redis_connected": true}`.
2. Send `POST /v1/chat` with header `X-Tenant-ID: tenant_a` and body `{"prompt": "What is machine learning?"}`. Observe `cache_hit: false` and latency in the hundreds-of-milliseconds range — this is a real OpenAI call.
3. Send the same header with a reworded prompt: `{"prompt": "Define machine learning for me"}`. Observe `cache_hit: true` and latency under 10ms — OpenAI was never contacted.
4. Open Grafana (`http://localhost:3000`). Confirm the cache hit rate panel reflects the hit from step 3, and the latency panel shows the sub-10ms hit alongside the multi-hundred-ms miss.
5. Send the same reworded prompt from step 3 but with `X-Tenant-ID: tenant_b`. Observe `cache_hit: false` — tenant_b has no visibility into tenant_a's cache, even for an identical prompt.
6. Temporarily set an invalid `OPENAI_API_KEY` (or use a prompt that will reliably cause an error). Send 5 cache-miss requests for `tenant_a`. Watch `circuit_breaker_state{tenant="tenant_a"}` flip from `0` (closed) to `2` (open) in Grafana.
7. Send another cache-miss request for `tenant_a` — show it returning `503 circuit_open` in under 10ms instead of waiting out a timeout. Restore a valid key.
8. Call `GET /admin/tenant_a/breaker` — show `state: "open"` and `cooldown_remaining_seconds` counting down.
9. Wait out the cooldown (60s). Send one more request — show the half-open test succeeding and the breaker returning to `closed`.
10. Show `GET /metrics` raw output, pointing out nonzero values for `cache_hits_total`, `circuit_trips_total`, and `request_latency_ms` histogram buckets.

---

# PART 11 — DEPLOYMENT TOPOLOGY

## 11.1 Local development (`docker-compose.yml`)

All four component types run as containers on one shared Docker network. The gateway addresses Redis Stack by container name (`redis-stack:6379`). Prometheus addresses the gateway by container name (`gateway:8000/metrics`). All four containers start with one command: `docker compose up`.

## 11.2 Horizontal scaling (documented target — not required for the base build, but must be architecturally possible)

| Component | Scaling approach | Notes |
|---|---|---|
| Gateway | Multiple stateless replicas behind a load balancer | All state — cache vectors, breaker state, failure counters — lives in Redis. Any replica can serve any request. This is what proves the Redis-backed shared state claim in Part 1.4 item 6. |
| Redis Stack | Single instance in the base build; Redis Cluster with consistent hashing as the documented future path | Clustering is explicitly out of scope for this build (Part 1.3). |
| Embedding generation | `ThreadPoolExecutor` in the base build; a dedicated embedding microservice on GPU hardware as the documented future path if embedding latency becomes the bottleneck at high load | Not required for the base build's demonstrable claims. |

---

*End of specification. If any code or future addition contradicts this document, this document wins — update it explicitly in the same change, never let drift accumulate silently.*
