# INSTRUCTIONS.md — Build Instructions for Multi-Tenant AI Inference Gateway with Semantic Caching

> **Read `PROJECT_CONTEXT.md` in full before reading this file.** That file is the specification — what to build, every schema, every endpoint, every design decision. This file is the execution plan — the order to build things in, what to verify at each step, and the rules to follow while coding. Do not start writing code before both files have been read completely.

---

## Prerequisites — verify all of these before writing a single line of code

This project runs entirely on the local machine. No cloud accounts, no external sign-ins required except an OpenAI API key. Everything else is free.

**Target OS: Windows** (these instructions are written for Windows specifically).

### Required tools

| Tool | Purpose | Needed from |
|---|---|---|
| Docker Desktop | Runs all containers (gateway, Redis Stack, Prometheus, Grafana) | Phase 1 |
| docker-compose | Orchestrates local dev environment | Phase 1 |
| Python (3.11+) | Runs the gateway (FastAPI + sentence-transformers) | Phase 1 |
| An OpenAI API key | Upstream LLM calls on cache miss | Phase 1 |

### Install steps (Windows — run in PowerShell as Administrator)

**Docker Desktop** — download and install manually from `https://www.docker.com/products/docker-desktop/`
- During install: select "Use WSL 2 instead of Hyper-V" (default on modern Windows)
- After install: open Docker Desktop, wait for the whale icon in the taskbar to stop animating (fully started)
- Docker Desktop bundles `docker-compose` — no separate install needed

**Python 3.11+** — if not already installed:
```powershell
winget install Python.Python.3.11
```

### Prerequisites verification gate — run ALL of these before starting Phase 1

```powershell
docker --version
docker compose version
python --version
```

Every command must return a version number without error. If any fail, fix that tool's install before proceeding.

Additionally, confirm Docker Desktop is fully running:
```powershell
docker run hello-world
```

This should print "Hello from Docker!" — if it does, Docker is working correctly.

### Obtain an OpenAI API key

Sign in at `https://platform.openai.com/`, generate an API key, and keep it ready — it will be set as the `OPENAI_API_KEY` environment variable in Phase 1. Do not commit this key to any file in the repository; it is read from the environment only, per `PROJECT_CONTEXT.md` Part 6.3.

### What each tool is and why it is needed

- **Docker Desktop** — runs every component of this project as an isolated container. Redis Stack, Prometheus, Grafana, and the gateway itself all run inside Docker containers rather than being installed directly on your machine.
- **docker-compose** — a single command (`docker compose up`) that starts all containers together with the correct networking between them.
- **Python** — the gateway is written in Python (FastAPI), and the local embedding model runs via the `sentence-transformers` library.
- **OpenAI API key** — required for the upstream LLM calls that happen on every cache miss (Path B / Path D in `PROJECT_CONTEXT.md` Part 3).

---

## 0. Operating rules for this build

### HARD STOP RULE — read this before anything else

**After completing every phase's implementation and running its verification checks, you MUST stop completely. Do not begin the next phase under any circumstances. Instead:**

1. Report the results of every verification check from that phase — pass or fail, with evidence (test output, curl responses, log lines, or whatever is appropriate for that phase's checks).
2. State clearly which phase you just completed and which phase comes next.
3. Wait for the user to explicitly say something like "looks good, continue to Phase X" or "proceed."
4. Only begin the next phase after receiving that explicit confirmation.

**This is a hard requirement, not a suggestion. Automatically proceeding to the next phase — even if all checks passed — is a violation of this rule.** The user must be in the loop at every phase boundary. There are 7 phases, so there are 7 stop points. Do not collapse them.

---

### General operating rules

1. **`PROJECT_CONTEXT.md` is authoritative.** If anything in this file appears to conflict with it, `PROJECT_CONTEXT.md` wins. Flag the conflict rather than silently picking one.
2. **Build in the phase order below, one phase at a time.** Do not work on the next phase while the current one is incomplete. Do not jump ahead. The Hard Stop Rule above governs every phase boundary.
3. **No invented scope.** Do not add endpoints, config values, tenants, or behaviors that are not in `PROJECT_CONTEXT.md`. If something seems missing, re-read the relevant Part of that file before assuming it's actually missing.
4. **No "GET then SET" Redis patterns.** Every piece of mutable circuit breaker state goes through either a native atomic Redis command or one of the two Lua scripts, exactly as specified in Part 4.3 and Part 5 of `PROJECT_CONTEXT.md`. This rule is non-negotiable and is the single most important correctness requirement on the resilience side of this project.
5. **Never run embedding generation synchronously inside an `async def` route handler.** It must always go through `loop.run_in_executor()` per Part 5.4. This is the single most important correctness requirement on the caching side of this project.
6. **Write tests as you go, not at the end.** Phase 2 below includes concurrency tests specifically because that's where bugs are easiest to introduce and hardest to notice without a test.
7. **Commit after each phase**, with a commit message naming the phase (e.g. `"Phase 2: breaker state machine + Lua scripts"`). This makes it easy to bisect if something breaks later.
8. **When in doubt about a number** (a timeout value, a threshold, a TTL, a port), use the defaults given in `PROJECT_CONTEXT.md` Part 6.1 and Part 6.2's example YAML. Do not invent different defaults.
9. **If any verification check fails**, fix it before reporting to the user. Report the original failure, what you changed to fix it, and the re-run result confirming the fix. Never report a verification check as passing if it has not actually been run and passed.

---

## Phase 1 — Scaffolding (no business logic yet)

**Goal:** every component exists, talks to its neighbors over the network, and does nothing meaningful yet.

1. Create the full folder structure exactly as specified in `PROJECT_CONTEXT.md` Part 9.
2. Write `docker-compose.yml` bringing up all four containerized component types (gateway, Redis Stack, Prometheus, Grafana) on one Docker network. Use the exact service names referenced in Part 11.1 (e.g. `redis-stack`, `gateway`).
3. Gateway: a bare FastAPI app with a single `GET /health` endpoint returning `200 {"status": "ok", "redis_connected": bool}`. No embedding, caching, or breaker logic yet — `redis_connected` can be a simple ping check.
4. Redis Stack: confirm the gateway container can connect (a startup log line is enough at this stage). Do not create any RediSearch indexes yet.
5. `config/tenants.yaml`: write the example file from `PROJECT_CONTEXT.md` Part 6.2 exactly (`tenant_a`, `tenant_b`), loaded into an in-memory registry at startup per Part 6.1 — but it only needs to load and log the parsed config at this stage, nothing reads from it yet.
6. Prometheus + Grafana: bring both up with a minimal `prometheus.yml` scrape config pointed at `gateway:8000/metrics`, even though `/metrics` doesn't exist yet (it's fine for the initial scrape to fail — this just proves the network topology is wired correctly).

**Phase 2 gate — run all checks below, then STOP. Report results to the user and wait for explicit confirmation before continuing:**
- `docker compose up` brings up all four containers without crash-looping.
- `curl http://localhost:8000/health` returns `200` with `redis_connected: true`.
- `config/tenants.yaml` is parsed successfully at gateway startup and both `tenant_a` and `tenant_b` configs are visible in the startup logs.
- Prometheus's targets page (`http://localhost:9090/targets`) shows the gateway target registered (its scrape state can be "down" at this stage — that's expected since `/metrics` doesn't exist yet).
- Grafana is reachable at `http://localhost:3000`.

---

## Phase 2 — Circuit breaker core (the hardest, most important phase on the resilience side)

**Goal:** the breaker state machine and concurrency-safe Redis operations work correctly in isolation, proven by tests, before any embedding, caching, or HTTP proxying is wired in.

1. Implement `redis_client.py`: connection setup, and key-builder helper functions for every breaker key pattern in `PROJECT_CONTEXT.md` Part 4.1 (e.g. `state_key(tenant)`, `failures_key(tenant)`, `cooldown_key(tenant)`, `half_open_claim_key(tenant)`) — never construct key strings inline elsewhere in the codebase.
2. Implement `lua_scripts.py`: both Lua scripts from Part 4.3, loaded once at startup via the Redis client's scripting registration, exposed as two Python functions: `record_failure(tenant, window_seconds, failure_threshold, cooldown_seconds) -> "tripped" | "still_closed"` and `resolve_half_open_test(tenant, outcome, cooldown_seconds) -> "closed" | "reopened"`.
3. Implement the half-open claim as a standalone function using `SET ... NX EX 5` directly (no Lua script needed — see Part 5.3) — name it something explicit like `try_claim_half_open_test(tenant) -> bool`.
4. Implement `state_machine.py`: a single function, e.g. `decide_breaker_path(tenant) -> "B" | "C" | "D"`, that performs exactly the breaker-half of the decision tree in `PROJECT_CONTEXT.md` Part 3.1 (this function is only invoked after a cache miss has already been determined — see Phase 3). This function should be the *only* place in the codebase that reads `breaker:{tenant}:state`.
5. Write `test_concurrency.py` now, before moving on. At minimum:
   - Spawn N concurrent calls to the failure-recording path and assert the final failure count equals exactly N (proves the `INCR`-based atomicity, Part 5.2).
   - Spawn N concurrent calls to `try_claim_half_open_test` for the same tenant and assert exactly one returns `True` (proves Part 5.3).
6. Write `test_state_machine.py` covering every transition named in `PROJECT_CONTEXT.md` Part 8's decision log: `closed -> open` on threshold breach, `open -> half_open` after cooldown, `half_open -> closed` on test success, `half_open -> open` on test failure, and the manual reset path (`POST /admin/{tenant}/breaker/reset`).

**Phase 3 gate — run all checks below, then STOP. Report results to the user and wait for explicit confirmation before continuing:**
- All tests in `test_concurrency.py` and `test_state_machine.py` pass.
- Manually run the failure-recording function 5 times in a row against a fresh tenant and confirm via `redis-cli` that the state key flips to `"open"` exactly when `failure_threshold` is reached, not before or after.
- Manually confirm a missing Redis key for a brand-new tenant is treated as `"closed"` (Part 3.7).

**Do not proceed to Phase 3 until every item above is confirmed. This phase is the foundation the resilience half of the system depends on.**

---

## Phase 3 — Embedding + semantic cache layer (the hardest, most important phase on the caching side)

**Goal:** the embedding model and RediSearch vector cache work correctly in isolation, proven by tests, before they're wired into the full request lifecycle.

1. Implement `embedding_service.py`: load `all-MiniLM-L6-v2` once at startup (not per-request), and expose `generate_embedding_async(text: str) -> list[float]`, implemented exactly per Part 5.4 using `loop.run_in_executor()` with a `ThreadPoolExecutor` sized from `THREAD_POOL_WORKERS` (Part 6.3).
2. Implement `vector_store.py`:
   - `create_tenant_index(tenant)`: runs the `FT.CREATE` command from Part 4.2 for a given tenant, called once at startup for every tenant in `tenants.yaml`.
   - `write_cache_entry(tenant, prompt, response, embedding, model_version, ttl)`: implements the write flow from Part 4.4 exactly (both `:vec` and `:meta` keys, same TTL on both).
   - `query_cache(tenant, embedding, threshold, model_version) -> str | None`: implements the read flow from Part 4.5 exactly, including the model-version check before returning a hit (Part 3.2 step 3).
3. Write `test_cache.py` now, before moving on. At minimum:
   - Write a cache entry for `tenant_a`, then query with a near-identical embedding and assert a hit is returned.
   - Query `tenant_b`'s index for the same embedding written under `tenant_a` and assert it returns no hit — proves structural tenant isolation (Part 8 decision log).
   - Write a cache entry with `model_version: "gpt-4"`, then query with `tenant_config.model_version = "gpt-3.5-turbo"` and assert the hit is rejected due to version mismatch (Part 3.2 step 3).
   - Assert that 500 concurrent calls to `generate_embedding_async` do not block a concurrently-running asyncio task (e.g. a simple sleeping coroutine) from completing on schedule — proves the event loop is never blocked (Part 5.4).

**Phase 4 gate — run all checks below, then STOP. Report results to the user and wait for explicit confirmation before continuing:**
- All tests in `test_cache.py` pass.
- Manually call `generate_embedding_async` and confirm the returned vector has exactly 384 dimensions.
- Manually write and then query a cache entry via `redis-cli` (`FT.SEARCH idx:tenant_a ...`) and confirm the raw RediSearch response matches what `query_cache` returns.
- Confirm `idx:tenant_a` and `idx:tenant_b` exist as two separate indexes (`FT._LIST` in `redis-cli`), not one shared index.

---

## Phase 4 — Wire the full request lifecycle

**Goal:** real HTTP requests to `POST /v1/chat` flow through Paths A, B, C, and D exactly as specified, combining the Phase 2 breaker core and Phase 3 cache layer into one orchestrated flow.

1. Implement `gateway_service.py`: the orchestrator function that performs, in order, exactly the steps in `PROJECT_CONTEXT.md` Part 3.1's decision tree — tenant validation, config load, embedding generation, RediSearch KNN query, then (on miss only) the breaker decision and either Path B, C, or D logic from Parts 3.3–3.5.
2. Implement `openai_client.py`: the outbound call to OpenAI's chat completion endpoint, with the per-tenant `timeout_seconds` from config, using an async HTTP client.
3. Wire `POST /v1/chat` in `chat_routes.py` to call `gateway_service`, returning the exact response envelope shape per path described in Part 7.1.
4. Implement `metrics.py`: define and instrument all 8 Prometheus metrics from Part 7.3 at the correct points in the request lifecycle (cache hit/miss counters, LLM call outcome counter, circuit trip/rejection counters, breaker state gauge, request latency histogram, embedding duration histogram). Expose them at `GET /metrics`.
5. Implement `GET /admin/{tenant}/breaker` and `POST /admin/{tenant}/breaker/reset` per Part 7.2.

**Phase 5 gate — run all checks below, then STOP. Report results to the user and wait for explicit confirmation before continuing:**
- With OpenAI reachable and a fresh tenant, the first call to `POST /v1/chat` returns `cache_hit: false` and a real OpenAI response; a second call with a reworded but semantically equivalent prompt for the same tenant returns `cache_hit: true` in under 10ms (verify via the response's measured latency, not just the field value).
- The same reworded prompt sent under a different tenant returns `cache_hit: false` — confirms tenant isolation holds end-to-end, not just at the Phase 3 unit-test level.
- Manually force 5 consecutive OpenAI failures for one tenant (e.g. temporarily point `OPENAI_API_KEY` at an invalid value) and confirm: failures accumulate, the breaker trips to `open` at the threshold, and subsequent cache-miss calls for that tenant return in single-digit milliseconds with the `circuit_open` body from Part 3.4.
- `GET /metrics` returns valid Prometheus exposition format and includes nonzero values for `cache_hits_total`, `cache_misses_total`, and `request_latency_ms` after the above test traffic.
- `GET /admin/{tenant}/breaker` correctly reflects the tripped state, and `POST /admin/{tenant}/breaker/reset` returns it to `closed`.

---

## Phase 5 — Half-open recovery + concurrency under real traffic

**Goal:** prove the half-open recovery path and the multi-replica shared-state claim from `PROJECT_CONTEXT.md` Part 1.4, items 5 and 6, hold under real HTTP traffic — not just in the Phase 2 unit tests.

1. Confirm (or fix, if Phase 4's wiring missed it) that after `cooldown_seconds` elapses following a trip, the next cache-miss request for that tenant takes Path D (half-open test) exactly once, even if multiple requests arrive concurrently — every other concurrent request during that window must take Path C.
2. Run 2 instances of the gateway container locally (e.g. `docker compose up --scale gateway=2` with a simple round-robin entrypoint, or two separate `uvicorn` processes on different ports both pointed at the same Redis Stack instance).
3. Trip the breaker for a tenant via Replica 1, then immediately send requests to Replica 2 and confirm Replica 2 also sees the breaker as `open` — proving shared Redis state, not independent in-process state.

**Phase 6 gate — run all checks below, then STOP. Report results to the user and wait for explicit confirmation before continuing:**
- Sending 20+ concurrent requests for a tenant right at the moment its breaker enters `half_open` results in exactly one request taking Path D and all others taking Path C — verify by checking `circuit_rejections_total{tenant}` incremented by 19 (or N-1) and only one real OpenAI call was attempted.
- The 2-replica test in step 3 above is reproducible: tripping on Replica 1 is visible on Replica 2 within one Redis read, with no replica-specific lag beyond normal network latency.
- A successful half-open test resets the breaker to `closed` and clears the failure counter, confirmed via `GET /admin/{tenant}/breaker` showing `failure_count: 0`.

---

## Phase 6 — Observability dashboard

**Goal:** Grafana fully reflects live system behavior per `PROJECT_CONTEXT.md` Part 2.2.5 and the metrics defined in Part 7.3.

1. Build a Grafana dashboard (`observability/grafana/dashboards/inference_gateway.json`) with at minimum: a cache hit rate panel (derived from `cache_hits_total` / (`cache_hits_total` + `cache_misses_total`)), a request latency panel (from the `request_latency_ms` histogram, showing p50/p95/p99), a circuit breaker state panel per tenant (from the `circuit_breaker_state` gauge), and a per-tenant request volume panel.
2. Provision the Prometheus datasource automatically via `observability/grafana/provisioning/datasources.yml` so it doesn't need to be configured manually after `docker compose up`.
3. Confirm Prometheus's scrape interval and target are correctly picking up all 8 metrics from Part 7.3.

**Phase 7 gate — run all checks below, then STOP. Report results to the user and wait for explicit confirmation before continuing:**
- Opening Grafana fresh after `docker compose up` shows the dashboard pre-loaded, with the Prometheus datasource already connected (no manual setup required).
- Generating test traffic (a mix of cache hits, cache misses, and at least one forced breaker trip) produces visible, correct changes on every panel within one scrape interval.
- The circuit breaker state panel correctly shows `closed` (0), `half_open` (1), and `open` (2) at the appropriate points during a manually triggered trip-and-recovery cycle.

---

## Phase 7 — Documentation and final polish

1. Write `README.md`: setup instructions (`docker compose up`, required environment variables including `OPENAI_API_KEY`), and the demo script from `PROJECT_CONTEXT.md` Part 10, copied or lightly adapted.
2. Include example before/after numbers in the README as evidence: cache-hit latency vs. cache-miss latency, and circuit-open rejection latency vs. a real OpenAI timeout — pulled from actual runs during this build, not estimated.
3. Do a final read-through of `PROJECT_CONTEXT.md` Part 1.4 (definition of done) and check off every item against the actual running system — not from memory, but by actually performing each check live.

**This project is complete only when every item in `PROJECT_CONTEXT.md` Part 1.4 has been freshly, manually verified against the running system — not assumed from earlier phase verifications.**

---

## Quick reference — what NOT to do (collected from `PROJECT_CONTEXT.md`, repeated here because these are the most common ways this kind of project goes wrong)

- Do not add authentication anywhere beyond the trusted `X-Tenant-ID` header assumption.
- Do not implement retry-on-the-caller's-behalf logic inside the gateway.
- Do not call `model.encode()` (or any embedding generation) synchronously inside an `async def` route handler — always go through `loop.run_in_executor()`.
- Do not implement any circuit breaker state change as a separate read-then-write from application code.
- Do not allow more than one request to be treated as "the" half-open test at a time.
- Do not let two different tenants share a RediSearch index or a Redis key namespace.
- Do not serve a cache hit without verifying the cached entry's `model_version` matches the tenant's current configured model version.
- Do not add a seventh logical component to the system (no Postgres audit store, no React dashboard, no separate vector DB — see Part 1.3 and Part 2.1).
- Do not invent config values that aren't derived per the process in `PROJECT_CONTEXT.md` Part 6.1.

---

## Final completion gate

After Phase 7, do not declare the project complete yourself. Instead:

1. Go through every item in `PROJECT_CONTEXT.md` Part 1.4 (definition of done) one by one.
2. For each item, state: what you did to verify it, and the actual evidence (output, screenshot description, test result).
3. Report this full checklist to the user.
4. Wait for the user to confirm the project is complete.

**The project is complete only when the user says it is — not when you believe all items are done.**
