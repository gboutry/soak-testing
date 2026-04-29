from __future__ import annotations

import time
from typing import Any


def run_api_sample(conn: Any, *, seconds: int, target_ops_per_second: float) -> dict[str, float]:
    deadline = time.monotonic() + seconds
    count = 0
    errors = 0
    latencies: list[float] = []
    while time.monotonic() < deadline:
        start = time.monotonic()
        try:
            list(conn.compute.servers())
            list(conn.network.ports())
            list(conn.block_storage.volumes())
            count += 3
        except Exception:
            errors += 1
        latencies.append(time.monotonic() - start)
        if target_ops_per_second > 0:
            time.sleep(min(1 / target_ops_per_second, 1))
    elapsed = max(seconds, 1)
    return {
        "requested_ops_per_second": target_ops_per_second,
        "achieved_ops_per_second": count / elapsed,
        "errors": errors,
        "latency_p50_seconds": sorted(latencies)[len(latencies) // 2] if latencies else 0,
    }
