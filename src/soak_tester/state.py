from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
import fcntl
from typing import Any, Literal

from pydantic import BaseModel, Field

from soak_tester.util import atomic_write_json, read_json, utcnow_iso

RunStatus = Literal[
    "planned",
    "inspecting",
    "provisioning",
    "bootstrapping",
    "warming-up",
    "running",
    "degraded",
    "stopping",
    "cleaning-up",
    "completed",
    "failed",
    "left-running",
    "cleanup-failed",
]


class Timeline(BaseModel):
    started_at: str | None = None
    provisioning_started_at: str | None = None
    bootstrapping_started_at: str | None = None
    warmup_started_at: str | None = None
    steady_state_started_at: str | None = None
    steady_state_ended_at: str | None = None
    cooldown_started_at: str | None = None
    ended_at: str | None = None
    cleanup_started_at: str | None = None
    cleanup_ended_at: str | None = None


class RunState(BaseModel):
    run_id: str
    status: RunStatus = "planned"
    target_percent: float
    duration: str | None = None
    os_cloud: str | None = None
    project_name: str | None = None
    controller_url: str | None = None
    stop_requested: bool = False
    timeline: Timeline = Field(default_factory=Timeline)
    summary: dict[str, Any] = Field(default_factory=dict)


def initial_state(
    run_id: str, *, target_percent: float, os_cloud: str, duration: str | None
) -> RunState:
    return RunState(
        run_id=run_id,
        status="planned",
        target_percent=target_percent,
        duration=duration,
        os_cloud=os_cloud,
        timeline=Timeline(started_at=utcnow_iso()),
    )


def load_state(run_dir: Path) -> RunState:
    payload = read_json(run_dir / "state.json")
    return RunState.model_validate(payload)


def save_state(run_dir: Path, state: RunState) -> None:
    # A separate `stop` command must survive periodic status writes.
    # Serialize the merge and replacement independently of the whole-run lock,
    # which must remain held while another process requests stop.
    with (run_dir / "state.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        current = read_json(run_dir / "state.json", {}) or {}
        state.stop_requested = state.stop_requested or current.get("stop_requested", False)
        atomic_write_json(run_dir / "state.json", state.model_dump(mode="json"))


def update_status(run_dir: Path, status: RunStatus) -> RunState:
    state = load_state(run_dir)
    state.status = status
    save_state(run_dir, state)
    return state


@contextmanager
def run_lock(run_dir: Path):
    """Prevent the runner and manual cleanup from mutating the same run concurrently."""
    with (run_dir / "run.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Run is active; request stop and wait before cleanup") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
