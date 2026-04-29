from __future__ import annotations

from soak_tester.state import RunState


def reconcile_once(state: RunState) -> RunState:
    if state.stop_requested and state.status not in {"stopping", "completed"}:
        state.status = "stopping"
    return state
