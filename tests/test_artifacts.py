import json

from soak_tester.events import EventWriter, MetricWriter
from soak_tester.openstack_client.resources import owned_metadata
from soak_tester.report import (
    _summarize_metrics,
    build_domain_summary,
    render_domain_summary_markdown,
    render_markdown_report,
)


def test_owned_metadata_contains_required_tags():
    metadata = owned_metadata("soak-1", "compute", "worker", "project-a")

    assert metadata == {
        "soak_tester_run_id": "soak-1",
        "soak_tester_owned": "true",
        "soak_tester_plane": "compute",
        "soak_tester_role": "worker",
        "soak_tester_project": "project-a",
    }


def test_event_writer_appends_jsonl(tmp_path):
    path = tmp_path / "events.jsonl"
    writer = EventWriter(path, "soak-1")

    writer.write(level="info", plane="network", event="flow_completed", worker_id="worker-1")

    payload = json.loads(path.read_text().strip())
    assert payload["run_id"] == "soak-1"
    assert payload["level"] == "info"
    assert payload["plane"] == "network"
    assert payload["event"] == "flow_completed"
    assert "ts" in payload


def test_metric_writer_appends_jsonl(tmp_path):
    path = tmp_path / "metrics.jsonl"
    writer = MetricWriter(path, "soak-1")

    writer.write(
        plane="network",
        worker_id="worker-1",
        metric="iperf.goodput_bps",
        value=9_700_000_000,
        unit="bps",
        tags={"receiver": "worker-2"},
    )

    payload = json.loads(path.read_text().strip())
    assert payload["metric"] == "iperf.goodput_bps"
    assert payload["value"] == 9_700_000_000
    assert payload["tags"]["receiver"] == "worker-2"


def test_summarize_metrics_adds_gbps_keys_for_bps_metrics():
    summary = _summarize_metrics(
        [
            {"metric": "iperf.goodput_bps", "value": 9_000_000_000},
            {"metric": "fio.read_bps", "value": 2_000_000_000},
            {"metric": "fio.write_bps", "value": 1_000_000_000},
            {"metric": "fio.read_iops", "value": 1000},
        ]
    )

    assert summary["iperf.goodput_gbps_avg"] == 9
    assert summary["fio.read_gbps_avg"] == 2
    assert summary["fio.write_gbps_avg"] == 1
    assert summary["fio.read_iops_avg"] == 1000


def test_summarize_metrics_sums_per_worker_series_averages():
    summary = _summarize_metrics(
        [
            {
                "metric": "iperf.goodput_bps",
                "worker_id": "sender-0",
                "value": 2_000_000_000,
                "tags": {"receiver": "receiver-0"},
            },
            {
                "metric": "iperf.goodput_bps",
                "worker_id": "sender-0",
                "value": 4_000_000_000,
                "tags": {"receiver": "receiver-0"},
            },
            {
                "metric": "iperf.goodput_bps",
                "worker_id": "sender-1",
                "value": 5_000_000_000,
                "tags": {"receiver": "receiver-1"},
            },
        ]
    )

    assert summary["iperf.goodput_gbps_avg"] == 8


def test_summarize_metrics_averages_percent_metrics_across_workers():
    summary = _summarize_metrics(
        [
            {"metric": "guest.cpu_percent", "worker_id": "compute-0", "value": 90},
            {"metric": "guest.cpu_percent", "worker_id": "compute-0", "value": 100},
            {"metric": "guest.cpu_percent", "worker_id": "compute-1", "value": 50},
            {"metric": "api.error_rate", "worker_id": "local", "value": 0.1},
            {"metric": "api.error_rate", "worker_id": "local", "value": 0.3},
        ]
    )

    assert summary["guest.cpu_percent_avg"] == 72.5
    assert summary["api.error_rate_avg"] == 0.2


def test_summarize_metrics_treats_repeated_flow_files_as_one_series():
    summary = _summarize_metrics(
        [
            {
                "metric": "iperf.goodput_bps",
                "worker_id": "sender-0",
                "value": 2_000_000_000,
                "tags": {"receiver": "receiver-0", "flow_file": "iperf3-00000.json"},
            },
            {
                "metric": "iperf.goodput_bps",
                "worker_id": "sender-0",
                "value": 4_000_000_000,
                "tags": {"receiver": "receiver-0", "flow_file": "iperf3-00001.json"},
            },
        ]
    )

    assert summary["iperf.goodput_gbps_avg"] == 3


def test_render_markdown_report_includes_api_details():
    markdown = render_markdown_report(
        {
            "run_id": "soak-1",
            "status": "completed",
            "target_percent": 25,
            "requested": {
                "compute_vcpus": 0,
                "network_gbps": 0,
                "storage_read_gbps": 0,
                "storage_write_gbps": 0,
                "api_ops_per_second": 10,
            },
            "achieved": {
                "api.ops_per_second_avg": 9,
                "api.success_ops_per_second_avg": 8,
                "api.errors_avg": 1,
                "api.error_rate_avg": 0.1,
                "api.latency_p95_ms_avg": 12,
                "api.latency_p99_ms_avg": 20,
            },
        }
    )

    assert "API achieved avg: 9 ops/s" in markdown
    assert "API success avg: 8 ops/s" in markdown
    assert "API latency p95: 12 ms" in markdown


def test_domain_summary_markdown_includes_all_planes(tmp_path):
    (tmp_path / "api-rally").mkdir()
    (tmp_path / "api-rally" / "summary.json").write_text(
        json.dumps(
            {
                "total_attempts": 100,
                "successes": 90,
                "errors": 10,
                "error_rate": 0.1,
                "total_ops_per_second": 20,
                "success_ops_per_second": 18,
                "latency_p50_ms": 10,
                "latency_p95_ms": 20,
                "latency_p99_ms": 30,
                "scenarios": {
                    "NovaServers.list_servers": {
                        "attempts": 50,
                        "successes": 45,
                        "errors": 5,
                        "latency_p50_ms": 11,
                        "latency_p95_ms": 21,
                        "latency_p99_ms": 31,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    metrics = [
        {"metric": "guest.cpu_percent", "worker_id": "compute-000", "value": 99},
        {"metric": "guest.memory_used_percent", "worker_id": "compute-000", "value": 70},
        {"metric": "iperf.goodput_bps", "worker_id": "network-sender-000", "value": 2_000_000_000, "tags": {"flow_id": "flow-1"}},
        {"metric": "iperf.retransmits", "worker_id": "network-sender-000", "value": 3, "tags": {"flow_id": "flow-1"}},
        {"metric": "fio.read_bps", "worker_id": "storage-000", "value": 1_000_000_000},
        {"metric": "fio.write_iops", "worker_id": "storage-000", "value": 5000},
    ]
    report = {
        "run_id": "soak-1",
        "status": "completed",
        "requested": {
            "compute_vcpus": 4,
            "network_gbps": 2,
            "storage_read_gbps": 1,
            "storage_write_gbps": 0.5,
            "api_ops_per_second": 0,
        },
        "achieved": _summarize_metrics(metrics),
        "steady_state_window": {"started_at": "start", "ended_at": "end"},
    }

    summary = build_domain_summary(tmp_path, report, metrics)
    markdown = render_domain_summary_markdown(summary)

    assert "## Compute" in markdown
    assert "## Network" in markdown
    assert "## Storage" in markdown
    assert "## API" in markdown
    assert "NovaServers.list_servers" in markdown
    assert "success ops per second: 18" in markdown
