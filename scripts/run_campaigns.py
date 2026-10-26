#!/usr/bin/env python3
"""Run sequential native soak campaigns and record dashboard time windows."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import signal
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from soak_tester.cli import RALLY_STOP_TIMEOUT_SECONDS
from soak_tester.cleanup import remaining_resources as inspect_remaining
from soak_tester.evidence import assess_run
from soak_tester.config import CampaignConfig, load_config
from soak_tester.openstack_client.connection import admin_connection
from soak_tester.util import atomic_write_json, duration_seconds


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_event(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"ts": timestamp(), **payload}) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def validate_evidence(run_dir: Path, config_path: Path) -> list[str]:
    return assess_run(run_dir, config_path)["errors"]


def remaining_resources(conn, run_dir: Path) -> dict[str, list[str]]:
    result = inspect_remaining(conn, run_dir)
    if result["errors"]:
        raise RuntimeError("; ".join(result["errors"]))
    return result["remaining"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--command", default="soak-tester")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    sequence = CampaignConfig.model_validate(yaml.safe_load(args.campaign.read_text())).model_dump(
        mode="json"
    )
    for campaign in sequence["campaigns"]:
        cfg = load_config(campaign["config"])
        if cfg.run.keep_resources:
            raise ValueError("Sequential campaigns require run.keep_resources: false")
    zone = ZoneInfo(sequence["timezone"])
    deadline = datetime.fromisoformat(sequence["latest_finish"])
    if deadline.tzinfo is None:
        raise ValueError("latest_finish must include a timezone offset")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "campaign.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return execute(args, sequence, zone, deadline)


def _stop_campaign_process(process) -> None:
    process.terminate()
    try:
        process.wait(timeout=RALLY_STOP_TIMEOUT_SECONDS + 20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=30)


def execute(args, sequence, zone, deadline) -> int:
    journal = args.output_dir / "campaign-events.jsonl"
    if journal.exists() or (args.output_dir / "campaign-results.json").exists():
        raise FileExistsError(
            "Refusing to overwrite existing campaign evidence; choose a new output directory"
        )
    results = []
    command_prefix = shlex.split(args.command)
    conn = None if args.dry_run else admin_connection(sequence["os_cloud"])
    for campaign in sequence["campaigns"]:
        now = datetime.now(timezone.utc)
        # Leave ten minutes for cleanup, plus requested measurement time.
        if (
            not args.dry_run
            and (deadline - now).total_seconds() < duration_seconds(campaign["duration"]) + 600
        ):
            append_event(
                journal, {"campaign": campaign["name"], "event": "deadline_admission_blocked"}
            )
            return 1
        config_path = Path(campaign["config"])
        load_config(config_path)
        command = [
            *command_prefix,
            "run",
            "--os-cloud",
            sequence["os_cloud"],
            "--config",
            str(config_path),
            "--target",
            str(sequence["target"]),
            "--duration",
            campaign["duration"],
        ]
        if args.dry_run:
            print(shlex.join(command))
            continue
        before = set(Path("runs").glob("soak-*"))
        started = timestamp()
        append_event(
            journal,
            {
                "campaign": campaign["name"],
                "event": "campaign_started",
                "started_at": started,
                "command": command,
            },
        )
        log_path = args.output_dir / f"{campaign['name']}.log"
        errors = []
        try:
            with log_path.open("w") as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
                try:
                    returncode = process.wait(
                        timeout=max(1, (deadline - now).total_seconds() - 600)
                    )
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    _stop_campaign_process(process)
                    returncode = 124
                    errors.append("Campaign interrupted or exceeded deadline reserved for cleanup")
        except OSError as exc:
            returncode = 127
            errors.append(str(exc))
        created = set(Path("runs").glob("soak-*")) - before
        run_dir = next(iter(created)) if len(created) == 1 else None
        timeline = {}
        remaining = {}
        if run_dir is None:
            errors.append(f"Expected exactly one new run, found {len(created)}")
        else:
            try:
                if returncode == 0:
                    errors.extend(validate_evidence(run_dir, config_path))
                if (run_dir / "resources.json").exists():
                    remaining = remaining_resources(conn, run_dir)
                    if any(remaining.values()):
                        subprocess.run(
                            shlex.split(args.command) + ["cleanup", "--run", str(run_dir)],
                            check=True,
                        )
                        remaining = remaining_resources(conn, run_dir)
                    if any(remaining.values()):
                        errors.append(
                            "Owned resources remain after cleanup; refusing next campaign"
                        )
                timeline = json.loads((run_dir / "state.json").read_text()).get("timeline", {})
            except Exception as exc:
                errors.append(f"Evidence or restoration check failed: {exc}")
                if (run_dir / "resources.json").exists():
                    try:
                        subprocess.run(
                            shlex.split(args.command) + ["cleanup", "--run", str(run_dir)],
                            check=True,
                        )
                        remaining = remaining_resources(conn, run_dir)
                    except Exception as cleanup_exc:
                        errors.append(f"Cleanup verification failed: {cleanup_exc}")
        ended = timestamp()
        result = {
            "campaign": campaign["name"],
            "started_at": started,
            "ended_at": ended,
            "started_local": datetime.fromisoformat(started).astimezone(zone).isoformat(),
            "ended_local": datetime.fromisoformat(ended).astimezone(zone).isoformat(),
            "native_exit_code": returncode,
            "run_dir": str(run_dir) if run_dir else None,
            "timeline": timeline,
            "errors": errors,
            "remaining_resources": remaining,
            "result": "VALID_MEASUREMENT" if returncode == 0 and not errors else "FAIL",
        }
        results.append(result)
        atomic_write_json(args.output_dir / "campaign-results.json", {"campaigns": results})
        append_event(journal, {**result, "event": "campaign_ended"})
        print(json.dumps(result), flush=True)
        if result["result"] != "VALID_MEASUREMENT":
            return 1
    return 0


if __name__ == "__main__":

    def stop_campaign(*_):
        raise KeyboardInterrupt("Campaign stop requested")

    signal.signal(signal.SIGTERM, stop_campaign)
    raise SystemExit(main())
