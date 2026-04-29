from __future__ import annotations

from soak_tester.util import percentile


def summarize_series(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {
            "sample_count": 0,
            "avg": 0.0,
            "min": 0.0,
            "max": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "avg_gbps": 0.0,
            "min_gbps": 0.0,
            "max_gbps": 0.0,
            "p50_gbps": 0.0,
            "p95_gbps": 0.0,
            "p99_gbps": 0.0,
        }
    avg = sum(values) / len(values)
    p50 = percentile(values, 50)
    p95 = percentile(values, 95)
    p99 = percentile(values, 99)
    return {
        "sample_count": len(values),
        "avg": avg,
        "min": min(values),
        "max": max(values),
        "p50": p50,
        "p95": p95,
        "p99": p99,
        "avg_gbps": avg / 1_000_000_000,
        "min_gbps": min(values) / 1_000_000_000,
        "max_gbps": max(values) / 1_000_000_000,
        "p50_gbps": p50 / 1_000_000_000,
        "p95_gbps": p95 / 1_000_000_000,
        "p99_gbps": p99 / 1_000_000_000,
    }
