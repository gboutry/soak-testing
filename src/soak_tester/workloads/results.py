from __future__ import annotations

import json
from typing import Any


def parse_iperf3_metrics(
    text: str,
    *,
    worker_id: str,
    receiver: str,
    tags: dict[str, str] | None = None,
    bidirectional: bool = False,
    reverse: bool = False,
    started_at: float | None = None,
    omit_seconds: int = 0,
    protocol: str = "tcp",
    capture_receiver_intervals: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    metric_tags = {"receiver": receiver, **(tags or {})}
    try:
        payload = json.loads(text)
        if payload.get("error"):
            return [_metric("network", worker_id, "iperf.flow_failed", 1, "count", metric_tags)], [
                _event(
                    "network",
                    worker_id,
                    "iperf_failed",
                    {"error": str(payload["error"]), **metric_tags},
                )
            ]
        if protocol not in {"tcp", "udp"}:
            raise ValueError("unsupported network protocol")
        if protocol == "udp":
            metric_tags["protocol"] = "udp"
        end = payload.get("end", {})
        received = end.get("sum_received") or {}
        udp_metrics = []
        if protocol == "udp":
            if received.get("sender") is not False:
                raise ValueError("UDP requires an explicit receiver summary")
            for key, unit in [
                ("lost_packets", "count"),
                ("packets", "count"),
                ("lost_percent", "percent"),
                ("jitter_ms", "ms"),
            ]:
                if key not in received:
                    raise ValueError("Missing UDP receiver field: " + key)
                udp_metrics.append(
                    _metric(
                        "network",
                        worker_id,
                        "iperf.udp_" + key,
                        float(received[key]),
                        unit,
                        metric_tags,
                    )
                )
        # Sender counters describe the same transfer; they are never added to receiver goodput.
        if received.get("bits_per_second") is None:
            raise ValueError("missing receiver goodput")
        directions = [("reverse" if reverse else "forward", received, end.get("sum_sent") or {})]
        if bidirectional:
            reverse_received = end.get("sum_received_bidir_reverse") or {}
            if reverse_received.get("bits_per_second") is None:
                raise ValueError("missing bidirectional reverse receiver goodput")
            directions.append(
                ("reverse", reverse_received, end.get("sum_sent_bidir_reverse") or {})
            )
        metrics = []
        for direction, received, sent in directions:
            direction_tags = dict(metric_tags)
            if bidirectional or reverse:
                direction_tags["direction"] = direction
            if direction == "reverse":
                direction_tags["source_host"], direction_tags["dest_host"] = (
                    metric_tags.get("dest_host", ""),
                    metric_tags.get("source_host", ""),
                )
            item = _metric(
                "network",
                worker_id,
                "iperf.goodput_bps",
                float(received["bits_per_second"]),
                "bps",
                direction_tags,
            )
            if started_at is not None:
                seconds = float(received.get("seconds") or 0)
                if seconds <= 0:
                    raise ValueError("missing measurement duration")
                item["measurement_start"] = started_at + omit_seconds
                item["measurement_end"] = started_at + omit_seconds + seconds
            metrics.append(item)
            if sent.get("retransmits") is not None:
                metrics.append(
                    _metric(
                        "network",
                        worker_id,
                        "iperf.retransmits",
                        float(sent["retransmits"]),
                        "count",
                        direction_tags,
                    )
                )
        events = []
        if capture_receiver_intervals:
            server = payload.get("server_output_json")
            if isinstance(server, str):
                server = json.loads(server)
            if not isinstance(server, dict) or not server.get("intervals"):
                raise ValueError("Requested receiver intervals missing")
            events.append(
                _event(
                    "network",
                    worker_id,
                    "iperf_receiver_intervals",
                    {**metric_tags, "intervals": server["intervals"]},
                )
            )
        return metrics + udp_metrics, events
    except Exception as exc:
        return [], [
            _event("network", worker_id, "iperf_parse_failed", {"error": str(exc), **metric_tags})
        ]


def parse_fio_metrics(
    text: str,
    *,
    worker_id: str,
    started_at: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        payload = json.loads(text)
        jobs = payload.get("jobs") or []
    except Exception as exc:
        return [], [_event("storage", worker_id, "fio_parse_failed", {"error": str(exc)})]

    errors = [job.get("error") for job in jobs if job.get("error")]
    if not jobs or errors:
        return [_metric("storage", worker_id, "fio.failed", 1, "count", {})], [
            _event("storage", worker_id, "fio_failed", {"job_errors": errors})
        ]

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
    if started_at is not None:
        runtime_ms = max(
            float(
                job.get("job_runtime")
                or max(
                    (job.get("read") or {}).get("runtime", 0),
                    (job.get("write") or {}).get("runtime", 0),
                )
            )
            for job in jobs
        )
        if runtime_ms <= 0:
            return [], [
                _event(
                    "storage",
                    worker_id,
                    "fio_parse_failed",
                    {"error": "missing measurement duration"},
                )
            ]
        for item in metrics:
            item["measurement_start"] = started_at
            item["measurement_end"] = started_at + runtime_ms / 1000
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
