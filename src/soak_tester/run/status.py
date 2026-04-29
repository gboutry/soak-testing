from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from soak_tester.state import load_state


def read_status(run_dir: Path) -> dict[str, Any]:
    state = load_state(run_dir)
    payload: dict[str, Any] = {"state": state.model_dump(mode="json"), "controller": None}
    if state.controller_url:
        try:
            response = requests.get(f"{state.controller_url}/status", timeout=5)
            response.raise_for_status()
            payload["controller"] = response.json()
        except requests.RequestException as exc:
            payload["controller_error"] = str(exc)
    return payload
