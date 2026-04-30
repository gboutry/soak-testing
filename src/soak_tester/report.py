from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from soak_tester.planner import read_plan
from soak_tester.state import load_state
from soak_tester.util import atomic_write_json, read_json


def generate_final_report(run_dir: Path) -> dict[str, Any]:
    state = load_state(run_dir)
    plan = read_plan(run_dir / "plan.yaml")
    resources = read_json(run_dir / "resources.json", {"resources": []})
    metrics = _read_jsonl(run_dir / "metrics.jsonl")
    observability = read_json(
        run_dir / "observability" / "prometheus-summary.json",
        default=None,
    )
    report = {
        "run_id": state.run_id,
        "status": state.status,
        "target_percent": state.target_percent,
        "requested": {
            "compute_vcpus": plan.compute.target_vcpus,
            "network_gbps": plan.network.target_gbps,
            "storage_read_gbps": plan.storage.target_read_gbps,
            "storage_write_gbps": plan.storage.target_write_gbps,
            "api_ops_per_second": plan.api.target_ops_per_second,
        },
        "planned_resources": plan.model_dump(mode="json"),
        "created_resources": resources.get("resources", []),
        "active_resources": len(resources.get("resources", [])),
        "achieved": _summarize_metrics(metrics),
        "worker_failures": _count_events(run_dir, "worker_failed"),
        "worker_replacements": _count_events(run_dir, "worker_replaced"),
        "steady_state_window": {
            "started_at": state.timeline.steady_state_started_at,
            "ended_at": state.timeline.steady_state_ended_at,
        },
        "observability": observability,
        "cleanup_status": state.status if "cleanup" in state.status else None,
    }
    atomic_write_json(run_dir / "final-report.json", report)
    (run_dir / "final-report.md").write_text(render_markdown_report(report), encoding="utf-8")
    return report


def render_markdown_report(report: dict[str, Any]) -> str:
    requested = report["requested"]
    achieved = report["achieved"]
    lines = [
        f"# soak-tester final report: {report['run_id']}",
        "",
        f"Status: `{report['status']}`",
        f"Target: `{report['target_percent']}%`",
        "",
        "## Requested load",
        "",
        f"- Compute target: {requested['compute_vcpus']} vCPUs",
        f"- Network target: {requested['network_gbps']} Gbit/s",
        f"- Storage read target: {requested['storage_read_gbps']} Gbit/s",
        f"- Storage write target: {requested['storage_write_gbps']} Gbit/s",
        f"- API target: {requested['api_ops_per_second']} ops/s",
        "",
        "## Workload observed",
        "",
        f"- iperf workload goodput avg: {achieved.get('iperf.goodput_gbps_avg', 0)} Gbit/s",
        f"- fio read bandwidth avg: {achieved.get('fio.read_gbps_avg', 0)} Gbit/s",
        f"- fio write bandwidth avg: {achieved.get('fio.write_gbps_avg', 0)} Gbit/s",
        f"- fio read IOPS avg: {achieved.get('fio.read_iops_avg', 0)}",
        f"- fio write IOPS avg: {achieved.get('fio.write_iops_avg', 0)}",
        f"- API achieved avg: {achieved.get('api.ops_per_second_avg', 0)} ops/s",
        f"- API success avg: {achieved.get('api.success_ops_per_second_avg', 0)} ops/s",
        f"- API errors: {achieved.get('api.errors_avg', 0)}",
        f"- API error rate: {achieved.get('api.error_rate_avg', 0)}",
        f"- API latency p95: {achieved.get('api.latency_p95_ms_avg', 0)} ms",
        f"- API latency p99: {achieved.get('api.latency_p99_ms_avg', 0)} ms",
        "",
        "## Conclusion",
        "",
        "The requested targets and workload-observed values are reported from run artifacts. "
        "Prometheus data, when present, is infrastructure-side evidence from selected "
        "interfaces and does not prove exact physical fabric throughput.",
    ]
    if report.get("observability"):
        lines.extend(["", "## Prometheus evidence", "", json.dumps(report["observability"], indent=2)])
    return "\n".join(lines) + "\n"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _summarize_metrics(metrics: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    grouped: dict[str, dict[str, list[float]]] = {}
    for metric in metrics:
        grouped.setdefault(metric["metric"], {}).setdefault(_metric_series_key(metric), []).append(
            float(metric["value"])
        )
    for name, series in grouped.items():
        series_avgs = [sum(values) / len(values) for values in series.values()]
        aggregate_avg = _aggregate_series_averages(name, series_avgs)
        result[f"{name}_avg"] = aggregate_avg
        if name.endswith("_bps"):
            result[f"{name.removesuffix('_bps')}_gbps_avg"] = aggregate_avg / 1_000_000_000
        if name.endswith(".goodput_bps"):
            result[f"{name.removesuffix('_bps')}_gbps_avg"] = aggregate_avg / 1_000_000_000
    return result


def _aggregate_series_averages(name: str, series_avgs: list[float]) -> float:
    if not series_avgs:
        return 0.0
    if (
        name.endswith("_percent")
        or name.endswith(".error_rate")
        or name.endswith("_latency_ns")
        or name.endswith("_latency_ms")
    ):
        return sum(series_avgs) / len(series_avgs)
    return sum(series_avgs)


def _metric_series_key(metric: dict[str, Any]) -> str:
    tags = dict(metric.get("tags") or {})
    tags.pop("flow_file", None)
    stable_tags = json.dumps(tags, sort_keys=True, separators=(",", ":"))
    return f"{metric.get('worker_id', '')}:{stable_tags}"


def _count_events(run_dir: Path, event_name: str) -> int:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return 0
    return sum(1 for item in _read_jsonl(path) if item.get("event") == event_name)
