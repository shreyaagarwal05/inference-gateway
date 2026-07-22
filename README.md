# Multi-Tenant AI Inference Gateway

FastAPI gateway with tenant-isolated semantic caching, a Redis-backed circuit breaker, Prometheus metrics, and Grafana dashboards.

## Prerequisites

- Docker Desktop with Docker Compose v2
- An OpenAI-compatible API key
- PowerShell or Bash with `curl`

## Setup

1. Copy the example environment file:

   ```bash
   cp .env.example .env
   ```

   On PowerShell:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Edit `.env` and set `OPENAI_API_KEY`. Keep `.env` local; it is ignored by Git and must never be committed.

   The default upstream is OpenAI:

   ```dotenv
   OPENAI_BASE_URL=https://api.openai.com/v1
   OPENAI_MODEL=
   ```

   For another OpenAI-compatible provider, set both values to that provider's base URL and a model available to the account.

3. Start the complete stack:

   ```bash
   docker compose up -d
   ```

4. Confirm the gateway and Redis are healthy:

   ```bash
   curl http://localhost:8000/health
   ```

   Expected response:

   ```json
   {"status":"ok","redis_connected":true}
   ```

## Service URLs

- Gateway: <http://localhost:8000>
- Prometheus: <http://localhost:9090>
- Grafana: <http://localhost:3000> (default login: `admin` / `admin`)
- Metrics: <http://localhost:8000/metrics>

## Basic request

Every request must include a configured tenant in `X-Tenant-ID`:

```bash
curl -X POST http://localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-ID: tenant_a' \
  -d '{"prompt":"What is machine learning?"}'
```

The first request is a cache miss. A semantically equivalent request for the same tenant can be served from Redis as a cache hit. Tenants use isolated cache namespaces and breaker state.

## Breaker administration

Inspect a tenant's breaker:

```bash
curl http://localhost:8000/admin/tenant_a/breaker
```

Reset it:

```bash
curl -X POST http://localhost:8000/admin/tenant_a/breaker/reset
```

## Demo flow

1. Start the stack and verify `/health`.
2. Send a cache-miss request for `tenant_a`.
3. Send a reworded equivalent prompt for `tenant_a` and observe `cache_hit: true`.
4. Open Grafana and view the cache-hit, latency, breaker-state, and request-volume panels.
5. Send the same prompt as `tenant_b` and observe tenant isolation.
6. For a breaker demo, temporarily use an invalid upstream key and send five unique cache-miss requests for `tenant_a`.
7. Confirm the breaker becomes `open`, then observe subsequent requests return `503` with `circuit_open`.
8. Inspect and reset the breaker through the admin endpoints, then restore the valid key.

Stop the stack with:

```bash
docker compose down
```

