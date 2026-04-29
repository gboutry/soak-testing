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
        f"- API achieved avg: {achieved.get('api.ops_per_second_avg', 0)} ops/s",
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
    grouped: dict[str, list[float]] = {}
    for metric in metrics:
        grouped.setdefault(metric["metric"], []).append(float(metric["value"]))
    for name, values in grouped.items():
        avg = sum(values) / len(values)
        result[f"{name}_avg"] = avg
        if name.endswith("_bps") or name.endswith(".goodput_bps"):
            result[f"{name.removesuffix('_bps').removesuffix('.goodput')}.goodput_gbps_avg"] = (
                avg / 1_000_000_000
            )
    return result


def _count_events(run_dir: Path, event_name: str) -> int:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return 0
    return sum(1 for item in _read_jsonl(path) if item.get("event") == event_name)
