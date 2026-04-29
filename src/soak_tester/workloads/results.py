from __future__ import annotations

import json
from typing import Any


def parse_iperf3_metrics(
    text: str,
    *,
    worker_id: str,
    receiver: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        payload = json.loads(text)
        end = payload.get("end", {})
        sum_received = end.get("sum_received") or {}
        sum_sent = end.get("sum_sent") or {}
        goodput = sum_received.get("bits_per_second") or sum_sent.get("bits_per_second")
        retransmits = sum_sent.get("retransmits")
    except Exception as exc:
        return [], [_event("network", worker_id, "iperf_parse_failed", {"error": str(exc)})]

    if goodput is None:
        return [], [_event("network", worker_id, "iperf_missing_goodput", {})]

    tags = {"receiver": receiver}
    metrics = [
        _metric("network", worker_id, "iperf.goodput_bps", float(goodput), "bps", tags),
    ]
    if retransmits is not None:
        metrics.append(
            _metric("network", worker_id, "iperf.retransmits", float(retransmits), "count", tags)
        )
    return metrics, []


def parse_fio_metrics(
    text: str,
    *,
    worker_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        payload = json.loads(text)
        jobs = payload.get("jobs") or []
    except Exception as exc:
        return [], [_event("storage", worker_id, "fio_parse_failed", {"error": str(exc)})]

    read_iops = sum(float((job.get("read") or {}).get("iops") or 0) for job in jobs)
    write_iops = sum(float((job.get("write") or {}).get("iops") or 0) for job in jobs)
    read_bps = sum(float((job.get("read") or {}).get("bw_bytes") or 0) * 8 for job in jobs)
    write_bps = sum(float((job.get("write") or {}).get("bw_bytes") or 0) * 8 for job in jobs)

    metrics = [
        _metric("storage", worker_id, "fio.read_iops", read_iops, "iops", {}),
        _metric("storage", worker_id, "fio.write_iops", write_iops, "iops", {}),
        _metric("storage", worker_id, "fio.read_bps", read_bps, "bps", {}),
        _metric("storage", worker_id, "fio.write_bps", write_bps, "bps", {}),
    ]
    for op in ("read", "write"):
        p95 = _max_percentile(jobs, op, "95.000000")
        p99 = _max_percentile(jobs, op, "99.000000")
        if p95 is not None:
            metrics.append(_metric("storage", worker_id, f"fio.{op}_p95_latency_ns", p95, "ns", {}))
        if p99 is not None:
            metrics.append(_metric("storage", worker_id, f"fio.{op}_p99_latency_ns", p99, "ns", {}))
    return metrics, []


def _max_percentile(jobs: list[dict[str, Any]], op: str, percentile: str) -> float | None:
    values = []
    for job in jobs:
        op_data = job.get(op) or {}
        percentiles = ((op_data.get("clat_ns") or {}).get("percentile")) or {}
        if percentile in percentiles:
            values.append(float(percentiles[percentile]))
    if not values:
        return None
    return max(values)


def _metric(
    plane: str,
    worker_id: str,
    metric: str,
    value: float,
    unit: str,
    tags: dict[str, str],
) -> dict[str, Any]:
    return {
        "plane": plane,
        "worker_id": worker_id,
        "metric": metric,
        "value": value,
        "unit": unit,
        "tags": tags,
    }


def _event(
    plane: str,
    worker_id: str,
    event: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "level": "warning",
        "plane": plane,
        "worker_id": worker_id,
        "event": event,
        "details": details,
    }
