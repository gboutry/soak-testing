from __future__ import annotations

from pathlib import Path

from soak_tester.observability.correlate import correlation_report


def render_correlation_markdown(run_dir: Path) -> str:
    report = correlation_report(run_dir)
    return (
        "# Observability correlation\n\n"
        f"{report['conclusion']}\n\n"
        "See `prometheus-summary.json` and `final-report.json` for source data.\n"
    )
