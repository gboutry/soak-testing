import json

from soak_tester.events import EventWriter, MetricWriter
from soak_tester.openstack_client.resources import owned_metadata


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
