from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from soak_tester.util import ensure_dir, utcnow_iso


class EventWriter:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        ensure_dir(path.parent)

    def write(
        self,
        *,
        level: str,
        plane: str,
        event: str,
        worker_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "ts": utcnow_iso(),
            "run_id": self.run_id,
            "level": level,
            "plane": plane,
            "worker_id": worker_id,
            "event": event,
            "details": details or {},
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")


class MetricWriter:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        ensure_dir(path.parent)

    def write(
        self,
        *,
        plane: str,
        worker_id: str,
        metric: str,
        value: float,
        unit: str,
        tags: dict[str, str] | None = None,
    ) -> None:
        payload = {
            "ts": utcnow_iso(),
            "run_id": self.run_id,
            "plane": plane,
            "worker_id": worker_id,
            "metric": metric,
            "value": value,
            "unit": unit,
            "tags": tags or {},
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")
