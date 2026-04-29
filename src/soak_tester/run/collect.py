from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

from soak_tester.util import ensure_dir, utcnow_iso


def collect_controller_artifacts(run_dir: Path, controller_url: str, run_id: str) -> dict[str, int]:
    counts = {"metrics": 0, "events": 0}
    try:
        metrics_payload = requests.get(f"{controller_url}/metrics", timeout=10).json()
        counts["metrics"] = append_controller_metrics(
            run_dir / "metrics.jsonl",
            run_id=run_id,
            payload=metrics_payload,
        )
    except requests.RequestException:
        pass
    try:
        events_payload = requests.get(f"{controller_url}/events", timeout=10).json()
        counts["events"] = append_controller_events(
            run_dir / "events.jsonl",
            run_id=run_id,
            payload=events_payload,
        )
    except requests.RequestException:
        pass
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


def wait_for_controller(controller_url: str, *, timeout_seconds: int = 120) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{controller_url}/status", timeout=5)
            response.raise_for_status()
            return True
        except requests.RequestException:
            time.sleep(5)
    return False


def wait_for_worker_heartbeats(
    controller_url: str,
    *,
    expected_worker_ids: set[str],
    timeout_seconds: int = 300,
) -> bool:
    if not expected_worker_ids:
        return True
    deadline = time.monotonic() + timeout_seconds
    seen: set[str] = set()
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{controller_url}/status", timeout=5)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException:
            time.sleep(5)
            continue
        for heartbeat in payload.get("heartbeats", []):
            worker_id = heartbeat.get("worker_id")
            if worker_id in expected_worker_ids:
                seen.add(worker_id)
        if expected_worker_ids.issubset(seen):
            return True
        time.sleep(5)
    return False


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
