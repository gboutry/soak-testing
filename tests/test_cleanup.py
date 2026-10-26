import json
from types import SimpleNamespace

from soak_tester.cleanup import _resource_matches_run, cleanup_order, cleanup_run, dry_run_cleanup


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


def test_cleanup_never_discovers_foreign_project_resources_by_name_or_tag():
    resource = {
        "name": "soak-project-run-net",
        "project_id": "foreign-project",
        "tags": ["soak_tester_run_id=run", "soak_tester_owned=true"],
    }
    assert not _resource_matches_run(resource, "run", "soak-project-run", "owned-project")


def test_cleanup_without_project_identity_requires_explicit_ownership():
    resource = {"name": "soak-project-run-net"}
    assert not _resource_matches_run(resource, "run", "soak-project-run", None)
    resource["tags"] = ["soak_tester_run_id=run"]
    assert not _resource_matches_run(resource, "run", "soak-project-run", None)
    resource["tags"].append("soak_tester_owned=true")
    assert _resource_matches_run(resource, "run", "soak-project-run", None)


class InventoryConnection:
    def __init__(self, objects=None):
        self.objects = objects or {}
        self.deleted = []
        services = {
            "server": "compute",
            "volume": "block_storage",
            "project": "identity",
            "user": "identity",
        }
        methods = {
            "server": "servers",
            "volume": "volumes",
            "floating_ip": "ips",
            "security_group": "security_groups",
            "keypair": "keypairs",
        }
        for service in ["compute", "block_storage", "network", "identity"]:
            setattr(self, service, SimpleNamespace())
        for kind in [
            "server",
            "volume",
            "network",
            "subnet",
            "router",
            "port",
            "floating_ip",
            "security_group",
            "project",
            "user",
            "keypair",
        ]:
            proxy = getattr(self, services.get(kind, "compute" if kind == "keypair" else "network"))
            setattr(
                proxy,
                methods.get(kind, kind + "s"),
                lambda _kind=kind, **kwargs: [
                    SimpleNamespace(id=id, name=id, project_id="p")
                    for id, t in self.objects.items()
                    if t == _kind
                ],
            )
            setattr(
                proxy,
                "get_" + ("ip" if kind == "floating_ip" else kind),
                lambda id, _kind=kind, **kwargs: (
                    SimpleNamespace(id=id) if self.objects.get(id) == _kind else None
                ),
            )

            def delete(id, _kind=kind, **kwargs):
                self.deleted.append((_kind, id))
                self.objects.pop(id, None)

            setattr(proxy, "delete_" + ("ip" if kind == "floating_ip" else kind), delete)


def test_cleanup_verifies_absence_and_is_repeatable(tmp_path):
    objects = {"p": "project", "u": "user", "s": "server", "v": "volume"}
    (tmp_path / "resources.json").write_text(
        json.dumps(
            {"resources": [{"type": kind, "id": id, "name": id} for id, kind in objects.items()]}
        )
    )
    conn = InventoryConnection(dict(objects))
    cleanup_run(tmp_path, conn, settle_seconds=0)
    assert json.loads((tmp_path / "cleanup.json").read_text())["status"] == "passed"
    assert not conn.objects
    cleanup_run(tmp_path, conn, settle_seconds=0)
    assert json.loads((tmp_path / "cleanup.json").read_text())["status"] == "passed"


def test_cleanup_delete_request_without_absence_is_not_success(tmp_path):
    (tmp_path / "resources.json").write_text(
        json.dumps({"resources": [{"type": "project", "id": "p"}, {"type": "server", "id": "s"}]})
    )
    conn = InventoryConnection({"p": "project", "s": "server"})
    conn.compute.delete_server = lambda *args, **kwargs: None
    cleanup_run(tmp_path, conn, settle_seconds=0)
    receipt = json.loads((tmp_path / "cleanup.json").read_text())
    assert receipt["status"] == "failed"
    assert receipt["remaining"]["server"] == ["s"]
    assert ("project", "p") not in conn.deleted


def test_cleanup_inventory_error_is_preserved_and_retains_project(tmp_path):
    (tmp_path / "resources.json").write_text(
        json.dumps({"resources": [{"type": "project", "id": "p"}]})
    )
    conn = InventoryConnection({"p": "project"})

    def unavailable(**kwargs):
        raise RuntimeError("inventory unavailable")

    conn.block_storage.volumes = unavailable
    import pytest

    with pytest.raises(RuntimeError, match="inventory unavailable"):
        cleanup_run(tmp_path, conn, settle_seconds=0)
    assert json.loads((tmp_path / "cleanup.json").read_text())["status"] == "failed"
    assert not conn.deleted


def test_uncertain_identity_response_recovers_only_exact_intent(tmp_path):
    from unittest.mock import Mock
    from soak_tester.cleanup import _recover_identity

    (tmp_path / "ownership-intent.json").write_text(
        json.dumps({"name": "run", "description": "owned-nonce"})
    )
    resources = {"resources": []}
    identity = SimpleNamespace(
        projects=Mock(
            return_value=[
                SimpleNamespace(id="p", name="run", description="owned-nonce"),
                SimpleNamespace(id="foreign", name="run", description="other"),
            ]
        ),
        users=Mock(return_value=[]),
    )
    _recover_identity(tmp_path, resources, SimpleNamespace(identity=identity))
    assert [r["id"] for r in resources["resources"]] == ["p"]


def test_cleanup_retries_transient_failure_and_keeps_receipt_history(tmp_path):
    (tmp_path / "resources.json").write_text(
        json.dumps({"resources": [{"type": "project", "id": "p"}, {"type": "server", "id": "s"}]})
    )
    conn = InventoryConnection({"p": "project", "s": "server"})
    delete = conn.compute.delete_server
    attempts = 0

    def flaky(id, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient delete failure")
        return delete(id, **kwargs)

    conn.compute.delete_server = flaky
    cleanup_run(tmp_path, conn, settle_seconds=0)
    receipt = json.loads((tmp_path / "cleanup.json").read_text())
    assert receipt["status"] == "passed"
    assert any("transient delete failure" in a.get("error", "") for a in receipt["actions"])
    assert not conn.objects


def test_cleanup_discovers_unrecorded_keypair_only_in_owned_user(tmp_path):
    from unittest.mock import Mock

    (tmp_path / "resources.json").write_text(
        json.dumps({"resources": [{"type": "project", "id": "p"}, {"type": "user", "id": "u"}]})
    )
    conn = InventoryConnection({"p": "project", "u": "user", "key": "keypair"})
    list_keys = conn.compute.keypairs
    conn.compute.keypairs = Mock(
        side_effect=lambda **kwargs: [
            SimpleNamespace(name=item.name, id=item.id) for item in list_keys(**kwargs)
        ]
    )
    delete = conn.compute.delete_keypair
    conn.compute.delete_keypair = Mock(side_effect=delete)
    cleanup_run(tmp_path, conn, settle_seconds=0)
    conn.compute.keypairs.assert_any_call(user_id="u")
    conn.compute.delete_keypair.assert_any_call("key", ignore_missing=True, user_id="u")
    assert json.loads((tmp_path / "cleanup.json").read_text())["status"] == "passed"
