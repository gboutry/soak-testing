from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from soak_tester.events import EventWriter, MetricWriter
from soak_tester.util import ensure_dir


@dataclass
class RallyScenarioSummary:
    attempts: int = 0
    successes: int = 0
    errors: int = 0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_p99_ms: float = 0.0


@dataclass
class RallyApiSummary:
    total_attempts: int = 0
    successes: int = 0
    errors: int = 0
    total_ops_per_second: float = 0.0
    success_ops_per_second: float = 0.0
    error_rate: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_p99_ms: float = 0.0
    scenarios: dict[str, RallyScenarioSummary] = field(default_factory=dict)
    atomic_actions: dict[str, RallyScenarioSummary] = field(default_factory=dict)


def require_rally() -> None:
    missing = [name for name in ("rally",) if shutil.which(name) is None]
    if missing:
        raise RuntimeError(
            "Rally API workload requires the 'rally' command. Install Rally and "
            "rally-openstack on the soak-tester runner host."
        )


def render_rally_task(
    *,
    duration_seconds: int,
    concurrency: int,
    times: int | None = None,
    volume_size_gib: int = 1,
    include_create_delete: bool = True,
    error_rate_max: float = 0.0,
    scenario_names: list[str] | None = None,
) -> str:
    scenario_names = scenario_names or [
        "Authenticate.validate_nova",
        "Authenticate.validate_neutron",
        "Authenticate.validate_cinder",
        "NovaServers.list_servers",
        "CinderVolumes.list_volumes",
    ]
    runner: dict[str, Any]
    if duration_seconds > 0:
        runner = {
            "type": "constant_for_duration",
            "duration": duration_seconds,
            "concurrency": concurrency,
        }
    elif times:
        runner = {"type": "constant", "times": times, "concurrency": concurrency}
    else:
        runner = {"type": "constant", "times": concurrency, "concurrency": concurrency}
    common = {
        "runner": runner,
        "sla": {"failure_rate": {"max": error_rate_max}},
    }
    all_scenarios: dict[str, list[dict[str, Any]]] = {
        "Authenticate.validate_nova": [{**common, "args": {"repetitions": 1}}],
        "Authenticate.validate_neutron": [{**common, "args": {"repetitions": 1}}],
        "Authenticate.validate_cinder": [{**common, "args": {"repetitions": 1}}],
        "NovaServers.list_servers": [{**common, "args": {"detailed": True}}],
        "CinderVolumes.list_volumes": [{**common, "args": {"detailed": True}}],
    }
    scenarios = {name: all_scenarios[name] for name in scenario_names}
    return yaml.safe_dump(scenarios, sort_keys=False)


def parse_rally_results(payload: str | list[dict[str, Any]] | dict[str, Any], *, elapsed_seconds: float) -> RallyApiSummary:
    data = json.loads(payload) if isinstance(payload, str) else payload
    if isinstance(data, dict) and "tasks" in data:
        return _parse_rally_report(data, elapsed_seconds=elapsed_seconds)
    summary = RallyApiSummary()
    all_latencies: list[float] = []
    scenario_latencies: dict[str, list[float]] = {}
    scenario_errors: dict[str, int] = {}
    atomic_latencies: dict[str, list[float]] = {}
    atomic_errors: dict[str, int] = {}
    for scenario in data:
        name = _scenario_name(scenario)
        results = scenario.get("result") or scenario.get("results") or []
        for item in results:
            duration_ms = float(item.get("duration") or item.get("duration_ms", 0))
            if duration_ms < 1000:
                duration_ms *= 1000
            failed = bool(item.get("error"))
            summary.total_attempts += 1
            scenario_latencies.setdefault(name, []).append(duration_ms)
            all_latencies.append(duration_ms)
            if failed:
                summary.errors += 1
                scenario_errors[name] = scenario_errors.get(name, 0) + 1
            else:
                summary.successes += 1
            for action, value in (item.get("atomic_actions") or {}).items():
                latency_ms = float(value)
                if latency_ms < 1000:
                    latency_ms *= 1000
                atomic_latencies.setdefault(action, []).append(latency_ms)
                if failed:
                    atomic_errors[action] = atomic_errors.get(action, 0) + 1
    elapsed = max(elapsed_seconds, 0.001)
    summary.total_ops_per_second = summary.total_attempts / elapsed
    summary.success_ops_per_second = summary.successes / elapsed
    summary.error_rate = summary.errors / max(summary.total_attempts, 1)
    summary.latency_p50_ms = percentile(all_latencies, 50)
    summary.latency_p95_ms = percentile(all_latencies, 95)
    summary.latency_p99_ms = percentile(all_latencies, 99)
    for name, latencies in scenario_latencies.items():
        errors = scenario_errors.get(name, 0)
        summary.scenarios[name] = RallyScenarioSummary(
            attempts=len(latencies),
            successes=len(latencies) - errors,
            errors=errors,
            latency_p50_ms=percentile(latencies, 50),
            latency_p95_ms=percentile(latencies, 95),
            latency_p99_ms=percentile(latencies, 99),
        )
    for name, latencies in atomic_latencies.items():
        errors = atomic_errors.get(name, 0)
        summary.atomic_actions[name] = RallyScenarioSummary(
            attempts=len(latencies),
            successes=len(latencies) - errors,
            errors=errors,
            latency_p50_ms=percentile(latencies, 50),
            latency_p95_ms=percentile(latencies, 95),
            latency_p99_ms=percentile(latencies, 99),
        )
    return summary


def run_rally_api_workload(
    *,
    run_dir: Path,
    auth: dict[str, Any],
    duration_seconds: int,
    concurrency: int,
    times: int | None,
    volume_size_gib: int,
    include_create_delete: bool,
    error_rate_max: float,
    mode: str = "saturation",
    metric_writer: MetricWriter,
    event_writer: EventWriter,
    command_runner: Any = subprocess.run,
) -> RallyApiSummary:
    require_rally()
    rally_dir = ensure_dir(run_dir / "api-rally")
    task_path = rally_dir / "task.yaml"
    deployment_path = rally_dir / "deployment.json"
    results_path = rally_dir / "results.json"
    summary_path = rally_dir / "summary.json"
    log_path = rally_dir / "rally.log"
    task_payload = render_rally_task(
        duration_seconds=duration_seconds,
        concurrency=concurrency,
        times=times,
        volume_size_gib=volume_size_gib,
        include_create_delete=include_create_delete,
        error_rate_max=error_rate_max,
    )
    task_path.write_text(task_payload, encoding="utf-8")
    deployment_name = f"soak-tester-{run_dir.name}"
    deployment_path.write_text(_deployment_json(auth, redact=True), encoding="utf-8")
    deployment_path.chmod(0o600)
    event_writer.write(level="info", plane="api", event="rally_started")
    env = _rally_env(auth)
    start = time.monotonic()
    task_returncode = 0
    try:
        if mode == "saturation":
            result_paths, task_returncode = _run_saturation_tasks(
                command_runner=command_runner,
                rally_dir=rally_dir,
                deployment_name=deployment_name,
                env=env,
                duration_seconds=duration_seconds,
                concurrency=concurrency,
                times=times,
                volume_size_gib=volume_size_gib,
                include_create_delete=include_create_delete,
                error_rate_max=error_rate_max,
            )
            _combine_rally_reports(result_paths, results_path)
        else:
            with log_path.open("a", encoding="utf-8") as log:
                _run_rally_command(
                    command_runner,
                    [
                        "rally",
                        "deployment",
                        "create",
                        "--fromenv",
                        "--name",
                        deployment_name,
                    ],
                    env=env,
                    log=log,
                )
                completed = command_runner(
                    ["rally", "task", "start", "--deployment", deployment_name, "--task", str(task_path)],
                    check=False,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                task_returncode = int(getattr(completed, "returncode", 0) or 0)
                task_uuid = _latest_rally_task_uuid(command_runner, deployment_name, env=env)
                command_runner(
                    ["rally", "task", "report", "--uuid", task_uuid, "--json", "--out", str(results_path)],
                    check=False,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
    finally:
        if mode != "saturation":
            with log_path.open("a", encoding="utf-8") as log:
                command_runner(
                    ["rally", "deployment", "destroy", "--deployment", deployment_name],
                    check=False,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
    elapsed = time.monotonic() - start
    if not results_path.exists():
        raise RuntimeError(f"Rally task failed with return code {task_returncode}; no results were exported")
    result_payload = results_path.read_text(encoding="utf-8")
    summary = parse_rally_results(result_payload, elapsed_seconds=elapsed)
    if mode == "saturation" and summary.total_attempts == 0:
        raise RuntimeError("Rally saturation mode produced no API iterations")
    summary_path.write_text(json.dumps(_summary_to_dict(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_rally_metrics(summary, metric_writer)
    event_writer.write(
        level="info",
        plane="api",
        event="rally_completed",
        details=_summary_to_dict(summary),
    )
    return summary


def _run_saturation_tasks(
    *,
    command_runner: Any,
    rally_dir: Path,
    deployment_name: str,
    env: dict[str, str],
    duration_seconds: int,
    concurrency: int,
    times: int | None,
    volume_size_gib: int,
    include_create_delete: bool,
    error_rate_max: float,
) -> tuple[list[Path], int]:
    scenarios = [
        "Authenticate.validate_nova",
        "Authenticate.validate_neutron",
        "Authenticate.validate_cinder",
        "NovaServers.list_servers",
        "CinderVolumes.list_volumes",
    ]
    result_paths: list[Path] = []
    returncodes: list[int] = []
    with ThreadPoolExecutor(max_workers=len(scenarios)) as executor:
        futures = [
            executor.submit(
                _run_saturation_scenario,
                command_runner=command_runner,
                rally_dir=rally_dir,
                deployment_name=deployment_name,
                env=env,
                scenario=scenario,
                duration_seconds=duration_seconds,
                concurrency=concurrency,
                times=times,
                volume_size_gib=volume_size_gib,
                include_create_delete=include_create_delete,
                error_rate_max=error_rate_max,
            )
            for scenario in scenarios
        ]
        for future in as_completed(futures):
            result_path, returncode = future.result()
            result_paths.append(result_path)
            returncodes.append(returncode)
    return sorted(result_paths), max(returncodes, default=0)


def _run_saturation_scenario(
    *,
    command_runner: Any,
    rally_dir: Path,
    deployment_name: str,
    env: dict[str, str],
    scenario: str,
    duration_seconds: int,
    concurrency: int,
    times: int | None,
    volume_size_gib: int,
    include_create_delete: bool,
    error_rate_max: float,
) -> tuple[Path, int]:
    slug = _slug(scenario)
    task_path = rally_dir / f"task-{slug}.yaml"
    log_path = rally_dir / f"rally-{slug}.log"
    results_path = rally_dir / f"results-{slug}.json"
    db_path = rally_dir / f"rally-{slug}.sqlite"
    config_path = rally_dir / f"rally-{slug}.conf"
    deployment_name = f"{deployment_name}-{slug}"
    config_path.write_text(
        f"[database]\nconnection = sqlite:///{db_path}\n",
        encoding="utf-8",
    )
    task_path.write_text(
        render_rally_task(
            duration_seconds=duration_seconds,
            concurrency=concurrency,
            times=times,
            volume_size_gib=volume_size_gib,
            include_create_delete=include_create_delete,
            error_rate_max=error_rate_max,
            scenario_names=[scenario],
        ),
        encoding="utf-8",
    )
    with log_path.open("a", encoding="utf-8") as log:
        _run_rally_command(
            command_runner,
            ["rally", "--config-file", str(config_path), "db", "create"],
            env=env,
            log=log,
        )
        _run_rally_command(
            command_runner,
            [
                "rally",
                "--config-file",
                str(config_path),
                "deployment",
                "create",
                "--fromenv",
                "--name",
                deployment_name,
            ],
            env=env,
            log=log,
        )
        completed = command_runner(
            [
                "rally",
                "--config-file",
                str(config_path),
                "task",
                "start",
                "--deployment",
                deployment_name,
                "--task",
                str(task_path),
            ],
            check=False,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        returncode = int(getattr(completed, "returncode", 0) or 0)
        task_uuid = _task_uuid_from_log(log_path)
        command_runner(
            [
                "rally",
                "--config-file",
                str(config_path),
                "task",
                "report",
                "--uuid",
                task_uuid,
                "--json",
                "--out",
                str(results_path),
            ],
            check=False,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        command_runner(
            [
                "rally",
                "--config-file",
                str(config_path),
                "deployment",
                "destroy",
                "--deployment",
                deployment_name,
            ],
            check=False,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    return results_path, returncode


def _combine_rally_reports(result_paths: list[Path], output_path: Path) -> None:
    combined: dict[str, Any] = {"tasks": []}
    for path in result_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        combined["tasks"].extend(payload.get("tasks") or [])
    output_path.write_text(json.dumps(combined, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _task_uuid_from_log(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    matches = re.findall(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        text,
    )
    if not matches:
        raise RuntimeError(f"Rally did not report a task UUID in {path}")
    return matches[-1]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _parse_rally_report(data: dict[str, Any], *, elapsed_seconds: float) -> RallyApiSummary:
    summary = RallyApiSummary()
    scenario_latencies: list[float] = []
    for task in data.get("tasks", []):
        for subtask in task.get("subtasks", []):
            for workload in subtask.get("workloads", []):
                scenario_name = _report_workload_name(workload)
                attempts = int(workload.get("total_iteration_count") or 0)
                errors = int(workload.get("failed_iteration_count") or 0)
                successes = max(0, attempts - errors)
                total_stats = ((workload.get("statistics") or {}).get("durations") or {}).get("total") or {}
                total_data = total_stats.get("data") or {}
                scenario = RallyScenarioSummary(
                    attempts=attempts,
                    successes=successes,
                    errors=errors,
                    latency_p50_ms=float(total_data.get("median") or 0) * 1000,
                    latency_p95_ms=float(total_data.get("95%ile") or 0) * 1000,
                    latency_p99_ms=float(total_data.get("99%ile") or total_data.get("max") or 0) * 1000,
                )
                summary.scenarios[scenario_name] = scenario
                summary.total_attempts += attempts
                summary.successes += successes
                summary.errors += errors
                if scenario.latency_p50_ms:
                    scenario_latencies.append(scenario.latency_p50_ms)
                for atomic in (((workload.get("statistics") or {}).get("durations") or {}).get("atomics") or []):
                    atomic_name = str(atomic.get("name") or atomic.get("display_name") or "unknown")
                    atomic_data = atomic.get("data") or {}
                    summary.atomic_actions[atomic_name] = RallyScenarioSummary(
                        attempts=int(atomic_data.get("iteration_count") or attempts),
                        successes=successes,
                        errors=errors,
                        latency_p50_ms=float(atomic_data.get("median") or 0) * 1000,
                        latency_p95_ms=float(atomic_data.get("95%ile") or 0) * 1000,
                        latency_p99_ms=float(atomic_data.get("99%ile") or atomic_data.get("max") or 0) * 1000,
                    )
    elapsed = max(elapsed_seconds, 0.001)
    summary.total_ops_per_second = summary.total_attempts / elapsed
    summary.success_ops_per_second = summary.successes / elapsed
    summary.error_rate = summary.errors / max(summary.total_attempts, 1)
    scenario_items = list(summary.scenarios.values())
    summary.latency_p50_ms = percentile([item.latency_p50_ms for item in scenario_items], 50)
    summary.latency_p95_ms = percentile([item.latency_p95_ms for item in scenario_items], 95)
    summary.latency_p99_ms = percentile([item.latency_p99_ms for item in scenario_items], 99)
    return summary


def _report_workload_name(workload: dict[str, Any]) -> str:
    scenario = workload.get("scenario") or {}
    if isinstance(scenario, dict) and scenario:
        return str(next(iter(scenario)))
    return str(workload.get("name") or workload.get("uuid") or "unknown")


def write_rally_metrics(summary: RallyApiSummary, writer: MetricWriter) -> None:
    _metric(writer, "api.rally.total_ops_per_second", summary.total_ops_per_second, "ops/s")
    _metric(writer, "api.rally.success_ops_per_second", summary.success_ops_per_second, "ops/s")
    _metric(writer, "api.rally.errors", summary.errors, "count")
    _metric(writer, "api.rally.error_rate", summary.error_rate, "ratio")
    _metric(writer, "api.rally.latency_p50_ms", summary.latency_p50_ms, "ms")
    _metric(writer, "api.rally.latency_p95_ms", summary.latency_p95_ms, "ms")
    _metric(writer, "api.rally.latency_p99_ms", summary.latency_p99_ms, "ms")
    for scenario, item in sorted(summary.scenarios.items()):
        tags = {"scenario": scenario}
        _metric(writer, "api.rally.scenario.success_ops", item.successes, "count", tags)
        _metric(writer, "api.rally.scenario.errors", item.errors, "count", tags)
        _metric(writer, "api.rally.scenario.latency_p95_ms", item.latency_p95_ms, "ms", tags)
    for action, item in sorted(summary.atomic_actions.items()):
        tags = {"atomic_action": action}
        _metric(writer, "api.rally.atomic.latency_p95_ms", item.latency_p95_ms, "ms", tags)


def percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(__import__("math").ceil((pct / 100) * len(ordered))) - 1))
    return float(ordered[index])


def _metric(
    writer: MetricWriter,
    name: str,
    value: float,
    unit: str,
    tags: dict[str, str] | None = None,
) -> None:
    writer.write(
        plane="api",
        worker_id="rally",
        metric=name,
        value=float(value),
        unit=unit,
        tags=tags or {},
    )


def _run_rally_command(command_runner: Any, cmd: list[str], *, env: dict[str, str], log: Any) -> None:
    command_runner(
        cmd,
        check=True,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _latest_rally_task_uuid(command_runner: Any, deployment_name: str, *, env: dict[str, str]) -> str:
    completed = command_runner(
        ["rally", "task", "list", "--deployment", deployment_name, "--uuids-only"],
        check=True,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    task_ids = [line.strip() for line in str(completed.stdout).splitlines() if line.strip()]
    if not task_ids:
        raise RuntimeError(f"Rally did not report a task UUID for deployment {deployment_name}")
    return task_ids[-1]


def _scenario_name(scenario: dict[str, Any]) -> str:
    key = scenario.get("key") or {}
    if isinstance(key, dict):
        return str(key.get("name") or key.get("kw") or "unknown")
    return str(scenario.get("name") or key or "unknown")


def _rally_env(auth: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "OS_AUTH_URL": str(auth["auth_url"]),
            "OS_USERNAME": str(auth["username"]),
            "OS_PASSWORD": str(auth["password"]),
            "OS_PROJECT_NAME": str(auth["project_name"]),
            "OS_USER_DOMAIN_NAME": str(auth.get("user_domain_name", "Default")),
            "OS_PROJECT_DOMAIN_NAME": str(auth.get("project_domain_name", "Default")),
            "OS_IDENTITY_API_VERSION": "3",
        }
    )
    if auth.get("region_name"):
        env["OS_REGION_NAME"] = str(auth["region_name"])
    if auth.get("interface"):
        env["OS_INTERFACE"] = str(auth["interface"])
    return env


def _deployment_json(auth: dict[str, Any], *, redact: bool = False) -> str:
    payload = {
        "type": "ExistingCloud",
        "auth_url": auth["auth_url"],
        "region_name": auth.get("region_name"),
        "endpoint_type": auth.get("interface") or "public",
        "admin": {
            "username": auth["username"],
            "password": "<redacted>" if redact else auth["password"],
            "project_name": auth["project_name"],
            "user_domain_name": auth.get("user_domain_name", "Default"),
            "project_domain_name": auth.get("project_domain_name", "Default"),
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _summary_to_dict(summary: RallyApiSummary) -> dict[str, Any]:
    return {
        "total_attempts": summary.total_attempts,
        "successes": summary.successes,
        "errors": summary.errors,
        "total_ops_per_second": summary.total_ops_per_second,
        "success_ops_per_second": summary.success_ops_per_second,
        "error_rate": summary.error_rate,
        "latency_p50_ms": summary.latency_p50_ms,
        "latency_p95_ms": summary.latency_p95_ms,
        "latency_p99_ms": summary.latency_p99_ms,
        "scenarios": {name: vars(item) for name, item in summary.scenarios.items()},
        "atomic_actions": {name: vars(item) for name, item in summary.atomic_actions.items()},
    }
