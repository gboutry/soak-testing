"""The same evidence boundary for a native run, its report and sequential campaigns."""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import yaml

from soak_tester.config import load_config
from soak_tester.util import read_json


def read_jsonl(path: Path) -> list[dict]:
    return (
        [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if path.exists()
        else []
    )


def timestamp(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def window_metrics(metrics, start, end):
    """Only complete contained intervals or timestamped point observations qualify."""
    if start is None or end is None or end <= start:
        return []
    eligible = []
    for metric in metrics:
        try:
            if not math.isfinite(float(metric["value"])):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        if metric.get("metric", "").startswith(("iperf.goodput", "fio.")):
            left, right = (
                timestamp(metric.get("measurement_start")),
                timestamp(metric.get("measurement_end")),
            )
            if left is None or right is None or not start <= left < right <= end:
                continue
        else:
            point = timestamp(metric.get("ts"))
            if point is None or not start <= point <= end:
                continue
        eligible.append(metric)
    return eligible


def assess_run(run_dir: Path, config_path: Path | None = None) -> dict:
    cfg = load_config(config_path or run_dir / "config.yaml")
    plan = yaml.safe_load((run_dir / "plan.yaml").read_text())
    state = read_json(run_dir / "state.json", {})
    metrics, events = read_jsonl(run_dir / "metrics.jsonl"), read_jsonl(run_dir / "events.jsonl")
    timeline = state.get("timeline", {})
    start, end = (
        timestamp(timeline.get("steady_state_started_at")),
        timestamp(timeline.get("steady_state_ended_at")),
    )
    eligible = window_metrics(metrics, start, end)
    errors = []
    failure_events = {
        "workload_start_failed",
        "workload_failed",
        "worker_failed",
        "iperf_failed",
        "iperf_parse_failed",
        "iperf_missing_goodput",
        "fio_failed",
        "fio_parse_failed",
        "api_operation_error",
        "rally_failed",
        "run_failed",
    }
    failed_attempts = [e for e in events if e.get("event") in failure_events]
    errors.extend(e["event"] for e in failed_attempts)
    for metric in metrics:
        if (
            metric.get("metric")
            in {"fio.failed", "iperf.flow_failed", "api.errors", "api.rally.errors"}
            and metric.get("value", 0) > 0
        ):
            errors.append(metric["metric"])
    measurement_errors = []
    if any(getattr(cfg.workloads, plane).enabled for plane in ["network", "compute", "storage"]):
        collection = state.get("summary", {}).get("controller_collection")
        if not isinstance(collection, dict) or collection.get("errors"):
            measurement_errors.append("Final controller collection incomplete or unverified")
    if any(
        not isinstance(m.get("value"), (int, float)) or not math.isfinite(m["value"])
        for m in metrics
    ):
        measurement_errors.append("Invalid or non-finite metric value")
    if start is None or end is None or end <= start:
        measurement_errors.append("Measurement window missing or invalid")
    coverage = {}
    unfinished_workers = []
    delivery_failures = []
    heartbeats = {
        h.get("worker_id"): h
        for h in read_json(run_dir / "controller-status.json", {}).get("heartbeats", [])
    }
    placement = {
        p["worker_id"]: p.get("hypervisor_hostname")
        for p in read_json(run_dir / "placement.json", [])
    }
    for plane, metric_name, expected, key, label in [
        (
            "storage",
            {"fio.read_iops", "fio.write_iops"},
            {f"storage-{i:03d}" for i in range(plan.get("storage", {}).get("worker_count", 0))},
            "worker_id",
            "FIO worker",
        ),
        (
            "compute",
            {"guest.cpu_percent", "guest.memory_used_percent"},
            {f"compute-{i:03d}" for i in range(plan.get("compute", {}).get("worker_count", 0))},
            "worker_id",
            "Compute",
        ),
        (
            "network",
            {"iperf.goodput_bps"},
            {f["flow_id"] for f in plan.get("network", {}).get("flow_assignments", [])},
            "flow_id",
            "Network flow",
        ),
    ]:
        if not getattr(cfg.workloads, plane).enabled:
            continue
        observed = {
            str((m.get("tags", {}) if key == "flow_id" else m).get(key))
            for m in eligible
            if m.get("metric") in metric_name
        }
        missing = sorted(expected - observed)
        coverage[plane] = {
            "expected": len(expected),
            "observed": len(observed & expected),
            "missing": missing,
        }
        if not expected or missing:
            measurement_errors.append(
                f"{label} evidence incomplete: {len(observed & expected)}/{len(expected)}"
            )
        if plane == "network" and cfg.workloads.network.protocol == "udp":
            required = {
                "iperf.goodput_bps",
                "iperf.udp_lost_packets",
                "iperf.udp_packets",
                "iperf.udp_lost_percent",
                "iperf.udp_jitter_ms",
            }
            complete = {
                flow
                for flow in expected
                if required.issubset(
                    {
                        m.get("metric")
                        for m in eligible
                        if m.get("tags", {}).get("flow_id") == flow
                        and m.get("tags", {}).get("protocol") == "udp"
                    }
                )
            }
            if complete != expected:
                measurement_errors.append("UDP receiver/loss evidence incomplete")
        if plane == "network" and cfg.workloads.network.capture_receiver_intervals:
            retained = {
                e.get("details", {}).get("flow_id")
                for e in events
                if e.get("event") == "iperf_receiver_intervals"
                and e.get("details", {}).get("intervals")
            }
            if not expected.issubset(retained):
                measurement_errors.append("Requested receiver interval evidence incomplete")
        worker_ids = (
            (
                {
                    f"network-sender-{i:03d}"
                    for i in range(plan.get("network", {}).get("sender_count", 0))
                }
                | {
                    f"network-receiver-{i:03d}"
                    for i in range(plan.get("network", {}).get("receiver_count", 0))
                }
            )
            if plane == "network"
            else expected
        )
        unfinished_workers.extend(
            w for w in worker_ids if heartbeats.get(w, {}).get("status") != "finished"
        )
        delivery_failures.extend(
            w for w in worker_ids if heartbeats.get(w, {}).get("delivery_failures", 0)
        )
        missing_placement = sorted(w for w in worker_ids if not placement.get(w))
        if missing_placement:
            measurement_errors.append(
                f"Actual {plane} placement missing: {', '.join(missing_placement)}"
            )
        if plane == "compute":
            for name in metric_name:
                measured = {m.get("worker_id") for m in eligible if m.get("metric") == name}
                if not expected.issubset(measured):
                    measurement_errors.append(f"Compute {name} evidence incomplete")
        if plane == "network":
            directions = (
                {"forward", "reverse"}
                if cfg.workloads.network.bidirectional
                else {"reverse" if cfg.workloads.network.reverse else "forward"}
            )
            flow_intervals = {}
            for flow in expected:
                samples = [
                    m
                    for m in eligible
                    if m.get("metric") == "iperf.goodput_bps"
                    and m.get("tags", {}).get("flow_id") == flow
                ]
                observed_directions = {
                    m.get("tags", {}).get("direction", "forward") for m in samples
                }
                if not directions.issubset(observed_directions):
                    measurement_errors.append(f"Network flow direction evidence incomplete: {flow}")
                flow_intervals[flow] = sorted(
                    {(m["measurement_start"], m["measurement_end"]) for m in samples}
                )
            coverage[plane]["observed_intervals"] = flow_intervals
            coverage[plane]["observed_seconds"] = {
                flow: _covered_seconds(intervals) for flow, intervals in flow_intervals.items()
            }
            coverage[plane]["window_seconds"] = (
                end - start if start is not None and end is not None else None
            )
            placement = {
                p["worker_id"]: p.get("hypervisor_hostname")
                for p in read_json(run_dir / "placement.json", [])
            }
            requested = {
                **plan.get("network", {}).get("sender_worker_hosts", {}),
                **plan.get("network", {}).get("receiver_worker_hosts", {}),
            }
            for worker, host in requested.items():
                if placement.get(worker) != host:
                    measurement_errors.append(
                        f"Network placement mismatch: {worker} expected {host}"
                    )
    if cfg.workloads.api.enabled:
        prefix = "api.rally." if cfg.workloads.api.engine == "rally" else "api."
        if not any(
            m.get("metric")
            == prefix
            + ("total_ops_per_second" if cfg.workloads.api.engine == "rally" else "ops_per_second")
            for m in eligible
        ):
            measurement_errors.append("API rate evidence missing in measurement window")
    if unfinished_workers:
        measurement_errors.append(
            "Final worker completion evidence missing: " + ", ".join(sorted(unfinished_workers))
        )
    if delivery_failures:
        measurement_errors.append(
            "Guest evidence delivery failed: " + ", ".join(sorted(delivery_failures))
        )
    errors.extend(measurement_errors)
    return {
        "workload_success": not unfinished_workers
        and not delivery_failures
        and not failed_attempts
        and not any(
            m.get("metric") in {"fio.failed", "iperf.flow_failed", "api.errors", "api.rally.errors"}
            and m.get("value", 0) > 0
            for m in metrics
        )
        and state.get("summary", {}).get("workload_status") == "completed",
        "measurement_validity": "invalid" if measurement_errors else "valid_observations",
        "errors": sorted(set(errors)),
        "failed_attempts": failed_attempts,
        "coverage": coverage,
        "included_metric_count": len(eligible),
        "excluded_metric_count": len(metrics) - len(eligible),
        "statistic": "sum of per-flow or per-worker means of contained completed observations; interval rates are duration weighted",
        "continuous_rate_claimed": False,
        "window": {
            "started_at": timeline.get("steady_state_started_at"),
            "ended_at": timeline.get("steady_state_ended_at"),
        },
    }


def _covered_seconds(intervals):
    total, end = 0.0, float("-inf")
    for left, right in sorted(intervals):
        total += max(0, right - max(left, end))
        end = max(end, right)
    return total
