from __future__ import annotations

from dataclasses import dataclass

from soak_tester.events import EventWriter, MetricWriter
from soak_tester.workloads.api import ApiWorkload, build_operation_sequence, percentile


def test_build_operation_sequence_uses_weighted_deterministic_order():
    sequence = build_operation_sequence({"list_servers": 2, "list_ports": 1})

    assert sequence == ["list_servers", "list_servers", "list_ports"]


def test_build_operation_sequence_defaults_to_read_only_operations():
    sequence = build_operation_sequence({})

    assert sequence == ["list_servers", "list_ports", "list_volumes"]


def test_percentile_uses_nearest_rank():
    values = [1, 2, 3, 4, 5]

    assert percentile(values, 50) == 3
    assert percentile(values, 95) == 5
    assert percentile(values, 99) == 5


def test_api_workload_writes_summary_metrics_and_events(tmp_path):
    conn = FakeConnection()
    tracker = FakeTracker()
    workload = ApiWorkload(
        conn=conn,
        operation_mix={"list_servers": 1, "list_ports": 1, "list_volumes": 1},
        target_ops_per_second=3,
        duration_seconds=1,
        run_id="soak-1",
        project_name="project-1",
        network_id="network-1",
        metric_writer=MetricWriter(tmp_path / "metrics.jsonl", "soak-1"),
        event_writer=EventWriter(tmp_path / "events.jsonl", "soak-1"),
        tracker=tracker,
        sleeper=lambda _seconds: None,
        clock=FakeClock(),
    )

    result = workload.run()

    assert result.attempts == 3
    assert result.successes == 3
    metrics = (tmp_path / "metrics.jsonl").read_text()
    assert '"metric": "api.ops_per_second"' in metrics
    assert '"metric": "api.latency_p95_ms"' in metrics
    events = (tmp_path / "events.jsonl").read_text()
    assert '"event": "api_started"' in events
    assert '"event": "api_completed"' in events


def test_api_workload_create_delete_port_records_resource(tmp_path):
    conn = FakeConnection()
    tracker = FakeTracker()
    workload = ApiWorkload(
        conn=conn,
        operation_mix={"create_delete_ports": 1},
        target_ops_per_second=1,
        duration_seconds=1,
        run_id="soak-1",
        project_name="project-1",
        network_id="network-1",
        metric_writer=MetricWriter(tmp_path / "metrics.jsonl", "soak-1"),
        event_writer=EventWriter(tmp_path / "events.jsonl", "soak-1"),
        tracker=tracker,
        sleeper=lambda _seconds: None,
        clock=FakeClock(),
    )

    workload.run()

    assert conn.network.created_ports == ["soak-api-soak-1-000000"]
    assert conn.network.deleted_ports == ["port-1"]
    assert tracker.resources[0]["type"] == "port"
    assert tracker.resources[0]["id"] == "port-1"


def test_api_workload_records_errors_and_continues(tmp_path):
    conn = FakeConnection(fail_list_ports=True)
    workload = ApiWorkload(
        conn=conn,
        operation_mix={"list_ports": 1, "list_servers": 1},
        target_ops_per_second=2,
        duration_seconds=1,
        run_id="soak-1",
        project_name="project-1",
        network_id="network-1",
        metric_writer=MetricWriter(tmp_path / "metrics.jsonl", "soak-1"),
        event_writer=EventWriter(tmp_path / "events.jsonl", "soak-1"),
        sleeper=lambda _seconds: None,
        clock=FakeClock(),
    )

    result = workload.run()

    assert result.attempts == 2
    assert result.errors == 1
    assert '"metric": "api.errors"' in (tmp_path / "metrics.jsonl").read_text()
    assert '"event": "api_operation_error"' in (tmp_path / "events.jsonl").read_text()


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        self.value += 0.01
        return self.value


class FakeTracker:
    def __init__(self):
        self.resources = []

    def add(self, record):
        self.resources.append(record)


@dataclass
class FakeResource:
    id: str
    name: str


class FakeCompute:
    def servers(self, details=True):
        return [FakeResource("server-1", "server")]

    def get_server(self, server_id):
        return FakeResource(server_id, "server")


class FakeNetwork:
    def __init__(self, *, fail_list_ports=False):
        self.fail_list_ports = fail_list_ports
        self.created_ports = []
        self.deleted_ports = []

    def ports(self):
        if self.fail_list_ports:
            raise RuntimeError("ports unavailable")
        return [FakeResource("port-existing", "existing-port")]

    def get_port(self, port_id):
        return FakeResource(port_id, "existing-port")

    def create_port(self, **kwargs):
        self.created_ports.append(kwargs["name"])
        return FakeResource("port-1", kwargs["name"])

    def delete_port(self, port_id, ignore_missing=True):
        self.deleted_ports.append(port_id)

    def set_tags(self, resource, tags):
        resource.tags = tags


class FakeBlockStorage:
    def volumes(self):
        return [FakeResource("volume-1", "volume")]

    def get_volume(self, volume_id):
        return FakeResource(volume_id, "volume")


class FakeConnection:
    def __init__(self, *, fail_list_ports=False):
        self.compute = FakeCompute()
        self.network = FakeNetwork(fail_list_ports=fail_list_ports)
        self.block_storage = FakeBlockStorage()
