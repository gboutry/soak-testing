from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

import yaml

from soak_tester.util import read_json


def apply_config_patch(config: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply dotted-path values to a config dictionary."""

    for key, value in patch.items():
        target = config
        parts = key.split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
            if not isinstance(target, dict):
                raise ValueError(f"cannot patch through non-mapping key: {key}")
        target[parts[-1]] = value
    return config


def load_matrix(path: Path) -> list[dict[str, Any]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    experiments = payload.get("experiments")
    if not isinstance(experiments, list) or not experiments:
        raise ValueError("matrix file requires a non-empty 'experiments' list")
    names = [item.get("name") for item in experiments if isinstance(item, dict)]
    if len(set(names)) != len(names):
        raise ValueError("experiment names must be unique")
    return experiments


def summarize_network_run(run_dir: Path) -> dict[str, Any]:
    report = read_json(run_dir / "final-report.json", default={})
    plan = yaml.safe_load((run_dir / "plan.yaml").read_text(encoding="utf-8"))
    metrics = _read_jsonl(run_dir / "metrics.jsonl")
    placement = read_json(run_dir / "placement.json", default=[])
    flow_values = _flow_goodput_gbps(metrics)
    retransmits = [
        float(item["value"])
        for item in metrics
        if item.get("metric") == "iperf.retransmits"
    ]
    cross_host = _cross_host_flow_count(metrics)
    return {
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "status": report.get("status"),
        "goodput_gbps": float((report.get("achieved") or {}).get("iperf.goodput_gbps_avg") or 0),
        "retransmits": sum(retransmits),
        "flow_count": len(flow_values),
        "cross_host_flow_count": cross_host,
        "per_flow_gbps": _stats(flow_values),
        "network_plan": (plan or {}).get("network", {}),
        "network_workers": _network_worker_count(placement),
    }


def summarize_experiment_runs(
    experiment_name: str,
    run_dirs: list[Path],
    *,
    baseline_gbps: float | None = None,
) -> dict[str, Any]:
    runs = [summarize_network_run(path) for path in run_dirs]
    goodputs = [run["goodput_gbps"] for run in runs]
    summary = {
        "name": experiment_name,
        "runs": runs,
        "goodput_gbps": _stats(goodputs),
        "best_run": max(runs, key=lambda item: item["goodput_gbps"], default=None),
        "all_cross_host": all(
            run["cross_host_flow_count"] == run["flow_count"] and run["flow_count"] > 0
            for run in runs
        ),
    }
    if baseline_gbps and baseline_gbps > 0:
        summary["improvement_percent_vs_baseline"] = (
            (summary["goodput_gbps"]["mean"] - baseline_gbps) / baseline_gbps * 100
        )
    return summary


def render_matrix_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Network Tuning Matrix",
        "",
        "| experiment | runs | mean Gbit/s | median | max | stddev | improvement | cross-host |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in result.get("experiments", []):
        stats = item["goodput_gbps"]
        improvement = item.get("improvement_percent_vs_baseline")
        improvement_text = "" if improvement is None else f"{improvement:.1f}%"
        lines.append(
            "| {name} | {runs} | {mean:.3f} | {median:.3f} | {max:.3f} | {stddev:.3f} | {improvement} | {cross} |".format(
                name=item["name"],
                runs=len(item["runs"]),
                mean=stats["mean"],
                median=stats["median"],
                max=stats["max"],
                stddev=stats["stddev"],
                improvement=improvement_text,
                cross="yes" if item["all_cross_host"] else "no",
            )
        )
    best = result.get("best")
    if best:
        lines.extend(
            [
                "",
                "## Best Configuration",
                "",
                f"- Experiment: `{best['name']}`",
                f"- Mean: `{best['goodput_gbps']['mean']:.3f} Gbit/s`",
                f"- Max: `{best['goodput_gbps']['max']:.3f} Gbit/s`",
                "",
                "This is workload-observed iperf goodput. It is not physical fabric proof.",
            ]
        )
    return "\n".join(lines) + "\n"


def write_matrix_summary(path: Path, experiments: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = experiments[0]["goodput_gbps"]["mean"] if experiments else 0
    for item in experiments[1:]:
        if baseline > 0:
            item["improvement_percent_vs_baseline"] = (
                (item["goodput_gbps"]["mean"] - baseline) / baseline * 100
            )
    best = max(experiments, key=lambda item: item["goodput_gbps"]["mean"], default=None)
    result = {
        "baseline": experiments[0] if experiments else None,
        "best": best,
        "experiments": experiments,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    path.with_suffix(".md").write_text(render_matrix_markdown(result), encoding="utf-8")
    return result


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _flow_goodput_gbps(metrics: list[dict[str, Any]]) -> list[float]:
    flows: dict[tuple[str, str], list[float]] = {}
    for item in metrics:
        if item.get("metric") != "iperf.goodput_bps":
            continue
        tags = item.get("tags") or {}
        key = (str(item.get("worker_id") or ""), str(tags.get("flow_id") or tags.get("receiver") or ""))
        flows.setdefault(key, []).append(float(item["value"]) / 1_000_000_000)
    return [sum(values) / len(values) for values in flows.values()]


def _cross_host_flow_count(metrics: list[dict[str, Any]]) -> int:
    flows = set()
    for item in metrics:
        if item.get("metric") != "iperf.goodput_bps":
            continue
        tags = item.get("tags") or {}
        source = tags.get("source_host")
        dest = tags.get("dest_host")
        if source and dest and source != dest:
            flows.add((item.get("worker_id"), tags.get("flow_id") or tags.get("receiver")))
    return len(flows)


def _network_worker_count(placement: list[dict[str, Any]]) -> int:
    return sum(1 for item in placement if str(item.get("worker_id") or "").startswith("network-"))


def _stats(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "mean": 0.0, "median": 0.0, "max": 0.0, "stddev": 0.0}
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "max": max(values),
        "stddev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }
