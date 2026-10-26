from __future__ import annotations

import json
import hashlib
import threading
import os
import signal
import subprocess
import sys
from importlib.metadata import version
from contextlib import contextmanager
import time
from pathlib import Path
from typing import Annotated

import requests
import typer
from rich.console import Console
from rich.table import Table

from soak_tester.cleanup import cleanup_run
from soak_tester.config import dump_config, load_config
from soak_tester.evidence import assess_run
from soak_tester.events import EventWriter, MetricWriter
from soak_tester.openstack_client.compute import (
    list_hypervisor_names,
    require_flavor,
    placement_record,
)
from soak_tester.openstack_client.connection import admin_connection
from soak_tester.openstack_client.identity import get_role
from soak_tester.openstack_client.image import require_image
from soak_tester.openstack_client.network import require_external_network
from soak_tester.openstack_client.quota import read_quotas
from soak_tester.openstack_client.volume import require_volume_type
from soak_tester.observability.collect import collect_prometheus, inspect_prometheus
from soak_tester.observability.correlate import correlation_report
from soak_tester.planner import create_plan, write_plan
from soak_tester.report import generate_final_report
from soak_tester.run.collect import (
    collect_controller_artifacts,
    collect_failure_console_outputs,
    wait_for_controller,
    wait_for_worker_heartbeats,
)
from soak_tester.run.provision import provision_openstack_run
from soak_tester.run.status import read_status
from soak_tester.state import initial_state, load_state, save_state, run_lock
from soak_tester.util import (
    atomic_write_json,
    duration_seconds,
    ensure_dir,
    make_run_id,
    read_json,
    utcnow_iso,
)
from soak_tester.workloads.api import ApiWorkload
from soak_tester.workloads.api_rally import run_rally_api_workload, require_rally

console = Console()
app = typer.Typer(help="OpenStack-native soak-load testing tool.")
observability_app = typer.Typer(help="Prometheus evidence commands.")
app.add_typer(observability_app, name="observability")


def main() -> None:
    app()


@app.command()
def inspect(
    os_cloud: Annotated[str, typer.Option("--os-cloud")],
    config: Annotated[Path, typer.Option("--config", exists=True)],
) -> None:
    """Validate admin OpenStack access and configured prerequisites."""
    cfg = load_config(config)
    conn = admin_connection(os_cloud)
    checks: list[tuple[str, str]] = []
    conn.authorize()
    checks.append(("admin credentials", "ok"))
    get_role(conn, cfg.openstack.member_role)
    checks.append(("member role", "ok"))
    require_image(conn, cfg.openstack.image)
    checks.append(("image", "ok"))
    for flavor in sorted(_required_flavor_names(cfg)):
        require_flavor(conn, flavor)
        checks.append((f"flavor {flavor}", "ok"))
    if cfg.resources.create_router:
        require_external_network(conn, cfg.openstack.external_network)
        checks.append(("external router network", "ok"))
    if cfg.workloads.storage.enabled:
        require_volume_type(conn, cfg.workloads.storage.volume_type)
        checks.append(("volume type", "ok"))
    quotas = read_quotas(conn)
    checks.append(("quotas readable", "ok" if quotas else "unavailable"))
    _print_checks(checks)


@app.command()
def plan(
    os_cloud: Annotated[str, typer.Option("--os-cloud")],
    config: Annotated[Path, typer.Option("--config", exists=True)],
    target: Annotated[float, typer.Option("--target", min=1, max=100)],
) -> None:
    """Generate a run plan artifact without provisioning resources."""
    run_dir = _create_run_dir()
    cfg = load_config(config)
    state = initial_state(run_dir.name, target_percent=target, os_cloud=os_cloud, duration=None)
    save_state(run_dir, state)
    conn = admin_connection(os_cloud)
    _prepare_openstack_context(conn, cfg)
    flavor_specs = {name: require_flavor(conn, name) for name in _required_flavor_names(cfg)}
    soak_plan = create_plan(
        cfg, target_percent=target, flavor_specs=flavor_specs, run_id=run_dir.name
    )
    _write_run_config(run_dir, config, cfg)
    write_plan(run_dir / "plan.yaml", soak_plan)
    EventWriter(run_dir / "events.jsonl", run_dir.name).write(
        level="info", plane="control", event="plan_created"
    )
    (run_dir / "metrics.jsonl").touch()
    console.print(f"Plan written to {run_dir / 'plan.yaml'}")


@app.command()
def run(
    os_cloud: Annotated[str, typer.Option("--os-cloud")],
    config: Annotated[Path, typer.Option("--config", exists=True)],
    target: Annotated[float, typer.Option("--target", min=1, max=100)],
    duration: Annotated[str, typer.Option("--duration")],
) -> None:
    """Plan, provision, measure and finalize one owned OpenStack run."""
    cfg = load_config(config)
    duration_s = duration_seconds(duration)
    if duration_s <= 0:
        raise typer.BadParameter("duration must be positive")
    if cfg.workloads.network.enabled and duration_s <= cfg.workloads.network.omit_seconds + 2:
        raise typer.BadParameter("network duration must exceed omit_seconds + 2")
    run_dir = _create_run_dir()
    state = initial_state(run_dir.name, target_percent=target, os_cloud=os_cloud, duration=duration)
    state.project_name = f"{cfg.resources.project_prefix}-{run_dir.name}"
    save_state(run_dir, state)
    _write_run_config(run_dir, config, cfg)
    events = EventWriter(run_dir / "events.jsonl", run_dir.name)
    metrics = MetricWriter(run_dir / "metrics.jsonl", run_dir.name)
    atomic_write_json(
        run_dir / "runtime.json",
        {
            "python": sys.version,
            "source_sha256": _source_digest(),
            "packages": {
                name: version(name) for name in ["soak-tester", "openstacksdk", "pydantic"]
            },
        },
    )
    with run_lock(run_dir), _stop_signals() as interrupted:
        _execute_run(run_dir, state, cfg, duration_s, events, metrics, interrupted)
    console.print(f"Run artifacts written to {run_dir}")


@contextmanager
def _stop_signals():
    stopped = threading.Event()
    previous = {sig: signal.getsignal(sig) for sig in [signal.SIGINT, signal.SIGTERM]}
    for sig in previous:
        signal.signal(sig, lambda *_: stopped.set())
    try:
        yield stopped
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _execute_run(run_dir, state, cfg, duration_s, events, metrics, interrupted):
    conn = None
    failure = None

    def stop_requested():
        return interrupted.is_set() or load_state(run_dir).stop_requested

    try:
        conn = admin_connection(state.os_cloud)
        _prepare_openstack_context(conn, cfg)
        flavors = _preflight(conn, cfg, run_dir=run_dir)
        atomic_write_json(
            run_dir / "flavors.json",
            {name: spec.model_dump(mode="json") for name, spec in flavors.items()},
        )
        soak_plan = create_plan(
            cfg, target_percent=state.target_percent, flavor_specs=flavors, run_id=state.run_id
        )
        _write_run_config(run_dir, run_dir / "config.yaml", cfg)
        write_plan(run_dir / "plan.yaml", soak_plan)
        if stop_requested():
            raise InterruptedError("Stop requested before provisioning")
        state.status = "provisioning"
        state.timeline.provisioning_started_at = utcnow_iso()
        save_state(run_dir, state)
        provision_result = provision_openstack_run(
            run_dir=run_dir,
            run_id=state.run_id,
            project_name=state.project_name,
            config=cfg,
            plan=soak_plan,
            admin_conn=conn,
            duration=state.duration,
            stop_requested=stop_requested,
        )
        state.controller_url = provision_result["controller_url"]
        placement = read_json(run_dir / "placement.json", [])
        for index, item in enumerate(placement):
            server = conn.compute.get_server(item["server_id"])
            placement[index] = placement_record(
                item["worker_id"], server, plane=item["plane"], role=item["role"]
            )
        atomic_write_json(run_dir / "placement.json", placement)
        save_state(run_dir, state)
        if not state.controller_url or not wait_for_controller(
            state.controller_url,
            timeout_seconds=120,
            diagnostics_path=run_dir / "readiness-controller.jsonl",
            stop_requested=stop_requested,
        ):
            raise RuntimeError("Controller did not become ready within deadline")
        if not wait_for_worker_heartbeats(
            state.controller_url,
            expected_worker_ids=_expected_worker_ids(soak_plan),
            timeout_seconds=300,
            diagnostics_path=run_dir / "readiness-workers.jsonl",
            stop_requested=stop_requested,
        ):
            raise RuntimeError("Not all workers reported ready within deadline")
        if stop_requested():
            raise InterruptedError("Stop requested during provisioning")
        state.status = "running"
        state.timeline.steady_state_started_at = utcnow_iso()
        save_state(run_dir, state)
        events.write(level="info", plane="control", event="run_started")
        response = requests.post(
            state.controller_url + "/control",
            json={"duration_seconds": duration_s},
            headers={"authorization": "Bearer " + (run_dir / "controller-token").read_text()},
            timeout=10,
        )
        response.raise_for_status()
        state.summary["guest_workload_window"] = response.json()
        save_state(run_dir, state)
        if cfg.workloads.api.enabled and cfg.workloads.api.engine == "rally":
            _run_rally_api_with_periodic_collection(
                run_dir=run_dir,
                state=state,
                cfg=cfg,
                duration_s=duration_s,
                metrics=metrics,
                events=events,
                stop_requested=stop_requested,
            )
        elif cfg.workloads.api.enabled:
            workload = ApiWorkload(
                conn=provision_result["project_conn"],
                operation_mix=cfg.workloads.api.operation_mix,
                create_delete_volume_size_gib=cfg.workloads.api.create_delete_volume_size_gib,
                target_ops_per_second=soak_plan.api.target_ops_per_second,
                duration_seconds=duration_s,
                run_id=state.run_id,
                project_name=state.project_name,
                network_id=_run_network_id(provision_result["tracker"]),
                metric_writer=metrics,
                event_writer=events,
                tracker=provision_result["tracker"],
                stop_requested=stop_requested,
            )
            _run_api_workload_with_periodic_collection(workload, run_dir=run_dir, state=state)
        else:
            _sleep_with_periodic_collection(
                duration_s, run_dir=run_dir, state=state, stop_requested=stop_requested
            )
        if stop_requested():
            raise InterruptedError("Stop requested")
        _sleep_with_periodic_collection(
            20, run_dir=run_dir, state=state, stop_requested=stop_requested
        )
        state.summary["workload_status"] = "completed"
    except BaseException as exc:
        failure = exc
        state.summary["workload_status"] = (
            "interrupted" if isinstance(exc, (KeyboardInterrupt, InterruptedError)) else "failed"
        )
        events.write(
            level="error",
            plane="control",
            event="run_failed",
            details={"error": str(exc), "type": type(exc).__name__},
        )
    finally:
        if state.timeline.steady_state_started_at:
            state.timeline.steady_state_ended_at = utcnow_iso()
        state.timeline.ended_at = utcnow_iso()
        # Preserve diagnostics before any deletion; one failed collection must not suppress the other.
        if state.controller_url:
            try:
                state.summary["controller_collection"] = collect_controller_artifacts(
                    run_dir, state.controller_url, state.run_id
                )
            except Exception as exc:
                events.write(
                    level="warning",
                    plane="control",
                    event="collection_failed",
                    details={"error": str(exc)},
                )
        state.status = "failed" if failure else "completed"
        save_state(run_dir, state)
        if (run_dir / "plan.yaml").exists():
            try:
                evidence = assess_run(run_dir)
                state.summary["evidence"] = evidence
                if not failure and (evidence["errors"] or not evidence["workload_success"]):
                    failure = RuntimeError(
                        "Workload or measurement evidence incomplete; see final-report.json"
                    )
                    state.status = "failed"
            except Exception as exc:
                if not failure:
                    failure = exc
                    state.status = "failed"
        if failure and conn is not None:
            try:
                collect_failure_console_outputs(conn, run_dir)
            except Exception as exc:
                events.write(
                    level="warning",
                    plane="control",
                    event="failure_diagnostics_failed",
                    details={"error": str(exc)},
                )
        if cfg.run.keep_resources:
            state.summary["cleanup_status"] = "retained"
            if not failure:
                state.status = "left-running"
        else:
            state.timeline.cleanup_started_at = utcnow_iso()
            try:
                cleanup_run(run_dir, conn=conn or admin_connection(state.os_cloud))
                receipt = read_json(run_dir / "cleanup.json", {})
                state.summary["cleanup_status"] = receipt.get("status", "failed")
            except Exception as exc:
                state.summary["cleanup_status"] = "failed"
                events.write(
                    level="error",
                    plane="control",
                    event="cleanup_failed",
                    details={"error": str(exc)},
                )
            state.timeline.cleanup_ended_at = utcnow_iso()
            if state.summary["cleanup_status"] != "passed" and not failure:
                state.status = "cleanup-failed"
        save_state(run_dir, state)
        if (run_dir / "plan.yaml").exists():
            report_error = _final_report(run_dir, state)
            if report_error and not failure:
                failure = report_error
    if failure:
        raise failure
    if state.summary.get("cleanup_status") == "failed":
        raise RuntimeError("Cleanup absence verification failed; see cleanup.json")


def _run_api_workload_with_periodic_collection(
    api_workload: ApiWorkload,
    *,
    run_dir: Path,
    state,
    interval_seconds: int = 60,
) -> None:
    errors: list[BaseException] = []

    def target() -> None:
        try:
            api_workload.run()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread.
            errors.append(exc)

    thread = threading.Thread(target=target, name="soak-api-workload")
    thread.start()
    next_collection = time.monotonic() + interval_seconds
    while thread.is_alive():
        thread.join(timeout=1)
        if time.monotonic() >= next_collection:
            _collect_controller_periodic(run_dir, state)
            next_collection = time.monotonic() + interval_seconds
    if errors:
        raise errors[0]


def _run_rally_api_with_periodic_collection(
    *,
    run_dir: Path,
    state,
    cfg,
    duration_s: int,
    metrics: MetricWriter,
    events: EventWriter,
    interval_seconds: int = 60,
    stop_requested=lambda: False,
) -> None:
    errors: list[BaseException] = []

    def target() -> None:
        try:
            auth = read_json(run_dir / "auth.json")
            run_rally_api_workload(
                run_dir=run_dir,
                auth=auth,
                duration_seconds=duration_s,
                concurrency=cfg.workloads.api.rally_concurrency,
                error_rate_max=cfg.workloads.api.rally_error_rate_max,
                mode=cfg.workloads.api.rally_mode,
                metric_writer=metrics,
                event_writer=events,
                command_runner=_cancellable_rally_runner(stop_requested),
            )
        except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread.
            errors.append(exc)

    thread = threading.Thread(target=target, name="soak-api-rally-workload")
    thread.start()
    next_collection = time.monotonic() + interval_seconds
    while thread.is_alive():
        thread.join(timeout=1)
        if time.monotonic() >= next_collection:
            _collect_controller_periodic(run_dir, state)
            next_collection = time.monotonic() + interval_seconds
    if errors:
        raise errors[0]


def _sleep_with_periodic_collection(
    duration_s: int,
    *,
    run_dir: Path,
    state,
    interval_seconds: int = 1,
    stop_requested=lambda: False,
) -> None:
    deadline = time.monotonic() + duration_s
    next_collection = time.monotonic() + 60
    while True:
        if stop_requested():
            raise InterruptedError("Stop requested")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(interval_seconds, remaining))
        if time.monotonic() >= next_collection:
            _collect_controller_periodic(run_dir, state)
            next_collection = time.monotonic() + 60


def _collect_controller_periodic(run_dir: Path, state) -> None:
    if not state.controller_url:
        return
    try:
        counts = collect_controller_artifacts(run_dir, state.controller_url, state.run_id)
    except Exception as exc:
        EventWriter(run_dir / "events.jsonl", state.run_id).write(
            level="warning", plane="control", event="collection_failed", details={"error": str(exc)}
        )
        return
    summary = state.summary.setdefault(
        "controller_collection_periodic", {"events": 0, "metrics": 0}
    )
    summary["events"] += counts.get("events", 0)
    summary["metrics"] += counts.get("metrics", 0)
    save_state(run_dir, state)


@app.command()
def status(
    run: Annotated[Path, typer.Option("--run", exists=True)],
    watch: Annotated[bool, typer.Option("--watch")] = False,
) -> None:
    """Show local run status and controller status when reachable."""
    while True:
        payload = read_status(run)
        _print_status(payload)
        if not watch:
            return
        time.sleep(5)


@app.command()
def events(
    run: Annotated[Path, typer.Option("--run", exists=True)],
    follow: Annotated[bool, typer.Option("--follow")] = False,
) -> None:
    """Print run events."""
    path = run / "events.jsonl"
    offset = 0
    while True:
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in lines[offset:]:
                console.print(line)
            offset = len(lines)
        if not follow:
            return
        time.sleep(2)


@app.command()
def stop(run: Annotated[Path, typer.Option("--run", exists=True)]) -> None:
    """Request a run stop through local state."""
    state = load_state(run)
    state.stop_requested = True
    state.status = "stopping"
    save_state(run, state)
    EventWriter(run / "events.jsonl", state.run_id).write(
        level="info", plane="control", event="stop_requested"
    )
    console.print(f"Stop requested for {state.run_id}")


@app.command()
def cleanup(
    run: Annotated[Path, typer.Option("--run", exists=True)],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Clean up resources owned by a run."""
    state = load_state(run)
    if dry_run:
        console.print(json.dumps(cleanup_run(run, dry_run=True), indent=2))
        return
    with run_lock(run):
        state.timeline.cleanup_started_at = utcnow_iso()
        save_state(run, state)
        cleanup_error = None
        try:
            actions = cleanup_run(run, conn=admin_connection(state.os_cloud or "sunbeam-admin"))
        except Exception as exc:
            cleanup_error = exc
            actions = []
        receipt = read_json(run / "cleanup.json", {})
        state.summary["cleanup_status"] = receipt.get("status", "failed")
        state.status = (
            (
                "failed"
                if state.status == "failed"
                or state.summary.get("workload_status") in {"failed", "interrupted"}
                else "completed"
            )
            if receipt.get("status") == "passed"
            else "cleanup-failed"
        )
        state.timeline.cleanup_ended_at = utcnow_iso()
        save_state(run, state)
        if (run / "plan.yaml").exists():
            report_error = _final_report(run, state)
            cleanup_error = cleanup_error or report_error
        console.print(json.dumps(actions, indent=2))
        if cleanup_error or receipt.get("status") != "passed":
            raise typer.Exit(1)


@observability_app.command("inspect")
def observability_inspect(run: Annotated[Path, typer.Option("--run", exists=True)]) -> None:
    console.print(json.dumps(inspect_prometheus(run), indent=2))


@observability_app.command("collect")
def observability_collect(run: Annotated[Path, typer.Option("--run", exists=True)]) -> None:
    console.print(json.dumps(collect_prometheus(run), indent=2))


@observability_app.command("report")
def observability_report(run: Annotated[Path, typer.Option("--run", exists=True)]) -> None:
    console.print(json.dumps(correlation_report(run), indent=2))


def _create_run_dir() -> Path:
    run_dir = Path("runs") / make_run_id()
    run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    ensure_dir(run_dir / "worker-logs")
    ensure_dir(run_dir / "controller")
    ensure_dir(run_dir / "observability")
    return run_dir


def _expected_worker_ids(soak_plan) -> set[str]:
    worker_ids = {f"compute-{index:03d}" for index in range(soak_plan.compute.worker_count)}
    worker_ids.update(
        f"network-receiver-{index:03d}" for index in range(soak_plan.network.receiver_count)
    )
    worker_ids.update(
        f"network-sender-{index:03d}" for index in range(soak_plan.network.sender_count)
    )
    worker_ids.update(f"storage-{index:03d}" for index in range(soak_plan.storage.worker_count))
    return worker_ids


def _required_flavor_names(cfg) -> set[str]:
    names = {cfg.flavors.controller}
    for plane in ["compute", "network", "storage"]:
        if getattr(cfg.workloads, plane).enabled:
            flavor = getattr(cfg.flavors, plane + "_worker")
            if flavor:
                names.add(flavor)
    if cfg.workloads.compute.enabled:
        names.update(cfg.workloads.compute.flavor_mix)
    return names


def _run_network_id(tracker) -> str:
    for resource in tracker.resources:
        if resource.get("type") == "network":
            return resource["id"]
    raise RuntimeError("run network id not found")


def _write_run_config(run_dir: Path, source: Path, cfg: object) -> None:
    del source
    (run_dir / "config.yaml").write_text(dump_config(cfg), encoding="utf-8")


def _prepare_openstack_context(conn: object, cfg: object) -> None:
    network = cfg.targets.network
    workload = cfg.workloads.network
    if not workload.enabled or network.scope != "per_host" or workload.hosts:
        return
    hosts = list_hypervisor_names(conn)
    if hosts:
        workload.hosts = hosts


def _print_checks(checks: list[tuple[str, str]]) -> None:
    table = Table("Check", "Status")
    for check, status_value in checks:
        table.add_row(check, status_value)
    console.print(table)


def _print_status(payload: dict[str, object]) -> None:
    state = payload["state"]
    console.print(f"Run: {state['run_id']}")
    console.print(f"Status: {state['status']}")
    console.print(f"Target: {state['target_percent']}%")
    if payload.get("controller"):
        console.print(json.dumps(payload["controller"], indent=2))


RALLY_STOP_TIMEOUT_SECONDS = 10


def _cancellable_rally_runner(stop_requested):
    # One deadline covers every concurrent task and its report/local teardown.
    # Campaign force-kill grace must exceed this budget so no Rally group can
    # survive the runner and race fallback cloud cleanup.
    cancellation_started = None
    cancellation_lock = threading.Lock()

    def stopped_at():
        nonlocal cancellation_started
        if stop_requested():
            with cancellation_lock:
                if cancellation_started is None:
                    cancellation_started = time.monotonic()
        return cancellation_started

    def execute(command, *, check=False, **kwargs):
        cleanup_command = "destroy" in command or "report" in command
        stopped = stopped_at()
        if stopped is not None and (
            not cleanup_command or time.monotonic() >= stopped + RALLY_STOP_TIMEOUT_SECONDS
        ):
            raise InterruptedError("Stop requested")
        process = subprocess.Popen(command, start_new_session=True, **kwargs)
        cancelled = False
        signal_sent = 0
        try:
            while True:
                stopped = stopped_at()
                if stopped is not None:
                    elapsed = time.monotonic() - stopped
                    desired_signal = (
                        signal.SIGKILL
                        if elapsed >= RALLY_STOP_TIMEOUT_SECONDS
                        else signal.SIGTERM
                        if elapsed >= RALLY_STOP_TIMEOUT_SECONDS / 2
                        else signal.SIGINT
                        if not cleanup_command
                        else 0
                    )
                    if desired_signal and desired_signal != signal_sent:
                        try:
                            os.killpg(process.pid, desired_signal)
                        except ProcessLookupError:
                            pass
                        signal_sent = desired_signal
                        cancelled = True
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        # The command leader may exit before descendants that ignored INT.
        # Retire its entire group before permitting cloud cleanup.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if cancelled or (stopped_at() is not None and not cleanup_command):
            raise InterruptedError("Rally stopped before cleanup")
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result

    return execute


def _preflight(conn, cfg, *, run_dir=None):
    conn.authorize()
    get_role(conn, cfg.openstack.member_role)
    image = require_image(conn, cfg.openstack.image)
    external = None
    if cfg.resources.create_router:
        external = require_external_network(conn, cfg.openstack.external_network)
    if cfg.workloads.storage.enabled:
        require_volume_type(conn, cfg.workloads.storage.volume_type)
    if cfg.workloads.api.enabled and cfg.workloads.api.engine == "rally":
        require_rally()
    flavors = {name: require_flavor(conn, name) for name in _required_flavor_names(cfg)}
    if cfg.workloads.network.enabled and cfg.workloads.network.vnic_type == "direct":
        specs = flavors[cfg.flavors.network_worker].extra_specs
        if str(specs.get("hw:vif_multiqueue_enabled", "false")).lower() in {"true", "1"}:
            raise ValueError("direct network flavor carries virtio-only hw:vif_multiqueue_enabled")
    if run_dir is not None:
        atomic_write_json(
            run_dir / "prerequisites.json",
            {
                "image": {
                    key: getattr(image, key, None)
                    for key in ["id", "name", "checksum", "created_at"]
                },
                "external_network": {
                    key: getattr(external, key, None) for key in ["id", "name", "mtu"]
                }
                if external
                else None,
                "assumptions": [
                    "cloud endpoints and quota policy permit the planned resources",
                    "images and flavors remain unchanged during the run",
                    "guest clocks are synchronized",
                    "worker image contains stress-ng, iperf3 and fio for enabled planes",
                ],
            },
        )
    return flavors


def _source_digest():
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _final_report(run_dir, state):
    try:
        generate_final_report(run_dir)
    except Exception as exc:
        state.summary["report_error"] = str(exc)
        state.status = "failed"
        save_state(run_dir, state)
        EventWriter(run_dir / "events.jsonl", state.run_id).write(
            level="error", plane="control", event="report_failed", details={"error": str(exc)}
        )
        return exc
    return None
