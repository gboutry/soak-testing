import json

from soak_tester.workloads.results import parse_fio_metrics, parse_iperf3_metrics


def test_parse_iperf3_metrics_prefers_receiver_goodput_and_records_retransmits():
    payload = {
        "end": {
            "sum_sent": {"bits_per_second": 8_000_000_000, "retransmits": 7},
            "sum_received": {"bits_per_second": 7_500_000_000},
        }
    }

    metrics, events = parse_iperf3_metrics(
        json.dumps(payload),
        worker_id="network-sender-000",
        receiver="10.250.0.10",
    )

    assert events == []
    assert metrics == [
        {
            "plane": "network",
            "worker_id": "network-sender-000",
            "metric": "iperf.goodput_bps",
            "value": 7_500_000_000,
            "unit": "bps",
            "tags": {"receiver": "10.250.0.10"},
        },
        {
            "plane": "network",
            "worker_id": "network-sender-000",
            "metric": "iperf.retransmits",
            "value": 7,
            "unit": "count",
            "tags": {"receiver": "10.250.0.10"},
        },
    ]


def test_parse_iperf3_metrics_preserves_flow_tags_and_error_payloads():
    payload = {"error": "the server is busy running a test. try again later"}

    metrics, events = parse_iperf3_metrics(
        json.dumps(payload),
        worker_id="network-sender-000",
        receiver="network-receiver-001",
        tags={
            "flow_id": "network-flow-00001",
            "receiver_port": "5202",
            "source_host": "host-000",
            "dest_host": "host-001",
        },
    )

    assert metrics == [
        {
            "plane": "network",
            "worker_id": "network-sender-000",
            "metric": "iperf.flow_failed",
            "value": 1,
            "unit": "count",
            "tags": {
                "receiver": "network-receiver-001",
                "flow_id": "network-flow-00001",
                "receiver_port": "5202",
                "source_host": "host-000",
                "dest_host": "host-001",
            },
        }
    ]
    assert events[0]["event"] == "iperf_failed"
    assert "server is busy" in events[0]["details"]["error"]


def test_parse_iperf3_metrics_reports_parse_error_event():
    metrics, events = parse_iperf3_metrics("not-json", worker_id="sender", receiver="receiver")

    assert metrics == []
    assert events[0]["event"] == "iperf_parse_failed"
    assert events[0]["plane"] == "network"
    assert events[0]["worker_id"] == "sender"


def test_parse_fio_metrics_extracts_bandwidth_iops_and_percentiles():
    payload = {
        "jobs": [
            {
                "read": {
                    "iops": 1000,
                    "bw_bytes": 200_000_000,
                    "clat_ns": {"percentile": {"95.000000": 9000, "99.000000": 15000}},
                },
                "write": {
                    "iops": 250,
                    "bw_bytes": 50_000_000,
                    "clat_ns": {"percentile": {"95.000000": 11000, "99.000000": 20000}},
                },
            },
            {
                "read": {"iops": 500, "bw_bytes": 100_000_000, "clat_ns": {"percentile": {}}},
                "write": {"iops": 125, "bw_bytes": 25_000_000, "clat_ns": {"percentile": {}}},
            },
        ]
    }

    metrics, events = parse_fio_metrics(json.dumps(payload), worker_id="storage-000")

    assert events == []
    values = {metric["metric"]: metric["value"] for metric in metrics}
    assert values["fio.read_iops"] == 1500
    assert values["fio.write_iops"] == 375
    assert values["fio.read_bps"] == 2_400_000_000
    assert values["fio.write_bps"] == 600_000_000
    assert values["fio.read_p95_latency_ns"] == 9000
    assert values["fio.write_p99_latency_ns"] == 20000


def test_parse_fio_metrics_handles_missing_latency_fields():
    payload = {"jobs": [{"read": {"iops": 1, "bw_bytes": 2}, "write": {"iops": 3, "bw_bytes": 4}}]}

    metrics, events = parse_fio_metrics(json.dumps(payload), worker_id="storage-000")

    assert events == []
    metric_names = {metric["metric"] for metric in metrics}
    assert "fio.read_p95_latency_ns" not in metric_names
    assert "fio.write_bps" in metric_names
