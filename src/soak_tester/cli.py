from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.table import Table

from soak_tester.calibration import (
    build_calibrated_config,
    build_calibration_profile,
    discover_cloud_capacity,
    write_calibration_profile,
)
from soak_tester.cleanup import cleanup_run
from soak_tester.config import dump_config, load_config
from soak_tester.events import EventWriter, MetricWriter
from soak_tester.openstack_client.compute import list_hypervisor_names, require_flavor
from soak_tester.openstack_client.connection import admin_connection
from soak_tester.openstack_client.identity import get_role
from soak_tester.openstack_client.image import require_image
from soak_tester.openstack_client.network import require_external_network
from soak_tester.openstack_client.quota import read_quotas
from soak_tester.openstack_client.volume import require_volume_type
from soak_tester.observability.collect import collect_prometheus, inspect_prometheus
from soak_tester.observability.correlate import correlation_report
from soak_tester.planner import create_plan, default_flavor_specs, write_plan
from soak_tester.report import generate_final_report
from soak_tester.run.collect import (
    collect_controller_artifacts,
    wait_for_controller,
    wait_for_worker_heartbeats,
)
from soak_tester.run.provision import provision_openstack_run
from soak_tester.run.status import read_status
from soak_tester.state import initial_state, load_state, save_state
from soak_tester.util import duration_seconds, ensure_dir, make_run_id, utcnow_iso
from soak_tester.workloads.api import ApiWorkload

console = Console()
app = typer.Typer(help="OpenStack-native soak-load testing tool.")
observability_app = typer.Typer(help="Prometheus evidence commands.")
calibrate_app = typer.Typer(help="Calibration commands.")
app.add_typer(observability_app, name="observability")
app.add_typer(calibrate_app, name="calibrate")


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
    for flavor in sorted(set(cfg.flavors.model_dump().values())):
        require_flavor(conn, flavor)
        checks.append((f"flavor {flavor}", "ok"))
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
    try:
        conn = admin_connection(os_cloud)
        _prepare_openstack_context(conn, cfg)
        flavor_specs = {name: require_flavor(conn, name) for name in _required_flavor_names(cfg)}
    except Exception as exc:
        console.print(f"[yellow]OpenStack flavor lookup unavailable, using 1 vCPU/1 GiB defaults: {exc}[/yellow]")
        flavor_specs = default_flavor_specs(cfg)
    soak_plan = create_plan(cfg, target_percent=target, flavor_specs=flavor_specs, run_id=run_dir.name)
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
    """Create artifacts and run the MVP OpenStack soak workflow."""
    run_dir = _create_run_dir()
    cfg = load_config(config)
    state = initial_state(run_dir.name, target_percent=target, os_cloud=os_cloud, duration=duration)
    state.failure_budget_remaining = cfg.run.failure_budget
    state.project_name = f"{cfg.resources.project_prefix}-{run_dir.name}"
    save_state(run_dir, state)
    events = EventWriter(run_dir / "events.jsonl", run_dir.name)
    metrics = MetricWriter(run_dir / "metrics.jsonl", run_dir.name)
    flavor_specs = default_flavor_specs(cfg)
    try:
        conn = admin_connection(os_cloud)
        _prepare_openstack_context(conn, cfg)
        flavor_specs = {name: require_flavor(conn, name) for name in _required_flavor_names(cfg)}
    except Exception as exc:
        events.write(level="warning", plane="control", event="openstack_lookup_unavailable", details={"error": str(exc)})
    soak_plan = create_plan(cfg, target_percent=target, flavor_specs=flavor_specs, run_id=run_dir.name)
    _write_run_config(run_dir, config, cfg)
    write_plan(run_dir / "plan.yaml", soak_plan)
    state.status = "provisioning"
    state.timeline.provisioning_started_at = utcnow_iso()
    save_state(run_dir, state)
    try:
        conn = admin_connection(os_cloud)
        provision_result = provision_openstack_run(
            run_dir=run_dir,
            run_id=run_dir.name,
            project_name=state.project_name,
            config=cfg,
            plan=soak_plan,
            admin_conn=conn,
            duration=duration,
        )
        state.controller_url = provision_result["controller_url"]
    except Exception as exc:
        events.write(
            level="error",
            plane="control",
            event="provisioning_failed",
            details={"error": str(exc)},
        )
        state.status = "failed"
        state.timeline.ended_at = utcnow_iso()
        save_state(run_dir, state)
        try:
            cleanup_run(run_dir, conn=admin_connection(os_cloud), dry_run=False)
        except Exception as cleanup_exc:
            events.write(
                level="error",
                plane="control",
                event="cleanup_after_failure_failed",
                details={"error": str(cleanup_exc)},
            )
        generate_final_report(run_dir)
        raise
    state.status = "running"
    if state.controller_url:
        wait_for_controller(state.controller_url, timeout_seconds=120)
        wait_for_worker_heartbeats(
            state.controller_url,
            expected_worker_ids=_expected_worker_ids(soak_plan),
            timeout_seconds=300,
        )
    state.timeline.steady_state_started_at = utcnow_iso()
    save_state(run_dir, state)
    events.write(level="info", plane="control", event="run_started")
    duration_s = duration_seconds(duration)
    if cfg.workloads.api.enabled and soak_plan.api.target_ops_per_second > 0:
        api_workload = ApiWorkload(
            conn=provision_result["project_conn"],
            operation_mix=cfg.workloads.api.operation_mix,
            target_ops_per_second=soak_plan.api.target_ops_per_second,
            duration_seconds=duration_s,
            run_id=run_dir.name,
            project_name=state.project_name,
            network_id=_run_network_id(provision_result["tracker"]),
            metric_writer=metrics,
            event_writer=events,
            tracker=provision_result["tracker"],
            create_delete_volume_size_gib=cfg.workloads.api.create_delete_volume_size_gib,
        )
        _run_api_workload_with_periodic_collection(api_workload, run_dir=run_dir, state=state)
    else:
        _sleep_with_periodic_collection(duration_s, run_dir=run_dir, state=state)
    state.timeline.steady_state_ended_at = utcnow_iso()
    state.timeline.ended_at = utcnow_iso()
    state.status = "left-running" if cfg.run.leave_running else "completed"
    save_state(run_dir, state)
    if state.controller_url:
        state.summary["controller_collection"] = collect_controller_artifacts(
            run_dir,
            state.controller_url,
            state.run_id,
        )
        save_state(run_dir, state)
    generate_final_report(run_dir)
    if cfg.run.cleanup_on_success and not cfg.run.leave_running:
        cleanup_actions = cleanup_run(run_dir, conn=admin_connection(os_cloud), dry_run=False)
        state.status = "completed"
        state.timeline.cleanup_started_at = state.timeline.steady_state_ended_at
        state.timeline.cleanup_ended_at = utcnow_iso()
        state.summary["cleanup_actions"] = cleanup_actions
        save_state(run_dir, state)
    console.print(f"Run artifacts written to {run_dir}")
    console.print("OpenStack resources were provisioned for this run. See resources.json and placement.json.")


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
    while thread.is_alive():
        thread.join(timeout=interval_seconds)
        _collect_controller_periodic(run_dir, state)
    if errors:
        raise errors[0]


def _sleep_with_periodic_collection(
    duration_s: int,
    *,
    run_dir: Path,
    state,
    interval_seconds: int = 60,
) -> None:
    deadline = time.monotonic() + duration_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(interval_seconds, remaining))
        _collect_controller_periodic(run_dir, state)


def _collect_controller_periodic(run_dir: Path, state) -> None:
    if not state.controller_url:
        return
    counts = collect_controller_artifacts(run_dir, state.controller_url, state.run_id)
    summary = state.summary.setdefault("controller_collection_periodic", {"events": 0, "metrics": 0})
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
    state.status = "cleaning-up"
    state.timeline.cleanup_started_at = utcnow_iso()
    save_state(run, state)
    conn = None if dry_run else admin_connection(state.os_cloud or "sunbeam-admin")
    actions = cleanup_run(run, conn=conn, dry_run=dry_run)
    if dry_run:
        console.print(json.dumps(actions, indent=2))
        return
    state.status = "completed" if not any("error" in action for action in actions) else "cleanup-failed"
    state.timeline.cleanup_ended_at = utcnow_iso()
    save_state(run, state)
    console.print("[yellow]OpenStack destructive cleanup requires wiring a live admin connection; dry-run actions shown instead.[/yellow]")
    console.print(json.dumps(actions, indent=2))


@observability_app.command("inspect")
def observability_inspect(run: Annotated[Path, typer.Option("--run", exists=True)]) -> None:
    console.print(json.dumps(inspect_prometheus(run), indent=2))


@observability_app.command("collect")
def observability_collect(run: Annotated[Path, typer.Option("--run", exists=True)]) -> None:
    console.print(json.dumps(collect_prometheus(run), indent=2))


@observability_app.command("report")
def observability_report(run: Annotated[Path, typer.Option("--run", exists=True)]) -> None:
    console.print(json.dumps(correlation_report(run), indent=2))


@calibrate_app.command("network")
def calibrate_network(
    os_cloud: Annotated[str, typer.Option("--os-cloud")],
    config: Annotated[Path, typer.Option("--config", exists=True)],
    duration: Annotated[str, typer.Option("--duration")] = "10m",
    target: Annotated[float, typer.Option("--target", min=1, max=100)] = 100,
) -> None:
    """Run a network-only calibration and write run artifacts."""
    run_dir = _create_run_dir()
    cfg = load_config(config)
    cfg.workloads.compute.enabled = False
    cfg.workloads.storage.enabled = False
    cfg.workloads.api.enabled = False
    cfg.run.leave_running = False
    cfg.run.cleanup_on_success = True
    state = initial_state(run_dir.name, target_percent=target, os_cloud=os_cloud, duration=duration)
    state.project_name = f"{cfg.resources.project_prefix}-{run_dir.name}"
    state.summary["calibration"] = "network"
    save_state(run_dir, state)
    events = EventWriter(run_dir / "events.jsonl", run_dir.name)
    conn = admin_connection(os_cloud)
    _prepare_openstack_context(conn, cfg)
    flavor_specs = {name: require_flavor(conn, name) for name in _required_flavor_names(cfg)}
    soak_plan = create_plan(cfg, target_percent=target, flavor_specs=flavor_specs, run_id=run_dir.name)
    _write_run_config(run_dir, config, cfg)
    write_plan(run_dir / "plan.yaml", soak_plan)
    state.status = "provisioning"
    state.timeline.provisioning_started_at = utcnow_iso()
    save_state(run_dir, state)
    provision_result = provision_openstack_run(
        run_dir=run_dir,
        run_id=run_dir.name,
        project_name=state.project_name,
        config=cfg,
        plan=soak_plan,
        admin_conn=conn,
        duration=duration,
    )
    state.controller_url = provision_result["controller_url"]
    state.status = "running"
    state.timeline.steady_state_started_at = utcnow_iso()
    save_state(run_dir, state)
    if state.controller_url:
        wait_for_controller(state.controller_url, timeout_seconds=120)
        wait_for_worker_heartbeats(
            state.controller_url,
            expected_worker_ids=_expected_worker_ids(soak_plan),
            timeout_seconds=300,
        )
    events.write(level="info", plane="network", event="network_calibration_started")
    time.sleep(duration_seconds(duration))
    if state.controller_url:
        state.summary["controller_collection"] = collect_controller_artifacts(
            run_dir,
            state.controller_url,
            state.run_id,
        )
    state.timeline.steady_state_ended_at = utcnow_iso()
    state.timeline.ended_at = utcnow_iso()
    state.status = "completed"
    save_state(run_dir, state)
    report = generate_final_report(run_dir)
    calibration = {
        "run_id": run_dir.name,
        "target_percent": target,
        "duration": duration,
        "planned_network": soak_plan.network.model_dump(mode="json"),
        "achieved": report.get("achieved", {}),
        "note": "Calibration is workload-observed guest iperf evidence, not physical fabric proof.",
    }
    (run_dir / "network-calibration.json").write_text(json.dumps(calibration, indent=2), encoding="utf-8")
    if cfg.run.cleanup_on_success:
        cleanup_run(run_dir, conn=admin_connection(os_cloud), dry_run=False)
    console.print(f"Network calibration artifacts written to {run_dir}")


@calibrate_app.command("cloud")
def calibrate_cloud(
    os_cloud: Annotated[str, typer.Option("--os-cloud")],
    config: Annotated[Path, typer.Option("--config", exists=True)],
) -> None:
    """Discover OpenStack-advertised capacity and write calibration artifacts."""
    run_dir = _create_run_dir()
    cfg = load_config(config)
    conn = admin_connection(os_cloud)
    _prepare_openstack_context(conn, cfg)
    calibration = discover_cloud_capacity(conn, cfg)
    generated_config = build_calibrated_config(
        config=cfg,
        cpu_vcpus=int(calibration["generated_capacity"]["compute"]["vcpus"]),
        memory_ram_gib=float(calibration["generated_capacity"]["compute"]["ram_gib"]),
        cpu_resource_class=str(
            calibration["generated_capacity"]["compute"]["cpu_resource_class"]
        ),
    )
    (run_dir / "calibration.json").write_text(
        json.dumps(calibration, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (run_dir / "calibrated-capacity.yaml").write_text(
        yaml.safe_dump(generated_config, sort_keys=False),
        encoding="utf-8",
    )
    console.print(f"Calibration written to {run_dir / 'calibration.json'}")
    console.print(f"Generated config written to {run_dir / 'calibrated-capacity.yaml'}")
    compute = calibration["compute"]["cpu"]["selected"]
    memory = calibration["compute"]["memory"]
    console.print(
        "Compute: "
        f"{compute['allocatable']} {compute['resource_class']} guest cores, "
        f"{memory['allocatable_gib']} GiB RAM"
    )


@calibrate_app.command("profile")
def calibrate_profile(
    os_cloud: Annotated[str, typer.Option("--os-cloud")],
    config: Annotated[Path, typer.Option("--config", exists=True)],
    target: Annotated[float, typer.Option("--target", min=1, max=100)] = 80,
    validation_run: Annotated[
        list[Path] | None,
        typer.Option("--validation-run", exists=True, help="Existing run directory to use as validation evidence."),
    ] = None,
) -> None:
    """Build a tuned calibration profile for per-plane target percentages."""
    run_dir = _create_run_dir()
    cfg = load_config(config)
    conn = admin_connection(os_cloud)
    _prepare_openstack_context(conn, cfg)
    cloud_calibration = discover_cloud_capacity(conn, cfg)
    flavor_specs = {name: require_flavor(conn, name) for name in _required_flavor_names(cfg)}
    profile, tuned_config = build_calibration_profile(
        config=cfg,
        cloud_calibration=cloud_calibration,
        flavor_specs=flavor_specs,
        target_percent=target,
        validation_runs=validation_run or [],
    )
    (run_dir / "calibration.json").write_text(
        json.dumps(cloud_calibration, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_calibration_profile(run_dir, profile, tuned_config)
    console.print(f"Calibration profile written to {run_dir / 'calibration-profile.json'}")
    console.print(f"Tuned config written to {run_dir / 'tuned-config.yaml'}")


def _create_run_dir() -> Path:
    run_dir = Path("runs") / make_run_id()
    ensure_dir(run_dir)
    ensure_dir(run_dir / "worker-logs")
    ensure_dir(run_dir / "controller")
    ensure_dir(run_dir / "observability")
    return run_dir


def _expected_worker_ids(soak_plan) -> set[str]:
    worker_ids = {f"compute-{index:03d}" for index in range(soak_plan.compute.worker_count)}
    worker_ids.update(f"network-receiver-{index:03d}" for index in range(soak_plan.network.receiver_count))
    worker_ids.update(f"network-sender-{index:03d}" for index in range(soak_plan.network.sender_count))
    worker_ids.update(f"storage-{index:03d}" for index in range(soak_plan.storage.worker_count))
    return worker_ids


def _required_flavor_names(cfg) -> set[str]:
    names = set(cfg.flavors.model_dump().values())
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
    network = cfg.capacity.network
    workload = cfg.workloads.network
    if network.scope != "per_host" or workload.hosts:
        return
    hosts = list_hypervisor_names(conn)
    if hosts:
        workload.hosts = hosts
        workload.host_count = len(hosts)


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
