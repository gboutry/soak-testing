import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from typer.testing import CliRunner

from soak_tester import cli
from soak_tester.planner import FlavorSpec
from soak_tester.state import initial_state, save_state, load_state, run_lock


@pytest.mark.parametrize("failure_stage", ["provision", "workload", None])
@pytest.mark.parametrize("profile", ["network-virtio", "api-native"])
def test_cli_finalizes_after_partial_failure_and_interruption(
    monkeypatch, tmp_path, failure_stage, profile
):
    order = []
    config_path = tmp_path / "input.yaml"
    config = yaml.safe_load((Path(__file__).parents[1] / "configs" / f"{profile}.yaml").read_text())
    if profile == "api-native":
        config["workloads"]["api"]["operation_mix"] = {"create_delete_volumes": 1}
        config["workloads"]["api"]["create_delete_volume_size_gib"] = 7
    config_path.write_text(yaml.safe_dump(config))
    api_workload = Mock()
    monkeypatch.setattr(cli, "ApiWorkload", api_workload)
    monkeypatch.setattr(cli, "assess_run", lambda *a: {"errors": [], "workload_success": True})
    monkeypatch.setattr(cli, "_create_run_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "admin_connection", lambda _: Mock())
    monkeypatch.setattr(
        cli,
        "_preflight",
        lambda conn, cfg, **kwargs: {
            name: FlavorSpec(name=name, vcpus=2, ram_gib=4)
            for name in cli._required_flavor_names(cfg)
        },
    )
    monkeypatch.setattr(
        cli, "require_flavor", lambda conn, name: FlavorSpec(name=name, vcpus=2, ram_gib=4)
    )

    def provision(**kwargs):
        (tmp_path / "controller-token").write_text("secret")
        if failure_stage == "provision":
            raise RuntimeError("partial create")
        return {
            "controller_url": "http://controller:8080",
            "project_conn": Mock(),
            "tracker": SimpleNamespace(resources=[{"type": "network", "id": "network-id"}]),
        }

    monkeypatch.setattr(cli, "provision_openstack_run", provision)
    monkeypatch.setattr(cli, "wait_for_controller", lambda *a, **k: True)
    monkeypatch.setattr(cli, "wait_for_worker_heartbeats", lambda *a, **k: True)
    monkeypatch.setattr(
        cli.requests,
        "post",
        lambda *a, **k: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"started_at": 100, "ends_at": 160}
        ),
    )

    def workload(*args, **kwargs):
        if failure_stage == "workload":
            raise InterruptedError("stopped")

    monkeypatch.setattr(cli, "_sleep_with_periodic_collection", workload)
    monkeypatch.setattr(cli, "_run_api_workload_with_periodic_collection", workload)
    monkeypatch.setattr(cli, "collect_controller_artifacts", lambda *a: order.append("evidence"))
    monkeypatch.setattr(cli, "collect_failure_console_outputs", lambda *a: order.append("console"))

    def cleanup(*a, **k):
        order.append("cleanup")
        (tmp_path / "cleanup.json").write_text(json.dumps({"status": "passed"}))

    monkeypatch.setattr(cli, "cleanup_run", cleanup)
    monkeypatch.setattr(cli, "generate_final_report", lambda *a: order.append("report"))
    result = CliRunner().invoke(
        cli.app,
        [
            "run",
            "--os-cloud",
            "test",
            "--config",
            str(config_path),
            "--target",
            "100",
            "--duration",
            "1m",
        ],
    )
    assert (result.exit_code == 0) == (failure_stage is None)
    if profile == "api-native" and failure_stage != "provision":
        assert api_workload.call_args.kwargs["create_delete_volume_size_gib"] == 7
    assert order[-2:] == ["cleanup", "report"]
    if "evidence" in order:
        assert order.index("evidence") < order.index("cleanup")
    state = load_state(tmp_path)
    assert state.summary["cleanup_status"] == "passed"
    assert (
        state.summary["workload_status"]
        == {"provision": "failed", "workload": "interrupted", None: "completed"}[failure_stage]
    )


def test_dry_run_cleanup_does_not_change_state(tmp_path):
    save_state(tmp_path, initial_state("r", target_percent=100, os_cloud="test", duration="1m"))
    before = (tmp_path / "state.json").read_bytes()
    result = CliRunner().invoke(cli.app, ["cleanup", "--run", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0
    assert (tmp_path / "state.json").read_bytes() == before
    assert not (tmp_path / "cleanup.json").exists()


def test_stop_request_survives_stale_periodic_state_write(tmp_path):
    stale = initial_state("r", target_percent=100, os_cloud="test", duration="1m")
    save_state(tmp_path, stale)
    assert CliRunner().invoke(cli.app, ["stop", "--run", str(tmp_path)]).exit_code == 0
    save_state(tmp_path, stale)
    assert load_state(tmp_path).stop_requested


def test_active_run_refuses_concurrent_cleanup(tmp_path):
    with run_lock(tmp_path), pytest.raises(RuntimeError, match="active"):
        with run_lock(tmp_path):
            pass


def test_sigterm_requests_stop_and_restores_handler():
    previous = signal.getsignal(signal.SIGTERM)
    with cli._stop_signals() as stopped:
        os.kill(os.getpid(), signal.SIGTERM)
        assert stopped.is_set()
    assert signal.getsignal(signal.SIGTERM) == previous


def test_rally_command_cancellation_reaps_owned_subprocess():
    calls = 0

    def stopping():
        nonlocal calls
        calls += 1
        return calls > 1

    runner = cli._cancellable_rally_runner(stopping)
    with pytest.raises(InterruptedError):
        runner(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def test_run_directory_collision_does_not_overwrite_artifacts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "make_run_id", lambda: "same")
    run_dir = cli._create_run_dir()
    (run_dir / "resources.json").write_text("retained")
    with pytest.raises(FileExistsError):
        cli._create_run_dir()
    assert (run_dir / "resources.json").read_text() == "retained"


def test_concurrent_stop_survives_inflight_status_replacement(monkeypatch, tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from soak_tester import state as state_module

    runner_state = initial_state("r", target_percent=100, os_cloud="test", duration="1m")
    save_state(tmp_path, runner_state)
    stop_state = load_state(tmp_path)
    stop_state.stop_requested = True
    pending_write = threading.Event()
    release_write = threading.Event()
    stopper_started = threading.Event()
    write = state_module.atomic_write_json

    def paused_write(path, payload):
        if not payload["stop_requested"]:
            pending_write.set()
            assert release_write.wait(5)
        write(path, payload)

    monkeypatch.setattr(state_module, "atomic_write_json", paused_write)

    def stop():
        stopper_started.set()
        save_state(tmp_path, stop_state)

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(save_state, tmp_path, runner_state)
        try:
            assert pending_write.wait(5)
            stopper = executor.submit(stop)
            assert stopper_started.wait(5)
            with pytest.raises(TimeoutError):
                stopper.result(timeout=0.1)
            # With an unlocked merge, stop can commit before this stale write.
            # With the state lock, stop waits and merges after it instead.
        finally:
            release_write.set()
        writer.result(timeout=5)
        stopper.result(timeout=5)
    assert load_state(tmp_path).stop_requested
