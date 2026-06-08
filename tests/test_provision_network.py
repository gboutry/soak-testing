import threading
from types import SimpleNamespace

from soak_tester.config import load_config
from soak_tester.run.provision import (
    ResourceTracker,
    _create_private_network,
    _create_worker,
    _network_sender_legacy_peers,
)


def test_network_senders_do_not_use_legacy_peers_when_flows_are_planned():
    assert _network_sender_legacy_peers(["10.0.0.10"], planned_flows=True) == []


def test_network_senders_keep_legacy_peers_without_planned_flows():
    assert _network_sender_legacy_peers(["10.0.0.10"], planned_flows=False) == [
        "10.0.0.10"
    ]


def test_private_network_uses_configured_mtu(tmp_path):
    config = load_config("tests/fixtures/config.yaml")
    config.resources.network_mtu = 8942
    conn = FakeConnection()
    tracker = ResourceTracker(tmp_path)

    network = _create_private_network(
        conn,
        tracker,
        project_name="soak-tester-soak-1",
        project_id="project-1",
        run_id="soak-1",
        config=config,
    )

    assert network.id == "network-1"
    assert conn.network.created_networks == [
        {
            "name": "soak-tester-soak-1-net",
            "project_id": "project-1",
            "description": "soak-tester network for soak-1",
            "mtu": 8942,
        }
    ]
    assert tracker.resources[0]["type"] == "network"


def test_direct_network_worker_boots_with_precreated_direct_port(tmp_path):
    config = load_config("tests/fixtures/config.yaml")
    config.workloads.network.vnic_type = "direct"
    config.workloads.network.hardware_offload_type = "switchdev"
    config.workloads.network.port_security_enabled = False
    conn = FakeConnection()
    tracker = ResourceTracker(tmp_path)
    placement: list[dict] = []

    server = _create_worker(
        conn,
        tracker,
        placement,
        threading.Lock(),
        "soak-1",
        "soak-tester-soak-1",
        config,
        "image-1",
        "network-1",
        "sg-1",
        "key-1",
        "network",
        "network-sender",
        0,
        "http://controller:8080",
        60,
    )

    assert server.id == "server-1"
    assert conn.network.created_ports == [
        {
            "name": "soak-tester-soak-1-network-sender-000-port",
            "network_id": "network-1",
            "binding_vnic_type": "direct",
            "hardware_offload_type": "switchdev",
            "port_security_enabled": False,
            "security_group_ids": [],
        }
    ]
    assert conn.compute.created_servers[0]["networks"] == [{"port": "port-1"}]
    assert "security_groups" not in conn.compute.created_servers[0]
    assert tracker.resources[0]["type"] == "port"
    assert tracker.resources[0]["id"] == "port-1"
    assert tracker.resources[1]["type"] == "server"


def test_compute_worker_keeps_implicit_network_boot_with_direct_network_config(tmp_path):
    config = load_config("tests/fixtures/config.yaml")
    config.workloads.network.vnic_type = "direct"
    conn = FakeConnection()

    _create_worker(
        conn,
        ResourceTracker(tmp_path),
        [],
        threading.Lock(),
        "soak-1",
        "soak-tester-soak-1",
        config,
        "image-1",
        "network-1",
        "sg-1",
        "key-1",
        "compute",
        "compute",
        0,
        "http://controller:8080",
        60,
    )

    assert conn.network.created_ports == []
    assert conn.compute.created_servers[0]["networks"] == [{"uuid": "network-1"}]
    assert conn.compute.created_servers[0]["security_groups"] == [{"name": "sg-1"}]


class FakeCompute:
    def __init__(self):
        self.created_servers: list[dict] = []

    def find_flavor(self, name):
        return SimpleNamespace(id=f"{name}-id")

    def create_server(self, **payload):
        self.created_servers.append(payload)
        return SimpleNamespace(
            id="server-1",
            name=payload["name"],
            addresses={"private": [{"addr": "10.0.0.10", "version": 4}]},
        )

    def wait_for_server(self, server, **_kwargs):
        return server


class FakeNetwork:
    def __init__(self):
        self.created_ports: list[dict] = []
        self.created_networks: list[dict] = []

    def find_security_group(self, name):
        return SimpleNamespace(id=name)

    def create_network(self, **payload):
        self.created_networks.append(payload)
        return SimpleNamespace(id="network-1", name=payload["name"], mtu=payload.get("mtu"))

    def create_port(self, **payload):
        self.created_ports.append(payload)
        return SimpleNamespace(id="port-1", name=payload["name"])

    def set_tags(self, _resource, _tags):
        return None


class FakeConnection:
    def __init__(self):
        self.compute = FakeCompute()
        self.network = FakeNetwork()
