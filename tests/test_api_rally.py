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
            times=120,
            volume_size_gib=1,
            include_create_delete=True,
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
