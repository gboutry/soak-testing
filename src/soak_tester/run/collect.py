from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

import requests

from soak_tester.util import atomic_write_json, ensure_dir, utcnow_iso


def collect_controller_artifacts(run_dir: Path, controller_url: str, run_id: str) -> dict[str, Any]:
    counts = {"metrics": 0, "events": 0, "errors": []}
    try:
        response = requests.get(f"{controller_url}/metrics", timeout=10)
        response.raise_for_status()
        metrics_payload = response.json()
        counts["metrics"] = append_controller_metrics(
            run_dir / "metrics.jsonl",
            run_id=run_id,
            payload=metrics_payload,
        )
    except (requests.RequestException, ValueError) as exc:
        counts["errors"].append(f"metrics: {exc}")
    try:
        response = requests.get(f"{controller_url}/events", timeout=10)
        response.raise_for_status()
        events_payload = response.json()
        counts["events"] = append_controller_events(
            run_dir / "events.jsonl",
            run_id=run_id,
            payload=events_payload,
        )
    except (requests.RequestException, ValueError) as exc:
        counts["errors"].append(f"events: {exc}")
    try:
        response = requests.get(f"{controller_url}/status", timeout=10)
        response.raise_for_status()
        atomic_write_json(run_dir / "controller-status.json", response.json())
    except (requests.RequestException, ValueError) as exc:
        counts["errors"].append(f"status: {exc}")
    return counts


def append_controller_metrics(path: Path, *, run_id: str, payload: dict[str, Any]) -> int:
    ensure_dir(path.parent)
    existing = _existing_keys(path)
    written = 0
    with path.open("a", encoding="utf-8") as stream:
        for metric in payload.get("metrics", []):
            normalized = dict(metric)
            normalized.setdefault("ts", utcnow_iso())
            normalized["run_id"] = run_id
            normalized.setdefault("tags", {})
            key = _stable_key(normalized)
            if key in existing:
                continue
            stream.write(json.dumps(normalized, sort_keys=True) + "\n")
            existing.add(key)
            written += 1
    return written


def append_controller_events(path: Path, *, run_id: str, payload: dict[str, Any]) -> int:
    ensure_dir(path.parent)
    existing = _existing_keys(path)
    written = 0
    with path.open("a", encoding="utf-8") as stream:
        for event in payload.get("events", []):
            normalized = dict(event)
            normalized.setdefault("ts", utcnow_iso())
            normalized["run_id"] = run_id
            normalized.setdefault("details", {})
            key = _stable_key(normalized)
            if key in existing:
                continue
            stream.write(json.dumps(normalized, sort_keys=True) + "\n")
            existing.add(key)
            written += 1
    return written


def wait_for_controller(
    controller_url: str,
    *,
    timeout_seconds: int = 120,
    diagnostics_path: Path | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        _check_readiness_stop(stop_requested)
        started = time.monotonic()
        try:
            response = requests.get(f"{controller_url}/status", timeout=5)
            response.raise_for_status()
            _record_readiness(diagnostics_path, started, {"http_status": response.status_code})
            _check_readiness_stop(stop_requested)
            return True
        except requests.RequestException as exc:
            _record_readiness(diagnostics_path, started, {"error": str(exc)})
            _check_readiness_stop(stop_requested)
            time.sleep(5)
    return False


def wait_for_worker_heartbeats(
    controller_url: str,
    *,
    expected_worker_ids: set[str],
    timeout_seconds: int = 300,
    diagnostics_path: Path | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> bool:
    _check_readiness_stop(stop_requested)
    if not expected_worker_ids:
        return True
    deadline = time.monotonic() + timeout_seconds
    seen: set[str] = set()
    while time.monotonic() < deadline:
        _check_readiness_stop(stop_requested)
        started = time.monotonic()
        try:
            response = requests.get(f"{controller_url}/status", timeout=5)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            _record_readiness(
                diagnostics_path,
                started,
                {"error": str(exc), "missing_worker_ids": sorted(expected_worker_ids - seen)},
            )
            _check_readiness_stop(stop_requested)
            time.sleep(5)
            continue
        for heartbeat in payload.get("heartbeats", []):
            worker_id = heartbeat.get("worker_id")
            if worker_id in expected_worker_ids:
                seen.add(worker_id)
        _record_readiness(
            diagnostics_path,
            started,
            {
                "http_status": response.status_code,
                "heartbeats": payload.get("heartbeats", []),
                "seen_worker_ids": sorted(seen),
                "missing_worker_ids": sorted(expected_worker_ids - seen),
            },
        )
        _check_readiness_stop(stop_requested)
        if expected_worker_ids.issubset(seen):
            return True
        time.sleep(5)
    return False


def _check_readiness_stop(stop_requested: Callable[[], bool] | None) -> None:
    if stop_requested is not None and stop_requested():
        raise InterruptedError("Stop requested during readiness")


def _record_readiness(path: Path | None, started: float, payload: dict[str, Any]) -> None:
    if path is None:
        return
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {"ts": utcnow_iso(), "elapsed_seconds": time.monotonic() - started, **payload}
            )
            + "\n"
        )


def collect_failure_console_outputs(conn, run_dir: Path) -> None:
    """Retain a bounded console sample before failed-run teardown."""
    placement_path = run_dir / "placement.json"
    placement = json.loads(placement_path.read_text()) if placement_path.exists() else []
    # Creation IDs are durable even when server readiness/placement failed.
    resources_path = run_dir / "resources.json"
    resources = (
        json.loads(resources_path.read_text()).get("resources", [])
        if resources_path.exists()
        else []
    )
    known = {item["server_id"] for item in placement}
    for resource in resources:
        if resource.get("type") == "server" and resource["id"] not in known:
            placement.append(
                {
                    "server_id": resource["id"],
                    "worker_id": resource.get("role", "unknown"),
                    "role": resource.get("role", "unknown"),
                }
            )
    if not placement:
        return
    missing = set()
    diagnostics = run_dir / "readiness-workers.jsonl"
    if diagnostics.exists():
        missing = set(json.loads(diagnostics.read_text().splitlines()[-1])["missing_worker_ids"])
    candidates = [item for item in placement if item["worker_id"] == "controller"]
    sampled_roles = set()
    for item in placement:
        role = item.get("role")
        if item["worker_id"] in missing and role not in sampled_roles:
            candidates.append(item)
            sampled_roles.add(role)
    endpoint = conn.session.get_endpoint(service_type="compute").rstrip("/")
    results = []
    for item in candidates[:5]:
        record = {"worker_id": item["worker_id"], "server_id": item["server_id"]}
        try:
            response = conn.session.post(
                endpoint + "/servers/" + item["server_id"] + "/action",
                json={"os-getConsoleOutput": {"length": 200}},
                timeout=8,
            )
            record["console"] = response.json()
        except Exception as exc:
            record["error"] = str(exc)
        results.append(record)
    path = run_dir / "failure-consoles.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps(results, indent=2) + "\n")


def _existing_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            keys.add(_stable_key(json.loads(line)))
        except json.JSONDecodeError:
            continue
    return keys


def _stable_key(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
