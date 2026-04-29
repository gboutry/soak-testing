from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from soak_tester.cleanup import cleanup_run
from soak_tester.config import dump_config, load_config
from soak_tester.events import EventWriter, MetricWriter
from soak_tester.openstack_client.compute import require_flavor
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
from soak_tester.run.provision import provision_skeleton
from soak_tester.run.status import read_status
from soak_tester.state import initial_state, load_state, save_state
from soak_tester.util import ensure_dir, make_run_id, utcnow_iso

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
    _write_run_config(run_dir, config, cfg)
    state = initial_state(run_dir.name, target_percent=target, os_cloud=os_cloud, duration=None)
    save_state(run_dir, state)
    try:
        conn = admin_connection(os_cloud)
        flavor_specs = {name: require_flavor(conn, name) for name in set(cfg.flavors.model_dump().values())}
    except Exception as exc:
        console.print(f"[yellow]OpenStack flavor lookup unavailable, using 1 vCPU/1 GiB defaults: {exc}[/yellow]")
        flavor_specs = default_flavor_specs(cfg)
    soak_plan = create_plan(cfg, target_percent=target, flavor_specs=flavor_specs, run_id=run_dir.name)
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
    _write_run_config(run_dir, config, cfg)
    state = initial_state(run_dir.name, target_percent=target, os_cloud=os_cloud, duration=duration)
    state.failure_budget_remaining = cfg.run.failure_budget
    state.project_name = f"{cfg.resources.project_prefix}-{run_dir.name}"
    save_state(run_dir, state)
    events = EventWriter(run_dir / "events.jsonl", run_dir.name)
    MetricWriter(run_dir / "metrics.jsonl", run_dir.name)
    flavor_specs = default_flavor_specs(cfg)
    try:
        conn = admin_connection(os_cloud)
        flavor_specs = {name: require_flavor(conn, name) for name in set(cfg.flavors.model_dump().values())}
    except Exception as exc:
        events.write(level="warning", plane="control", event="openstack_lookup_unavailable", details={"error": str(exc)})
    soak_plan = create_plan(cfg, target_percent=target, flavor_specs=flavor_specs, run_id=run_dir.name)
    write_plan(run_dir / "plan.yaml", soak_plan)
    state.status = "provisioning"
    state.timeline.provisioning_started_at = utcnow_iso()
    save_state(run_dir, state)
    provision_skeleton(run_dir=run_dir, run_id=run_dir.name, project_name=state.project_name, config=cfg)
    (run_dir / "placement.json").write_text("[]\n", encoding="utf-8")
    state.status = "running"
    state.timeline.steady_state_started_at = utcnow_iso()
    save_state(run_dir, state)
    events.write(level="info", plane="control", event="run_started")
    state.timeline.steady_state_ended_at = utcnow_iso()
    state.timeline.ended_at = utcnow_iso()
    state.status = "left-running" if cfg.run.leave_running else "completed"
    save_state(run_dir, state)
    generate_final_report(run_dir)
    console.print(f"Run artifacts written to {run_dir}")
    console.print("MVP run orchestration created local artifacts; full VM provisioning is implemented behind SDK wrappers for follow-up hardening.")


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
    actions = cleanup_run(run, dry_run=True if dry_run else True)
    if dry_run:
        console.print(json.dumps(actions, indent=2))
        return
    state.status = "cleanup-failed"
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


def _create_run_dir() -> Path:
    run_dir = Path("runs") / make_run_id()
    ensure_dir(run_dir)
    ensure_dir(run_dir / "worker-logs")
    ensure_dir(run_dir / "controller")
    ensure_dir(run_dir / "observability")
    return run_dir


def _write_run_config(run_dir: Path, source: Path, cfg: object) -> None:
    if source.exists():
        shutil.copyfile(source, run_dir / "config.yaml")
    else:
        (run_dir / "config.yaml").write_text(dump_config(cfg), encoding="utf-8")


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
