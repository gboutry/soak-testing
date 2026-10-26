import json
import os
import signal

import pytest
from unittest.mock import Mock, patch

import requests

from soak_tester.run.collect import (
    append_controller_metrics,
    collect_failure_console_outputs,
    wait_for_controller,
    wait_for_worker_heartbeats,
)


def test_append_controller_metrics_adds_run_id_and_deduplicates(tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    payload = {
        "metrics": [
            {
                "ts": "2026-04-29T14:00:00Z",
                "plane": "network",
                "worker_id": "sender",
                "metric": "iperf.goodput_bps",
                "value": 1,
                "unit": "bps",
                "tags": {"receiver": "receiver"},
            },
            {
                "ts": "2026-04-29T14:00:00Z",
                "plane": "network",
                "worker_id": "sender",
                "metric": "iperf.goodput_bps",
                "value": 1,
                "unit": "bps",
                "tags": {"receiver": "receiver"},
            },
        ]
    }

    written = append_controller_metrics(metrics_path, run_id="soak-1", payload=payload)
    written_again = append_controller_metrics(metrics_path, run_id="soak-1", payload=payload)

    assert written == 1
    assert written_again == 0
    lines = metrics_path.read_text().splitlines()
    assert len(lines) == 1
    metric = json.loads(lines[0])
    assert metric["run_id"] == "soak-1"
    assert metric["metric"] == "iperf.goodput_bps"


def test_wait_for_worker_heartbeats_waits_until_expected_workers_seen():
    first = Mock()
    first.raise_for_status.return_value = None
    first.json.return_value = {"heartbeats": [{"worker_id": "compute-000"}]}
    second = Mock()
    second.raise_for_status.return_value = None
    second.json.return_value = {
        "heartbeats": [{"worker_id": "compute-000"}, {"worker_id": "storage-000"}]
    }

    with (
        patch("soak_tester.run.collect.requests.get", side_effect=[first, second]) as request,
        patch("soak_tester.run.collect.time.sleep"),
    ):
        assert wait_for_worker_heartbeats(
            "http://controller:8080",
            expected_worker_ids={"compute-000", "storage-000"},
            timeout_seconds=30,
        )

    assert request.call_count == 2


def test_readiness_retains_missing_ids_and_http_errors(tmp_path):
    response = Mock(status_code=200)
    response.json.return_value = {"heartbeats": [{"worker_id": "compute-000"}]}
    path = tmp_path / "readiness.jsonl"
    with (
        patch(
            "soak_tester.run.collect.requests.get",
            side_effect=[
                requests.ConnectionError("connection refused"),
                response,
            ],
        ),
        patch("soak_tester.run.collect.time.sleep"),
        patch("soak_tester.run.collect.time.monotonic", side_effect=[0, 1, 1, 2, 3, 3, 4, 31]),
    ):
        assert not wait_for_worker_heartbeats(
            "http://controller:8080",
            expected_worker_ids={"compute-000", "storage-000"},
            timeout_seconds=30,
            diagnostics_path=path,
        )
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["error"] == "connection refused"
    assert records[0]["missing_worker_ids"] == ["compute-000", "storage-000"]
    assert records[1]["missing_worker_ids"] == ["storage-000"]
    assert records[1]["heartbeats"] == [{"worker_id": "compute-000"}]


def test_controller_readiness_retains_http_outcome(tmp_path):
    path = tmp_path / "readiness.jsonl"
    with patch("soak_tester.run.collect.requests.get", return_value=Mock(status_code=200)):
        assert wait_for_controller("http://controller:8080", diagnostics_path=path)
    assert json.loads(path.read_text())["http_status"] == 200


def test_failure_console_sample_keeps_missing_worker_error_and_controller(tmp_path):
    (tmp_path / "placement.json").write_text(
        json.dumps(
            [
                {"worker_id": "controller", "server_id": "controller-id", "role": "controller"},
                {"worker_id": "compute-000", "server_id": "ready-id", "role": "compute"},
                {"worker_id": "storage-000", "server_id": "missing-id", "role": "storage"},
            ]
        )
    )
    (tmp_path / "readiness-workers.jsonl").write_text(
        json.dumps({"missing_worker_ids": ["storage-000"]}) + "\n"
    )
    conn = Mock()
    conn.session.get_endpoint.return_value = "http://nova/v2.1"
    response = Mock()
    response.json.return_value = {"output": "cloud-init done"}
    conn.session.post.side_effect = [response, requests.Timeout("console timeout")]
    collect_failure_console_outputs(conn, tmp_path)
    rows = json.loads((tmp_path / "failure-consoles.json").read_text())
    assert [row["worker_id"] for row in rows] == ["controller", "storage-000"]
    assert rows[0]["console"] == {"output": "cloud-init done"}
    assert rows[1]["error"] == "console timeout"
    assert (tmp_path / "failure-consoles.json").stat().st_mode & 0o777 == 0o600
    assert all(call.kwargs["timeout"] == 8 for call in conn.session.post.call_args_list)


def test_native_run_retains_failure_diagnostics_before_cleanup(monkeypatch, tmp_path):
    from pathlib import Path

    from typer.testing import CliRunner

    from soak_tester import cli
    from soak_tester.planner import FlavorSpec

    order = []
    monkeypatch.setattr(cli, "_create_run_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "admin_connection", lambda _: Mock())
    monkeypatch.setattr(cli, "_prepare_openstack_context", lambda *args: None)
    monkeypatch.setattr(
        cli,
        "_preflight",
        lambda conn, cfg, **kwargs: {
            name: FlavorSpec(name=name, vcpus=2, ram_gib=4)
            for name in cli._required_flavor_names(cfg)
        },
    )
    monkeypatch.setattr(
        cli,
        "require_flavor",
        lambda conn, name: FlavorSpec(
            name=name,
            vcpus=2,
            ram_gib=4,
        ),
    )
    monkeypatch.setattr(
        cli,
        "provision_openstack_run",
        lambda **kwargs: {"controller_url": "http://controller:8080"},
    )
    monkeypatch.setattr(cli, "wait_for_controller", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        cli, "collect_controller_artifacts", lambda *args: order.append("controller")
    )
    monkeypatch.setattr(
        cli, "collect_failure_console_outputs", lambda *args: order.append("console")
    )
    monkeypatch.setattr(cli, "cleanup_run", lambda *args, **kwargs: order.append("cleanup"))
    result = CliRunner().invoke(
        cli.app,
        [
            "run",
            "--os-cloud",
            "test",
            "--config",
            str(Path(__file__).parent / "fixtures/config.yaml"),
            "--target",
            "80",
            "--duration",
            "1m",
        ],
    )
    assert result.exit_code != 0
    assert order == ["controller", "console", "cleanup"]
    assert json.loads((tmp_path / "state.json").read_text())["status"] == "failed"


@pytest.mark.parametrize("kind", ["controller", "worker", "receiver"])
@pytest.mark.parametrize("request_fails", [False, True])
def test_readiness_stops_after_signal_and_retains_last_diagnostics(tmp_path, kind, request_fails):
    from soak_tester import cli
    from soak_tester.run.provision import _wait_for_network_receivers

    path = tmp_path / "readiness.jsonl"

    def get(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        if request_fails:
            raise requests.ConnectionError("not ready")
        response = Mock(status_code=200)
        response.json.return_value = {"heartbeats": []}
        return response

    with (
        cli._stop_signals() as stopped,
        patch("soak_tester.run.collect.requests.get", side_effect=get) as request,
        patch("soak_tester.run.collect.time.sleep") as sleep,
    ):
        options = {"diagnostics_path": path, "stop_requested": stopped.is_set}
        with pytest.raises(InterruptedError, match="Stop requested during readiness"):
            if kind == "controller":
                wait_for_controller("http://controller:8080", **options)
            elif kind == "worker":
                wait_for_worker_heartbeats(
                    "http://controller:8080", expected_worker_ids={"compute-000"}, **options
                )
            else:
                _wait_for_network_receivers("http://controller:8080", 1, **options)
        assert stopped.is_set()
        assert request.call_count == 1
        sleep.assert_not_called()
    record = json.loads(path.read_text())
    assert ("error" in record) == request_fails
