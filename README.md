# API Gateway with Rate Limiting and Authentication

A backend-only API gateway built with **FastAPI, Redis, PostgreSQL and Docker**. It is the single
entry point to two small demo services (products and orders) and handles authentication,
rate limiting, request routing, request logging and upstream timeouts.

| Capability | How it works |
|---|---|
| **JWT authentication** | Register/login with Argon2-hashed passwords; short-lived HS256 access tokens |
| **API keys** | Generated with `secrets`, only a SHA-256 hash is stored, plaintext shown once, revocable |
| **Rate limiting** | Token bucket *or* sliding window (configurable), atomic Redis Lua scripts, per API key + per user + per IP |
| **Routing** | Async forwarding with a shared `httpx.AsyncClient`; fixed upstream hosts (no SSRF) |
| **Analytics** | Request metadata in PostgreSQL; per-user usage summary and request list |
| **Timeouts** | Configurable per service; `504` on timeout, `502` on connection/protocol failure |

## Architecture

```
                         ┌────────────────────────── API Gateway (FastAPI) ───────────────────────────┐
 client ── X-API-Key ──▶ │ 1. per-IP rate limit   ─┐                                                   │
                         │ 2. API-key auth (PG)    │   Redis (Lua)      ┌──────────────┐               │
                         │ 3. per-user + per-key   ├──▶ token bucket /  │ Product Svc  │◀── /api/products/*
                         │    rate limits          │    sliding window  └──────────────┘               │
                         │ 4. forward (httpx)  ────┘                    ┌──────────────┐               │
                         │ 5. log metadata (background task) ──▶ PG     │ Order Svc    │◀── /api/orders/*
                         └─────────────────────────────────────────────▶└──────────────┘               │
 client ── JWT ────────▶ │ /auth/*  /api-keys  /analytics/*   (management API, PostgreSQL)             │
                         └─────────────────────────────────────────────────────────────────────────────┘
```

Request order on `/api/*` matters: the IP limit runs *before* authentication so unauthenticated
floods never reach the database; authentication runs before the user/key limits so those can be
keyed by identity.

## Quick start

**Which `.env`?** The root `.env` (from `.env.example`) configures `docker compose`.
Only if you run the gateway outside Docker, also copy `gateway/.env.example` to `gateway/.env`
and use literal values there (no `${...}` expansion).

Requires Docker with Compose v2.

```bash
cp .env.example .env
# set a real secret (>= 32 chars) and DB password in .env:
sed -i "s|^JWT_SECRET=.*|JWT_SECRET=$(openssl rand -hex 32)|" .env

docker compose up -d --build          # gateway, products, orders, postgres, redis
docker compose run --rm migrate       # explicit setup step: Alembic migrations
```

Swagger UI: <http://localhost:8000/docs>  ·  Health: `/health` (liveness), `/ready` (DB + Redis)

### Try it

```bash
BASE=http://localhost:8000

# 1. Register and log in
curl -s $BASE/auth/register -H 'content-type: application/json' \
  -d '{"email":"alice@example.com","password":"correct-horse-battery"}'
TOKEN=$(curl -s $BASE/auth/login -H 'content-type: application/json' \
  -d '{"email":"alice@example.com","password":"correct-horse-battery"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

# 2. Create an API key (the plaintext "key" is shown ONLY in this response)
curl -s $BASE/api-keys -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"demo"}'
KEY=gw_...paste-it-here...

# 3. Call the upstream services through the gateway
curl -s $BASE/api/products -H "X-API-Key: $KEY"
curl -s $BASE/api/products/1 -H "X-API-Key: $KEY"
curl -s $BASE/api/orders -X POST -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"product_id":1,"quantity":2}'

# 4. See the rate-limit headers (-i), and force a 504 with the demo `delay` parameter
curl -si $BASE/api/products -H "X-API-Key: $KEY" | grep -i '^x-ratelimit'
curl -s  "$BASE/api/products?delay=10" -H "X-API-Key: $KEY"      # -> 504 after UPSTREAM_TIMEOUT_SECONDS

# 5. Usage analytics (JWT)
curl -s "$BASE/analytics/usage"    -H "Authorization: Bearer $TOKEN"
curl -s "$BASE/analytics/requests?limit=10" -H "Authorization: Bearer $TOKEN"
curl -s "$BASE/analytics/usage?start=2026-01-01T00:00:00Z&api_key_id=<key-uuid>" -H "Authorization: Bearer $TOKEN"

# 6. Revoke the key
curl -s -X DELETE $BASE/api-keys/<key-uuid> -H "Authorization: Bearer $TOKEN"
```

## API overview

| Endpoint | Auth | Notes |
|---|---|---|
| `POST /auth/register`, `POST /auth/login` | – | Per-IP brute-force throttle (`AUTH_RATE_LIMIT_*`) |
| `GET /auth/me` | JWT | |
| `POST /api-keys`, `GET /api-keys`, `DELETE /api-keys/{id}` | JWT | Delete = revoke (soft); other users' keys → 404 |
| `GET /analytics/usage`, `GET /analytics/requests` | JWT | Filters: `start`, `end`, `api_key_id`; own data only |
| `ANY /api/products/*`, `/api/orders/*` | `X-API-Key` | GET, POST, PUT, PATCH, DELETE |

Status codes you will see from the gateway itself: `401` (missing/invalid/revoked credentials),
`404` (unknown route), `400` (unsafe path), `413` (body > `MAX_REQUEST_BODY_BYTES`),
`429` (+ `Retry-After`, `X-RateLimit-*`), `502`, `504`, `503` (Redis unavailable).

## Configuration

All settings are environment variables (see `.env.example`).

| Variable | Default | Meaning |
|---|---|---|
| `RATE_LIMIT_ALGORITHM` | `token_bucket` | `token_bucket` or `sliding_window` |
| `TOKEN_BUCKET_CAPACITY` / `TOKEN_BUCKET_REFILL_RATE` | 100 / 10 | Per API key: burst size / tokens per second |
| `SLIDING_WINDOW_LIMIT` / `SLIDING_WINDOW_SECONDS` | 100 / 60 | Per API key |
| `USER_LIMIT_MULTIPLIER` | 5 | Per-user limit = per-key limit × this |
| `IP_RATE_LIMIT_REQUESTS` / `_WINDOW_SECONDS` | 200 / 60 | Per client IP |
| `AUTH_RATE_LIMIT_ATTEMPTS` / `_WINDOW_SECONDS` | 10 / 60 | Per client IP on register/login |
| `TRUSTED_PROXY_COUNT` | 0 | See "Client IP" below |
| `UPSTREAM_TIMEOUT_SECONDS` / `UPSTREAM_CONNECT_TIMEOUT_SECONDS` | 5 / 2 | Optional per-service overrides: `PRODUCT_SERVICE_TIMEOUT_SECONDS`, `ORDER_SERVICE_TIMEOUT_SECONDS` |
| `JWT_SECRET`, `JWT_EXPIRE_MINUTES` | – / 30 | Secret must be ≥ 32 characters |

## Design decisions

- **Atomic limiting.** Each check is one Lua script, so concurrent requests (even across gateway
  instances) cannot oversubscribe a limit. The scripts use Redis `TIME`, not the gateway's clock.
  The algorithm name is part of the Redis key so switching algorithms cannot cause `WRONGTYPE` errors.
- **All limits are enforced, not substituted.** A proxied request must pass the IP, user *and* key
  limits. Checks stop at the first denial; limits checked earlier have already been charged for
  that request (a deliberate simplicity trade-off). Redis keys always carry a TTL.
- **Fail closed.** If Redis is down, the gateway answers `503` rather than letting traffic through unlimited.
- **Client IP.** By default the direct peer address is used and `X-Forwarded-For` is ignored,
  because any client can send it. If you run N trusted reverse proxies in front, set
  `TRUSTED_PROXY_COUNT=N`; the Nth entry from the right is then used.
- **No SSRF.** Upstream hosts come only from configuration. Clients choose a path under a fixed
  prefix (`..`, backslashes and control characters are rejected); redirects are not followed.
- **Header hygiene.** Only an allow-list of client headers is forwarded (never `Authorization`,
  `X-API-Key`, `Cookie`, `X-Forwarded-*`); hop-by-hop headers are stripped from responses. The
  gateway adds `X-Request-ID`, `X-Gateway-User-Id` and its own `X-Forwarded-For`.
- **API keys use SHA-256, passwords use Argon2.** Keys are 256 random bits, so a fast hash is safe
  and keeps per-request auth cheap; passwords are low-entropy and need a slow, salted hash.
- **Logging never blocks or fails a request.** Log rows are written in a background task after the
  response; only metadata is stored (path without query string, no bodies/headers/credentials).
- **Known limits (by design, for scope).** Request logs exist only for authenticated `/api/*`
  requests, so IP-level `429`s (before authentication) and failed logins are not in the analytics
  tables (they appear in application logs). Bodies are buffered in memory (capped at
  `MAX_REQUEST_BODY_BYTES`), so streaming and very large uploads are not supported. The demo
  `?delay=` parameter on the sample services exists only to demonstrate timeouts.
- **Analytics definitions.** *Successful* = status < 400; *rate-limited* = rejected by the gateway
  with 429; *failed* = status ≥ 400 excluding those. Average response time covers all logged requests.

## Project layout

```
gateway/app/api/        auth, api_keys, analytics, proxy routers + deps (auth / rate-limit dependencies)
gateway/app/core/       config, security (Argon2/JWT/API keys), rate_limit (Lua + limiter), middleware, logs
gateway/app/db/         SQLAlchemy 2.0 models, async session
gateway/app/services/   routing (safe URL/headers/forwarding), logging (request log persistence)
gateway/migrations/     Alembic (async env, 0001_initial)
gateway/tests/          pytest suite        gateway/benchmarks/   benchmark scripts
services/products|orders  demo upstream services
```

## Development, tests and linting

The integration tests use **real PostgreSQL and Redis** (the test database is dropped/recreated
for every test and Redis DB 15 is flushed, so use disposable instances).

**In Docker (easiest):**

```bash
docker compose run --rm test                                   # whole suite
docker compose run --rm test pytest --cov=app --cov-report=term-missing
docker compose run --rm test ruff check . && docker compose run --rm test ruff format --check .
```

If your PostgreSQL volume already existed before you first ran this, the `gateway_test`
database was not auto-created: `docker compose exec postgres createdb -U gateway gateway_test`.

**Locally (macOS / Linux):**

```bash
cd gateway
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export TEST_DATABASE_URL="postgresql+asyncpg://gateway:YOUR_PASSWORD@127.0.0.1:5433/gateway_test"
export TEST_REDIS_URL="redis://127.0.0.1:6379/15"
export REQUIRE_INFRA=1

python -m pytest
python -m pytest --cov=app --cov-report=term-missing
ruff check . && ruff format --check .
```

**Locally (Windows PowerShell):**

```powershell
cd gateway
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

$env:TEST_DATABASE_URL = "postgresql+asyncpg://gateway:YOUR_PASSWORD@127.0.0.1:5433/gateway_test"
$env:TEST_REDIS_URL = "redis://127.0.0.1:6379/15"
$env:REQUIRE_INFRA = "1"

python -m pytest
python -m pytest --cov=app --cov-report=term-missing
ruff check .; ruff format --check .
```

Without reachable PostgreSQL/Redis the integration tests are *skipped* (set `REQUIRE_INFRA=1` to
make that a failure). The unit tests in `tests/test_security.py` need neither.

## Benchmarks

Two small scripts produce real, reproducible numbers. They write JSON to `gateway/benchmarks/results/`.

```bash
# 1) Rate-limiter overhead (one Lua check vs. a bare Redis PING)
docker compose run --rm gateway python -m benchmarks.limiter_bench \
    --redis-url redis://redis:6379/0 --requests 20000 --concurrency 20

# 2) Gateway latency (p50/p95) vs. calling the product service directly.
#    Raise the limits first so nothing is rejected: in .env set
#      TOKEN_BUCKET_CAPACITY=1000000  TOKEN_BUCKET_REFILL_RATE=1000000  IP_RATE_LIMIT_REQUESTS=1000000
#    then: docker compose up -d
docker compose run --rm gateway python -m benchmarks.http_bench latency \
    --base-url http://gateway:8000 --requests 5000 --concurrency 50 \
    --upstream-url http://products:8001/products

# 3) Rate-limit correctness under concurrent load (restore the real limits in .env first).
#    Fires a burst far above the limit and checks accepted <= capacity + refill_rate * elapsed.
docker compose run --rm gateway python -m benchmarks.http_bench correctness \
    --base-url http://gateway:8000 --requests 1000 --concurrency 100 \
    --capacity 100 --refill-rate 10          # sliding window: --algorithm sliding_window --window-seconds 60
```

### Recorded results

Benchmarks ran in Docker on WSL2 using Python 3.12.16, with 12 logical CPUs
reported by the benchmark environment. The client, gateway, Redis, PostgreSQL,
and upstream services shared the same machine.

These are local measurements, not production capacity estimates.

#### HTTP latency and throughput

Three runs were performed with 5,000 requests per target and 10 concurrent
clients. Values below show the range across those runs.

| Metric | Through gateway | Direct to product service |
|---|---:|---:|
| Throughput | 166.0–185.1 requests/sec | 520.4–567.3 requests/sec |
| p50 latency | 50.47–56.46 ms | 12.19–13.99 ms |
| p95 latency | 78.60–87.70 ms | 33.72–37.40 ms |
| p99 latency | 97.27–109.99 ms | 50.83–65.67 ms |
| Mean latency | 53.92–60.14 ms | 16.82–18.34 ms |
| Successful requests | 15,000 / 15,000 | 15,000 / 15,000 |
| Rate-limit rejections | 0 | 0 |
| Other errors | 0 | 0 |

The difference between the average mean latencies was approximately 39.81 ms.
Throughput was measured at concurrency 10 and does not represent maximum
system capacity. Percentiles were calculated separately for each run.

#### Sliding-window correctness

The API-key limit was configured to 100 requests per 60 seconds. IP and user
limits were raised to prevent them from interfering with the API-key test.

| Metric | Result |
|---|---:|
| Total requests | 1,000 |
| Concurrent clients | 100 |
| Elapsed time | 4.362 seconds |
| Accepted requests (`200`) | 100 |
| Rejected requests (`429`) | 900 |
| Unexpected errors | 0 |
| Maximum permitted requests | 100 |
| Result | Passed |

The burst completed within one window and accepted exactly the configured
capacity. This validates the tested burst scenario; it does not establish
correctness for every possible traffic pattern.

#### Rate-limiter microbenchmark

Each scenario used 20,000 operations and concurrency 20.

| Scenario | Operations/sec | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| Redis PING baseline | 12,356.1 | 1.531 ms | 1.861 ms | 2.666 ms |
| Token bucket | 9,275.2 | 2.047 ms | 2.726 ms | 4.247 ms |
| Sliding window | 8,557.9 | 2.173 ms | 3.229 ms | 4.433 ms |

The difference in median latency relative to Redis PING was 0.516 ms for
token bucket and 0.642 ms for sliding window. These differences include
client and Redis round-trip costs and are not isolated Lua execution times.

Benchmark JSON output is written to `gateway/benchmarks/results/`.

## Out of scope

No frontend, Nginx, Prometheus/Grafana, Kubernetes, service discovery, load balancing, tracing,
circuit breakers, retries, OAuth, refresh tokens, billing or RBAC.
