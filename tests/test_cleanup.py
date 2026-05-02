import json
from types import SimpleNamespace

from soak_tester.cleanup import cleanup_order, cleanup_run, dry_run_cleanup


def test_cleanup_dry_run_uses_safe_order():
    resources = {
        "resources": [
            {"type": "project", "id": "project-1", "name": "project"},
            {"type": "server", "id": "server-1", "name": "server"},
            {"type": "volume", "id": "volume-1", "name": "volume"},
            {"type": "floating_ip", "id": "fip-1", "name": "203.0.113.10"},
            {
                "type": "router_interface",
                "id": "router-1:subnet-1",
                "name": "router:subnet",
                "extra": {"router_id": "router-1", "subnet_id": "subnet-1"},
            },
            {"type": "port", "id": "port-1", "name": "port"},
            {"type": "security_group", "id": "sg-1", "name": "sg"},
            {"type": "router", "id": "router-1", "name": "router"},
            {"type": "subnet", "id": "subnet-1", "name": "subnet"},
            {"type": "network", "id": "network-1", "name": "network"},
        ]
    }

    actions = dry_run_cleanup(resources)

    assert [action["type"] for action in actions] == [
        "server",
        "volume",
        "floating_ip",
        "router_interface",
        "port",
        "security_group",
        "router",
        "subnet",
        "network",
        "project",
    ]
    assert cleanup_order("server") < cleanup_order("project")
    assert cleanup_order("router_interface") < cleanup_order("port")
    assert cleanup_order("router_interface") < cleanup_order("subnet")
    assert cleanup_order("router_interface") < cleanup_order("network")


class FakeNetwork:
    def networks(self, **_query):
        return [
            SimpleNamespace(
                id="network-1",
                name="soak-tester-soak-1-net",
                project_id="project-1",
                tags=["soak_tester_run_id=soak-1"],
            )
        ]

    def subnets(self, **query):
        if query == {"network_id": "network-1"}:
            return [
                SimpleNamespace(
                    id="subnet-1",
                    name="soak-tester-soak-1-subnet",
                    network_id="network-1",
                )
            ]
        return []

    def ports(self, **query):
        if query == {"network_id": "network-1"}:
            return [
                SimpleNamespace(
                    id="router-port-1",
                    name="",
                    network_id="network-1",
                    device_id="router-1",
                    device_owner="network:router_interface",
                    fixed_ips=[{"subnet_id": "subnet-1", "ip_address": "10.250.0.1"}],
                ),
                SimpleNamespace(
                    id="dhcp-port-1",
                    name="",
                    network_id="network-1",
                    device_id="",
                    device_owner="network:dhcp",
                    fixed_ips=[{"subnet_id": "subnet-1", "ip_address": "10.250.0.2"}],
                ),
            ]
        return []

    def routers(self, **_query):
        return [
            SimpleNamespace(
                id="router-1",
                name="soak-tester-soak-1-router",
                project_id="project-1",
                tags=["soak_tester_run_id=soak-1"],
            )
        ]


class FakeConnection:
    network = FakeNetwork()


def test_cleanup_dry_run_discovers_neutron_dependencies(tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "run_id": "soak-1",
                "status": "failed",
                "target_percent": 80,
                "project_name": "soak-tester-soak-1",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "resources.json").write_text(
        json.dumps(
            {
                "resources": [
                    {
                        "type": "project",
                        "id": "project-1",
                        "name": "soak-tester-soak-1",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    actions = cleanup_run(tmp_path, FakeConnection(), dry_run=True)

    action_types = [action["type"] for action in actions]
    assert action_types == [
        "router_interface",
        "port",
        "router",
        "subnet",
        "network",
        "project",
    ]
    router_interface = actions[0]
    assert router_interface["extra"] == {
        "router_id": "router-1",
        "subnet_id": "subnet-1",
        "port_id": "router-port-1",
    }


class MissingRouterNetwork:
    def get_router(self, _router_id):
        raise RuntimeError("router not found")

    def delete_router(self, resource_id, ignore_missing):
        self.deleted = (resource_id, ignore_missing)


class MissingRouterConnection:
    def __init__(self):
        self.network = MissingRouterNetwork()


def test_cleanup_ignores_router_already_deleted(tmp_path):
    (tmp_path / "resources.json").write_text(
        json.dumps(
            {
                "resources": [
                    {
                        "type": "router",
                        "id": "router-1",
                        "name": "soak-tester-soak-1-router",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    conn = MissingRouterConnection()

    actions = cleanup_run(tmp_path, conn, settle_seconds=0)

    assert "error" not in actions[0]
    assert conn.network.deleted == ("router-1", True)
