from __future__ import annotations

from pathlib import Path
from typing import Any

from soak_tester.util import read_json


def correlation_report(run_dir: Path) -> dict[str, Any]:
    return {
        "final_report": read_json(run_dir / "final-report.json", default={}),
        "prometheus": read_json(run_dir / "observability" / "prometheus-summary.json", default={}),
        "conclusion": (
            "Prometheus data is infrastructure-side evidence from selected interfaces and "
            "does not prove exact physical fabric throughput."
        ),
    }
