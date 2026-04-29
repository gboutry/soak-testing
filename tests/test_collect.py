import json
from unittest.mock import Mock, patch

from soak_tester.run.collect import append_controller_metrics, wait_for_worker_heartbeats


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
