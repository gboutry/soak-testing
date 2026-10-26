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


def test_parse_fio_metrics_reports_job_errors_without_success_metrics():
    metrics, events = parse_fio_metrics(
        json.dumps({"jobs": [{"error": 19, "read": {"iops": 0}}]}),
        worker_id="storage-000",
    )
    assert [metric["metric"] for metric in metrics] == ["fio.failed"]
    assert events[0]["event"] == "fio_failed"
    assert events[0]["details"]["job_errors"] == [19]


def test_parse_fio_metrics_rejects_empty_jobs():
    metrics, events = parse_fio_metrics('{"jobs": []}', worker_id="storage-000")
    assert metrics[0]["metric"] == "fio.failed"
    assert events[0]["event"] == "fio_failed"


def test_bidir_counts_each_receiver_once_and_keeps_directions_separate():
    metrics, events = parse_iperf3_metrics(
        json.dumps(
            {
                "end": {
                    "sum_received": {"bits_per_second": 10, "seconds": 2},
                    "sum_sent": {"bits_per_second": 11},
                    "sum_received_bidir_reverse": {"bits_per_second": 20, "seconds": 2},
                    "sum_sent_bidir_reverse": {"bits_per_second": 21},
                }
            }
        ),
        worker_id="sender",
        receiver="receiver",
        bidirectional=True,
        started_at=100,
        omit_seconds=5,
        tags={"source_host": "n1", "dest_host": "n2"},
    )
    assert events == []
    assert sum(m["value"] for m in metrics) == 30
    assert [m["tags"]["source_host"] for m in metrics] == ["n1", "n2"]
    assert all(m["measurement_start"] == 105 and m["measurement_end"] == 107 for m in metrics)


def test_sender_only_output_is_not_goodput():
    metrics, events = parse_iperf3_metrics(
        '{"end":{"sum_sent":{"bits_per_second":10}}}', worker_id="s", receiver="r"
    )
    assert not metrics
    assert events[0]["event"] == "iperf_parse_failed"


def test_receiver_intervals_are_retained_and_missing_capture_is_an_error():
    payload = {
        "end": {"sum_received": {"bits_per_second": 10, "seconds": 5}},
        "server_output_json": {"intervals": [{"sum": {"bits_per_second": 9}}]},
    }
    metrics, events = parse_iperf3_metrics(
        json.dumps(payload),
        worker_id="s",
        receiver="r",
        started_at=100,
        capture_receiver_intervals=True,
        tags={"flow_id": "flow"},
    )
    assert metrics[0]["measurement_end"] == 105
    assert events[0]["event"] == "iperf_receiver_intervals"
    assert events[0]["details"]["flow_id"] == "flow"
    assert events[0]["details"]["intervals"] == payload["server_output_json"]["intervals"]
    payload.pop("server_output_json")
    metrics, events = parse_iperf3_metrics(
        json.dumps(payload),
        worker_id="s",
        receiver="r",
        capture_receiver_intervals=True,
    )
    assert not metrics
    assert events[0]["event"] == "iperf_parse_failed"


def test_udp_summary_requires_receiver_and_preserves_measured_zero_with_loss():
    received = {
        "bits_per_second": 0,
        "sender": False,
        "seconds": 5,
        "lost_packets": 10,
        "packets": 10,
        "lost_percent": 100,
        "jitter_ms": 0,
    }
    payload = {"end": {"sum_received": received, "sum_sent": {"bits_per_second": 999}}}
    metrics, events = parse_iperf3_metrics(
        json.dumps(payload),
        worker_id="s",
        receiver="r",
        protocol="udp",
        started_at=100,
    )
    assert not events
    assert next(m for m in metrics if m["metric"] == "iperf.goodput_bps")["value"] == 0
    assert all(m["tags"]["protocol"] == "udp" for m in metrics)
    assert metrics[0]["measurement_start"] == 100
    assert metrics[0]["measurement_end"] == 105
    received["sender"] = True
    metrics, events = parse_iperf3_metrics(
        json.dumps(payload), worker_id="s", receiver="r", protocol="udp"
    )
    assert not metrics
    assert events[0]["event"] == "iperf_parse_failed"
