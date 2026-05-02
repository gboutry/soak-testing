#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from soak_tester.network_tuning import (
    apply_config_patch,
    load_matrix,
    summarize_experiment_runs,
    write_matrix_summary,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a reproducible soak-tester network tuning matrix.")
    parser.add_argument("--os-cloud", required=True)
    parser.add_argument("--base-config", required=True, type=Path)
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--output-dir", default="network-tuning", type=Path)
    parser.add_argument("--duration", default="3m")
    parser.add_argument("--target", default="100")
    parser.add_argument("--repetitions", default=3, type=int)
    parser.add_argument("--command", default="soak-tester")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    base = yaml.safe_load(args.base_config.read_text(encoding="utf-8")) or {}
    experiments = load_matrix(args.matrix)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    config_dir = args.output_dir / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    baseline_mean = None

    for experiment in experiments:
        name = str(experiment["name"])
        patch = experiment.get("patch") or {}
        config_payload = apply_config_patch(copy.deepcopy(base), patch)
        config_path = config_dir / f"{name}.yaml"
        config_path.write_text(yaml.safe_dump(config_payload, sort_keys=False), encoding="utf-8")
        run_dirs: list[Path] = []
        for repetition in range(args.repetitions):
            before = _run_dirs()
            command = [
                *shlex.split(args.command),
                "calibrate",
                "network",
                "--os-cloud",
                args.os_cloud,
                "--config",
                str(config_path),
                "--duration",
                args.duration,
                "--target",
                str(args.target),
            ]
            print(f"[{name} #{repetition + 1}] {' '.join(command)}", flush=True)
            if args.dry_run:
                continue
            env = os.environ.copy()
            env.setdefault("OS_CLOUD", args.os_cloud)
            subprocess.run(command, check=True, env=env)
            new_runs = sorted(_run_dirs() - before, key=lambda path: path.stat().st_mtime)
            if not new_runs:
                raise RuntimeError(f"no run directory created for {name} repetition {repetition + 1}")
            run_dirs.append(new_runs[-1])
        if args.dry_run:
            continue
        summary = summarize_experiment_runs(name, run_dirs, baseline_gbps=baseline_mean)
        if baseline_mean is None:
            baseline_mean = float(summary["goodput_gbps"]["mean"])
        summaries.append(summary)
        write_matrix_summary(args.output_dir / "network-tuning-results.json", summaries)

    if not args.dry_run:
        write_matrix_summary(args.output_dir / "network-tuning-results.json", summaries)
    return 0


def _run_dirs() -> set[Path]:
    root = Path("runs")
    if not root.exists():
        return set()
    return {path for path in root.glob("soak-*") if path.is_dir()}


if __name__ == "__main__":
    sys.exit(main())
