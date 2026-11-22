"""Rate-limiter overhead: latency of one limiter check (a single Redis Lua call), compared with
a bare Redis PING on the same connection as the network round-trip baseline.

    python -m benchmarks.limiter_bench --redis-url redis://redis:6379/0 \
        --requests 20000 --concurrency 20

Limits are set so high that no request is ever rejected; this measures cost, not behaviour.
"""

import argparse
import asyncio
import json
import platform
import statistics
import time
import uuid
from pathlib import Path

from pydantic import SecretStr
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.rate_limit import RateLimiter


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


def summarise(latencies_ms: list[float], elapsed: float) -> dict:
    return {
        "operations": len(latencies_ms),
        "ops_per_second": round(len(latencies_ms) / elapsed, 1),
        "p50_ms": round(percentile(latencies_ms, 50), 3),
        "p95_ms": round(percentile(latencies_ms, 95), 3),
        "p99_ms": round(percentile(latencies_ms, 99), 3),
        "mean_ms": round(statistics.fmean(latencies_ms), 3),
    }


async def measure(operation, total: int, concurrency: int) -> dict:
    if total <= 0:
        raise ValueError("total must be positive")
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")

    worker_count = min(total, concurrency)
    per_worker, remainder = divmod(total, worker_count)
    latencies: list[float] = []

    async def worker(count: int) -> None:
        for _ in range(count):
            started = time.perf_counter()
            await operation()
            latencies.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()

    await asyncio.gather(
        *(worker(per_worker + (1 if index < remainder else 0)) for index in range(worker_count))
    )

    elapsed = max(time.perf_counter() - started, 1e-12)
    return summarise(latencies, elapsed)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark rate-limiter overhead against Redis PING."
    )
    parser.add_argument("--redis-url", default="redis://localhost:6379/0")
    parser.add_argument("--requests", type=int, default=20_000)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results/limiter.json"))
    args = parser.parse_args()

    if args.requests <= 0 or args.concurrency <= 0:
        parser.error("--requests and --concurrency must be positive")

    redis = Redis.from_url(args.redis_url)
    results: dict[str, dict] = {}

    results["redis_ping_baseline"] = await measure(redis.ping, args.requests, args.concurrency)
    for algorithm in ("token_bucket", "sliding_window"):
        settings = Settings(
            database_url="postgresql+asyncpg://unused",
            redis_url=args.redis_url,
            jwt_secret=SecretStr("benchmark-secret-" + "x" * 32),
            rate_limit_algorithm=algorithm,
            token_bucket_capacity=10**9,
            token_bucket_refill_rate=10**6,
            sliding_window_limit=10**9,
            sliding_window_seconds=60,
        )
        limiter = RateLimiter(redis, settings)
        identifier = f"bench-{uuid.uuid4().hex}"
        results[algorithm] = await measure(
            lambda limiter=limiter, identifier=identifier: limiter.check("api_key", identifier),
            args.requests,
            args.concurrency,
        )
        async for key in redis.scan_iter(match=f"rl:*:api_key:{identifier}"):
            await redis.delete(key)
    await redis.aclose()

    report = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": __import__("os").cpu_count(),
        },
        "concurrency": args.concurrency,
        "requests_per_scenario": args.requests,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))

    print(f"\nconcurrency={args.concurrency}  requests/scenario={args.requests}\n")
    print("| scenario | ops/s | p50 ms | p95 ms | p99 ms |")
    print("|---|---|---|---|---|")
    for name, r in results.items():
        print(f"| {name} | {r['ops_per_second']} | {r['p50_ms']} | {r['p95_ms']} | {r['p99_ms']} |")
    print(f"\nJSON written to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
