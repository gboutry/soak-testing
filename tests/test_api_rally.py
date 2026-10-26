import json

import yaml

from soak_tester.workloads.api_rally import (
    RallyApiSummary,
    parse_rally_results,
    render_rally_task,
)


def test_render_rally_task_uses_control_plane_mix_and_constant_duration_runner():
    task = yaml.safe_load(
        render_rally_task(
            duration_seconds=600,
            concurrency=12,
        )
    )

    assert task["NovaServers.list_servers"][0]["runner"] == {
        "type": "constant_for_duration",
        "duration": 600,
        "concurrency": 12,
    }
    assert "context" not in task["NovaServers.list_servers"][0]
    assert "Authenticate.validate_nova" in task
    assert "Authenticate.validate_neutron" in task
    assert "Authenticate.validate_cinder" in task
    assert "NeutronNetworks.create_and_list_networks" not in task
    assert "CinderVolumes.create_and_delete_volume" not in task


def test_render_rally_task_can_render_single_full_duration_scenario():
    task = yaml.safe_load(
        render_rally_task(
            duration_seconds=300,
            concurrency=64,
            scenario_names=["NovaServers.list_servers"],
        )
    )

    assert list(task) == ["NovaServers.list_servers"]
    assert task["NovaServers.list_servers"][0]["runner"] == {
        "type": "constant_for_duration",
        "duration": 300,
        "concurrency": 64,
    }


def test_parse_rally_results_summarizes_scenarios_and_atomic_actions():
    payload = [
        {
            "key": {"name": "NovaServers.list_servers"},
            "result": [
                {
                    "duration": 0.2,
                    "error": [],
                    "atomic_actions": {"nova.list_servers": 0.15},
                },
                {
                    "duration": 0.4,
                    "error": ["boom"],
                    "atomic_actions": {"nova.list_servers": 0.25},
                },
            ],
        },
        {
            "key": {"name": "NeutronNetworks.list_ports"},
            "result": [
                {
                    "duration": 0.1,
                    "error": None,
                    "atomic_actions": {"neutron.list_ports": 0.08},
                }
            ],
        },
    ]

    summary = parse_rally_results(json.dumps(payload), elapsed_seconds=10)

    assert isinstance(summary, RallyApiSummary)
    assert summary.total_attempts == 3
    assert summary.successes == 2
    assert summary.errors == 1
    assert summary.total_ops_per_second == 0.3
    assert summary.success_ops_per_second == 0.2
    assert summary.error_rate == 1 / 3
    assert summary.latency_p95_ms == 400
    assert summary.scenarios["NovaServers.list_servers"].errors == 1
    assert summary.atomic_actions["nova.list_servers"].latency_p95_ms == 250


def test_parse_rally_report_json_summarizes_aggregate_workloads():
    payload = {
        "tasks": [
            {
                "subtasks": [
                    {
                        "workloads": [
                            {
                                "scenario": {"Authenticate.validate_nova": {"repetitions": 1}},
                                "total_iteration_count": 10,
                                "failed_iteration_count": 1,
                                "statistics": {
                                    "durations": {
                                        "total": {
                                            "data": {
                                                "median": 1.5,
                                                "95%ile": 2.5,
                                                "max": 4.0,
                                            }
                                        },
                                        "atomics": [
                                            {
                                                "name": "authenticate.validate_nova",
                                                "data": {
                                                    "iteration_count": 10,
                                                    "median": 0.5,
                                                    "95%ile": 0.9,
                                                    "max": 1.2,
                                                },
                                            }
                                        ],
                                    }
                                },
                            }
                        ]
                    }
                ]
            }
        ]
    }

    summary = parse_rally_results(payload, elapsed_seconds=5)

    assert summary.total_attempts == 10
    assert summary.successes == 9
    assert summary.errors == 1
    assert summary.total_ops_per_second == 2
    assert summary.scenarios["Authenticate.validate_nova"].latency_p95_ms == 2500
    assert summary.atomic_actions["authenticate.validate_nova"].latency_p95_ms == 900


def test_aggregate_rally_report_does_not_invent_global_percentiles():
    summary = parse_rally_results(
        {
            "tasks": [
                {
                    "subtasks": [
                        {
                            "workloads": [
                                {
                                    "total_iteration_count": 100,
                                    "statistics": {
                                        "durations": {"total": {"data": {"95%ile": 1.2}}}
                                    },
                                }
                            ]
                        }
                    ]
                }
            ]
        },
        elapsed_seconds=10,
    )
    assert summary.latency_p95_ms is None
    assert next(iter(summary.scenarios.values())).latency_p95_ms == 1200


def test_rally_environment_cannot_reuse_inherited_admin_token(monkeypatch):
    from soak_tester.workloads.api_rally import _rally_env

    monkeypatch.setenv("OS_TOKEN", "do-not-use")
    monkeypatch.setenv("OS_CLOUD", "admin")
    monkeypatch.setenv("OS_CACERT", "/trusted/ca.pem")
    env = _rally_env(
        {
            "auth_url": "https://identity",
            "username": "owned",
            "password": "owned-secret",
            "project_name": "owned-project",
        }
    )
    assert "OS_TOKEN" not in env and "OS_CLOUD" not in env
    assert env["OS_CACERT"] == "/trusted/ca.pem"
    assert env["OS_USERNAME"] == "owned"


def test_single_task_rally_uses_private_database_and_tears_down_on_interruption(
    monkeypatch, tmp_path
):
    import pytest
    from types import SimpleNamespace
    from soak_tester.workloads import api_rally
    from soak_tester.events import EventWriter, MetricWriter

    monkeypatch.setattr(api_rally, "require_rally", lambda: None)
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if "start" in command:
            raise InterruptedError("stopped task")
        return SimpleNamespace(returncode=0)

    with pytest.raises(InterruptedError):
        api_rally.run_rally_api_workload(
            run_dir=tmp_path,
            auth={
                "auth_url": "https://identity",
                "username": "owned",
                "password": "owned-secret",
                "project_name": "owned-project",
            },
            duration_seconds=60,
            concurrency=2,
            error_rate_max=0,
            mode="single_task",
            metric_writer=MetricWriter(tmp_path / "metrics.jsonl", "run"),
            event_writer=EventWriter(tmp_path / "events.jsonl", "run"),
            command_runner=runner,
        )
    assert all("--config-file" in command for command in calls)
    assert calls[-1][-4:-2] == ["deployment", "destroy"]
    assert str(tmp_path) in calls[0][calls[0].index("--config-file") + 1]


def test_single_task_rally_rejects_empty_export(monkeypatch, tmp_path):
    from pathlib import Path
    import pytest
    from types import SimpleNamespace
    from soak_tester.workloads import api_rally
    from soak_tester.events import EventWriter, MetricWriter

    monkeypatch.setattr(api_rally, "require_rally", lambda: None)
    monkeypatch.setattr(api_rally, "_task_uuid_from_log", lambda _: "task")
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if "report" in command:
            Path(command[command.index("--out") + 1]).write_text('{"tasks": []}')
        return SimpleNamespace(returncode=0)

    with pytest.raises(RuntimeError, match="single_task mode produced no API iterations"):
        api_rally.run_rally_api_workload(
            run_dir=tmp_path,
            auth={
                "auth_url": "https://identity",
                "username": "owned",
                "password": "owned-secret",
                "project_name": "owned",
            },
            duration_seconds=10,
            concurrency=1,
            error_rate_max=0,
            mode="single_task",
            metric_writer=MetricWriter(tmp_path / "metrics.jsonl", "r"),
            event_writer=EventWriter(tmp_path / "events.jsonl", "r"),
            command_runner=runner,
        )
    assert "destroy" in calls[-1]
    assert '"event": "rally_completed"' not in (tmp_path / "events.jsonl").read_text()
    assert not (tmp_path / "metrics.jsonl").exists()
