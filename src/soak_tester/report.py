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
    domain_summary = build_domain_summary(run_dir, report, metrics)
    atomic_write_json(run_dir / "summary.json", domain_summary)
    (run_dir / "summary.md").write_text(
        render_domain_summary_markdown(domain_summary),
        encoding="utf-8",
    )
    return report


def build_domain_summary(
    run_dir: Path,
    report: dict[str, Any],
    metrics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    metrics = metrics if metrics is not None else _read_jsonl(run_dir / "metrics.jsonl")
    requested = report.get("requested") or {}
    achieved = report.get("achieved") or {}
    rally_summary = read_json(run_dir / "api-rally" / "summary.json", default=None)
    return {
        "run_id": report.get("run_id"),
        "status": report.get("status"),
        "steady_state_window": report.get("steady_state_window"),
        "compute": _compute_summary(requested, achieved, metrics),
        "network": _network_summary(requested, achieved, metrics),
        "storage": _storage_summary(requested, achieved, metrics),
        "api": _api_summary(requested, achieved, rally_summary),
        "artifacts": {
            "final_report_json": "final-report.json",
            "final_report_markdown": "final-report.md",
            "metrics": "metrics.jsonl",
            "events": "events.jsonl",
            "rally_summary": "api-rally/summary.json" if rally_summary else None,
            "rally_results": "api-rally/results.json" if rally_summary else None,
        },
    }


def render_domain_summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# soak-tester summary: {summary.get('run_id')}",
        "",
        f"Status: `{summary.get('status')}`",
        "",
        "## Compute",
        "",
        *_render_key_values(summary["compute"]),
        "",
        "## Network",
        "",
        *_render_key_values(summary["network"]),
        "",
        "## Storage",
        "",
        *_render_key_values(summary["storage"]),
        "",
        "## API",
        "",
        *_render_key_values({k: v for k, v in summary["api"].items() if k != "scenarios"}),
    ]
    scenarios = summary["api"].get("scenarios") or {}
    if scenarios:
        lines.extend(["", "### API scenarios", ""])
        lines.extend(
            [
                "| scenario | attempts | successes | errors | p50 ms | p95 ms | p99 ms |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for name, item in sorted(scenarios.items()):
            lines.append(
                "| {name} | {attempts} | {successes} | {errors} | {p50} | {p95} | {p99} |".format(
                    name=name,
                    attempts=_fmt(item.get("attempts", 0)),
                    successes=_fmt(item.get("successes", 0)),
                    errors=_fmt(item.get("errors", 0)),
                    p50=_fmt(item.get("latency_p50_ms", 0)),
                    p95=_fmt(item.get("latency_p95_ms", 0)),
                    p99=_fmt(item.get("latency_p99_ms", 0)),
                )
            )
    lines.extend(
        [
            "",
            "## Artifacts",
            "",
            *_render_key_values({k: v for k, v in summary["artifacts"].items() if v}),
            "",
            "Values are workload-observed unless explicitly labelled otherwise.",
            "",
        ]
    )
    return "\n".join(lines)


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
        f"- Rally API achieved avg: {achieved.get('api.rally.total_ops_per_second_avg', 0)} ops/s",
        f"- Rally API success avg: {achieved.get('api.rally.success_ops_per_second_avg', 0)} ops/s",
        f"- API errors: {achieved.get('api.errors_avg', 0)}",
        f"- API error rate: {achieved.get('api.error_rate_avg', 0)}",
        f"- Rally API errors: {achieved.get('api.rally.errors_avg', 0)}",
        f"- Rally API error rate: {achieved.get('api.rally.error_rate_avg', 0)}",
        f"- API latency p95: {achieved.get('api.latency_p95_ms_avg', 0)} ms",
        f"- API latency p99: {achieved.get('api.latency_p99_ms_avg', 0)} ms",
        f"- Rally API latency p95: {achieved.get('api.rally.latency_p95_ms_avg', 0)} ms",
        f"- Rally API latency p99: {achieved.get('api.rally.latency_p99_ms_avg', 0)} ms",
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


def _compute_summary(
    requested: dict[str, Any],
    achieved: dict[str, float],
    metrics: list[dict[str, Any]],
) -> dict[str, Any]:
    workers = {
        str(item.get("worker_id"))
        for item in metrics
        if str(item.get("worker_id", "")).startswith("compute-")
    }
    return {
        "requested_vcpus": requested.get("compute_vcpus", 0),
        "observed_workers": len(workers),
        "guest_cpu_percent_avg": achieved.get("guest.cpu_percent_avg", 0),
        "guest_memory_used_percent_avg": achieved.get("guest.memory_used_percent_avg", 0),
    }


def _network_summary(
    requested: dict[str, Any],
    achieved: dict[str, float],
    metrics: list[dict[str, Any]],
) -> dict[str, Any]:
    goodput = _metric_values(metrics, "iperf.goodput_bps")
    retransmits = _metric_values(metrics, "iperf.retransmits")
    flows = {
        (
            str(item.get("worker_id", "")),
            str((item.get("tags") or {}).get("flow_id") or (item.get("tags") or {}).get("receiver") or ""),
        )
        for item in metrics
        if item.get("metric") == "iperf.goodput_bps"
    }
    return {
        "requested_gbps": requested.get("network_gbps", 0),
        "iperf_goodput_gbps_avg": achieved.get("iperf.goodput_gbps_avg", 0),
        "iperf_flow_samples": len(goodput),
        "iperf_unique_flows": len(flows),
        "iperf_retransmits_total": sum(retransmits),
        "iperf_retransmits_max": max(retransmits, default=0),
    }


def _storage_summary(
    requested: dict[str, Any],
    achieved: dict[str, float],
    metrics: list[dict[str, Any]],
) -> dict[str, Any]:
    workers = {
        str(item.get("worker_id"))
        for item in metrics
        if str(item.get("worker_id", "")).startswith("storage-")
    }
    return {
        "requested_read_gbps": requested.get("storage_read_gbps", 0),
        "requested_write_gbps": requested.get("storage_write_gbps", 0),
        "observed_workers": len(workers),
        "fio_read_gbps_avg": achieved.get("fio.read_gbps_avg", 0),
        "fio_write_gbps_avg": achieved.get("fio.write_gbps_avg", 0),
        "fio_read_iops_avg": achieved.get("fio.read_iops_avg", 0),
        "fio_write_iops_avg": achieved.get("fio.write_iops_avg", 0),
        "fio_read_p95_latency_ns_avg": achieved.get("fio.read_p95_latency_ns_avg", 0),
        "fio_write_p95_latency_ns_avg": achieved.get("fio.write_p95_latency_ns_avg", 0),
    }


def _api_summary(
    requested: dict[str, Any],
    achieved: dict[str, float],
    rally_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    if rally_summary:
        return {
            "mode": "rally",
            "requested_ops_per_second": requested.get("api_ops_per_second", 0),
            "total_attempts": rally_summary.get("total_attempts", 0),
            "successes": rally_summary.get("successes", 0),
            "errors": rally_summary.get("errors", 0),
            "error_rate": rally_summary.get("error_rate", 0),
            "total_ops_per_second": rally_summary.get("total_ops_per_second", 0),
            "success_ops_per_second": rally_summary.get("success_ops_per_second", 0),
            "latency_p50_ms": rally_summary.get("latency_p50_ms", 0),
            "latency_p95_ms": rally_summary.get("latency_p95_ms", 0),
            "latency_p99_ms": rally_summary.get("latency_p99_ms", 0),
            "scenarios": rally_summary.get("scenarios", {}),
        }
    return {
        "mode": "native",
        "requested_ops_per_second": requested.get("api_ops_per_second", 0),
        "total_ops_per_second": achieved.get("api.ops_per_second_avg", 0),
        "success_ops_per_second": achieved.get("api.success_ops_per_second_avg", 0),
        "errors": achieved.get("api.errors_avg", 0),
        "error_rate": achieved.get("api.error_rate_avg", 0),
        "latency_p95_ms": achieved.get("api.latency_p95_ms_avg", 0),
        "latency_p99_ms": achieved.get("api.latency_p99_ms_avg", 0),
        "scenarios": {},
    }


def _metric_values(metrics: list[dict[str, Any]], name: str) -> list[float]:
    return [float(item["value"]) for item in metrics if item.get("metric") == name]


def _render_key_values(values: dict[str, Any]) -> list[str]:
    return [f"- {key.replace('_', ' ')}: {_fmt(value)}" for key, value in values.items()]


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


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
