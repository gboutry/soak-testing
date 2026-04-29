from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from soak_tester.config import load_config
from soak_tester.observability.prometheus import PrometheusClient
from soak_tester.observability.query_pack import render_builtin_pack
from soak_tester.observability.summarize import summarize_series
from soak_tester.state import load_state
from soak_tester.util import atomic_write_json, ensure_dir, read_json


def inspect_prometheus(run_dir: Path) -> dict[str, Any]:
    config = load_config(run_dir / "config.yaml")
    placement = read_json(run_dir / "placement.json", default=[])
    result = {
        "enabled": config.observability.prometheus.enabled,
        "query_packs": config.observability.prometheus.query_packs,
        "has_hypervisors": bool(_compute_hosts_regex(placement)),
    }
    if result["enabled"]:
        PrometheusClient(config.observability.prometheus).query("1")
        result["reachable"] = True
    return result


def collect_prometheus(run_dir: Path) -> dict[str, Any]:
    config = load_config(run_dir / "config.yaml")
    state = load_state(run_dir)
    placement = read_json(run_dir / "placement.json", default=[])
    obs_dir = ensure_dir(run_dir / "observability")
    compute_hosts = _compute_hosts_regex(placement)
    queries: dict[str, str] = {}
    for pack in config.observability.prometheus.query_packs:
        queries.update(
            render_builtin_pack(
                pack,
                compute_hosts=compute_hosts or ".*",
                network_devices=config.observability.prometheus.network.device_regex,
                excluded_network_devices=config.observability.prometheus.network.exclude_device_regex,
            )
        )
    (obs_dir / "prometheus-queries.yaml").write_text(
        yaml.safe_dump(queries, sort_keys=False),
        encoding="utf-8",
    )
    start = state.timeline.steady_state_started_at or state.timeline.started_at
    end = state.timeline.steady_state_ended_at or state.timeline.ended_at or start
    client = PrometheusClient(config.observability.prometheus)
    results = {
        name: client.query_range(
            query,
            start=start,
            end=end,
            step=config.observability.prometheus.query_step,
        )
        for name, query in queries.items()
    }
    atomic_write_json(obs_dir / "prometheus-results.json", results)
    summary = summarize_prometheus_results(results)
    atomic_write_json(obs_dir / "prometheus-summary.json", summary)
    (obs_dir / "prometheus-summary.md").write_text(render_prometheus_summary(summary), encoding="utf-8")
    return summary


def summarize_prometheus_results(results: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name, payload in results.items():
        values: list[float] = []
        for series in payload.get("data", {}).get("result", []):
            for _, value in series.get("values", []):
                values.append(float(value))
        summary[name] = summarize_series(values)
    if "compute_host_network_rx_bps" in summary and "compute_host_network_tx_bps" in summary:
        summary["compute_host_network_rx_tx_avg_gbps"] = (
            summary["compute_host_network_rx_bps"]["avg_gbps"]
            + summary["compute_host_network_tx_bps"]["avg_gbps"]
        )
    return summary


def render_prometheus_summary(summary: dict[str, Any]) -> str:
    return (
        "# Prometheus evidence summary\n\n"
        "Prometheus is reported as infrastructure-side evidence only. "
        "It does not prove exact physical fabric throughput.\n\n"
        f"```json\n{summary}\n```\n"
    )


def _compute_hosts_regex(placement: list[dict[str, Any]]) -> str:
    hosts = sorted(
        {
            item.get("hypervisor_hostname")
            for item in placement
            if item.get("hypervisor_hostname")
        }
    )
    return "|".join(re.escape(host) for host in hosts)
