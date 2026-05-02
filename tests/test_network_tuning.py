import json
from pathlib import Path

import yaml

from soak_tester.config import load_config
from soak_tester.network_tuning import (
    apply_config_patch,
    render_matrix_markdown,
    summarize_experiment_runs,
)
from soak_tester.workloads.network import iperf_client_command


def test_iperf_client_command_includes_tuning_options():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.workloads.network.omit_seconds = 10
    config.workloads.network.tcp_window = "16M"
    config.workloads.network.zerocopy = True
    config.workloads.network.bidirectional = True
    config.workloads.network.extra_client_args = ["--connect-timeout", "5000"]

    command = iperf_client_command(config, "10.0.0.2")

    assert command == [
        "iperf3",
        "--client",
        "10.0.0.2",
        "--time",
        "60",
        "--parallel",
        "4",
        "--bitrate",
        "10.0G",
        "--json",
        "--omit",
        "10",
        "--window",
        "16M",
        "--zerocopy",
        "--bidir",
        "--connect-timeout",
        "5000",
    ]


def test_apply_config_patch_updates_dotted_paths():
    payload = {"workloads": {"network": {"parallel_streams_per_flow": 4}}}

    updated = apply_config_patch(
        payload,
        {
            "workloads.network.parallel_streams_per_flow": 32,
            "workloads.network.zerocopy": True,
        },
    )

    assert updated["workloads"]["network"]["parallel_streams_per_flow"] == 32
    assert updated["workloads"]["network"]["zerocopy"] is True


def test_summarize_experiment_runs_reports_stats_and_cross_host(tmp_path):
    run_a = _network_run(
        tmp_path / "soak-a",
        goodput_gbps=30,
        flow_values=[10, 20],
    )
    run_b = _network_run(
        tmp_path / "soak-b",
        goodput_gbps=36,
        flow_values=[16, 20],
    )

    summary = summarize_experiment_runs("p32", [run_a, run_b], baseline_gbps=30)

    assert summary["goodput_gbps"]["mean"] == 33
    assert summary["goodput_gbps"]["median"] == 33
    assert summary["goodput_gbps"]["max"] == 36
    assert summary["improvement_percent_vs_baseline"] == 10
    assert summary["all_cross_host"] is True


def test_render_matrix_markdown_contains_experiment_table():
    markdown = render_matrix_markdown(
        {
            "best": {
                "name": "p32",
                "goodput_gbps": {"mean": 33, "max": 36},
            },
            "experiments": [
                {
                    "name": "baseline",
                    "runs": [{"run_id": "soak-a"}],
                    "goodput_gbps": {
                        "mean": 30,
                        "median": 30,
                        "max": 30,
                        "stddev": 0,
                    },
                    "all_cross_host": True,
                }
            ],
        }
    )

    assert "| baseline | 1 | 30.000 | 30.000 | 30.000 | 0.000 |  | yes |" in markdown
    assert "Experiment: `p32`" in markdown


def _network_run(path: Path, *, goodput_gbps: float, flow_values: list[float]) -> Path:
    path.mkdir()
    (path / "final-report.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "achieved": {"iperf.goodput_gbps_avg": goodput_gbps},
            }
        ),
        encoding="utf-8",
    )
    (path / "plan.yaml").write_text(
        yaml.safe_dump({"network": {"flows": len(flow_values)}}),
        encoding="utf-8",
    )
    (path / "placement.json").write_text(
        json.dumps(
            [
                {"worker_id": "network-sender-000"},
                {"worker_id": "network-receiver-000"},
            ]
        ),
        encoding="utf-8",
    )
    lines = []
    for index, value in enumerate(flow_values):
        lines.append(
            json.dumps(
                {
                    "worker_id": f"network-sender-{index:03d}",
                    "metric": "iperf.goodput_bps",
                    "value": value * 1_000_000_000,
                    "tags": {
                        "flow_id": f"network-flow-{index:05d}",
                        "source_host": "host-a",
                        "dest_host": "host-b",
                    },
                }
            )
        )
    (path / "metrics.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
