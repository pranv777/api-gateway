"""End-to-end HTTP benchmarks against a running gateway.

Latency (run with limits raised so nothing is rejected, see README):

    python -m benchmarks.http_bench latency --base-url http://gateway:8000 \
        --requests 5000 --concurrency 50 --upstream-url http://products:8001/products

Rate-limit correctness under concurrent load (run with the limits from your .env):

    python -m benchmarks.http_bench correctness --base-url http://gateway:8000 \
        --requests 1000 --concurrency 100 --capacity 100 --refill-rate 10
"""

import argparse
import asyncio
import json
import math
import platform
import statistics
import time
import uuid
from pathlib import Path

import httpx

PASSWORD = "benchmark-password-1"


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


async def provision(client: httpx.AsyncClient) -> str:
    """Create a throwaway user and API key; return the plaintext key."""
    email = f"bench-{uuid.uuid4().hex[:12]}@example.com"
    body = {"email": email, "password": PASSWORD}
    (await client.post("/auth/register", json=body)).raise_for_status()
    login = await client.post("/auth/login", json=body)
    login.raise_for_status()
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    created = await client.post("/api-keys", json={"name": "benchmark"}, headers=headers)
    created.raise_for_status()
    return created.json()["key"]


async def fire(
    client: httpx.AsyncClient, url: str, headers: dict, total: int, concurrency: int
) -> tuple[list[tuple[int, float]], float]:
    """Send `total` GETs with at most `concurrency` in flight. Returns [(status, ms)], wall time."""
    semaphore = asyncio.Semaphore(concurrency)
    samples: list[tuple[int, float]] = []

    async def one() -> None:
        async with semaphore:
            started = time.perf_counter()
            try:
                status = (await client.get(url, headers=headers)).status_code
            except httpx.HTTPError:
                status = 0  # connection-level failure
            samples.append((status, (time.perf_counter() - started) * 1000))

    started = time.perf_counter()
    await asyncio.gather(*[one() for _ in range(total)])
    return samples, time.perf_counter() - started


def latency_stats(samples: list[tuple[int, float]], wall: float) -> dict:
    ok = [ms for status, ms in samples if status == 200]
    stats = {
        "total_requests": len(samples),
        "status_200": len(ok),
        "status_429": sum(1 for s, _ in samples if s == 429),
        "other_or_failed": sum(1 for s, _ in samples if s not in (200, 429)),
        "requests_per_second": round(len(samples) / wall, 1),
    }
    if ok:
        stats |= {
            "p50_ms": round(percentile(ok, 50), 2),
            "p95_ms": round(percentile(ok, 95), 2),
            "p99_ms": round(percentile(ok, 99), 2),
            "mean_ms": round(statistics.fmean(ok), 2),
        }
    return stats


async def run_latency(args, client: httpx.AsyncClient, key: str) -> dict:
    results = {}
    gateway_url = f"{args.base_url}/api/products"
    await fire(client, gateway_url, {"X-API-Key": key}, 50, 10)  # warm-up (connections, caches)
    samples, wall = await fire(
        client, gateway_url, {"X-API-Key": key}, args.requests, args.concurrency
    )
    results["through_gateway"] = latency_stats(samples, wall)
    if args.upstream_url:
        await fire(client, args.upstream_url, {}, 50, 10)
        samples, wall = await fire(client, args.upstream_url, {}, args.requests, args.concurrency)
        results["direct_to_upstream"] = latency_stats(samples, wall)
    return results


async def run_correctness(args, client: httpx.AsyncClient, key: str) -> dict:
    if args.capacity <= 0:
        raise ValueError("--capacity must be positive")
    if args.requests <= args.capacity:
        raise ValueError("--requests must exceed --capacity")
    if args.concurrency <= 0:
        raise ValueError("--concurrency must be positive")
    if args.refill_rate <= 0 or args.window_seconds <= 0:
        raise ValueError("Refill rate and window duration must be positive")

    samples, wall = await fire(
        client,
        f"{args.base_url}/api/products",
        {"X-API-Key": key},
        args.requests,
        args.concurrency,
    )

    accepted = sum(status == 200 for status, _ in samples)
    rejected = sum(status == 429 for status, _ in samples)
    errors = len(samples) - accepted - rejected

    problems = []

    if args.algorithm == "token_bucket":
        allowed_max = math.floor(args.capacity + args.refill_rate * wall)
    else:
        allowed_max = args.capacity

        # This check evaluates one burst inside a single window.
        if wall >= args.window_seconds:
            problems.append(
                "Run crossed a sliding-window boundary; use a shorter burst "
                "or a longer configured window."
            )

    # provision() creates a fresh user and key, so the initial budget is full.
    if accepted < args.capacity:
        problems.append(
            "Initial capacity was not accepted; check other limits, "
            "server configuration, and upstream availability."
        )

    if accepted > allowed_max:
        problems.append("Accepted requests exceeded the configured bound.")

    if rejected == 0:
        problems.append("No rate-limit rejections; increase burst load.")

    if errors:
        problems.append(f"{errors} unexpected responses or connection failures.")

    if len(samples) != args.requests:
        problems.append("The requested number of samples was not collected.")

    return {
        "algorithm": args.algorithm,
        "configured_limit": args.capacity,
        "burst_requests": args.requests,
        "concurrency": args.concurrency,
        "elapsed_seconds": round(wall, 3),
        "accepted_200": accepted,
        "rejected_429": rejected,
        "other_or_failed": errors,
        "max_permitted_by_limit": allowed_max,
        "limit_respected": accepted <= allowed_max,
        "passed": not problems,
        "problems": problems,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=["latency", "correctness"])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--upstream-url", help="latency only: also measure this URL directly")
    parser.add_argument(
        "--algorithm", choices=["token_bucket", "sliding_window"], default="token_bucket"
    )
    parser.add_argument("--capacity", type=int, default=100, help="correctness: per-key limit")
    parser.add_argument("--refill-rate", type=float, default=10.0, help="token bucket refill/s")
    parser.add_argument("--window-seconds", type=int, default=60, help="sliding window length")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    limits = httpx.Limits(max_connections=args.concurrency + 10)
    async with httpx.AsyncClient(timeout=30, limits=limits) as client:
        client.base_url = httpx.URL(args.base_url)
        key = await provision(client)
        runner = run_latency if args.scenario == "latency" else run_correctness
        results = await runner(args, client, key)

    report = {
        "scenario": args.scenario,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": __import__("os").cpu_count(),
        },
        "results": results,
    }
    output = args.output or Path(f"benchmarks/results/http_{args.scenario}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nJSON written to {output}")
    if args.scenario == "correctness" and not results["passed"]:
        raise SystemExit("FAIL: " + "; ".join(results["problems"]))


if __name__ == "__main__":
    asyncio.run(main())
