from types import SimpleNamespace

from soak_tester.openstack_client.quota import set_unlimited_project_quotas


class Proxy:
    def __init__(self):
        self.calls = []

    def update_quota_set(self, project_id, **kwargs):
        self.calls.append(("update_quota_set", project_id, kwargs))


class NetworkProxy:
    def __init__(self):
        self.calls = []

    def update_quota(self, project_id, **kwargs):
        self.calls.append(("update_quota", project_id, kwargs))


def test_set_unlimited_project_quotas_sets_nova_cinder_and_neutron_limits_to_minus_one():
    compute = Proxy()
    block_storage = Proxy()
    network = NetworkProxy()
    conn = SimpleNamespace(compute=compute, block_storage=block_storage, network=network)

    result = set_unlimited_project_quotas(conn, "project-1")

    assert compute.calls == [
        (
            "update_quota_set",
            "project-1",
            {
                "cores": -1,
                "instances": -1,
                "ram": -1,
                "server_groups": -1,
                "server_group_members": -1,
            },
        )
    ]
    assert block_storage.calls == [
        (
            "update_quota_set",
            "project-1",
            {
                "volumes": -1,
                "snapshots": -1,
                "gigabytes": -1,
                "backups": -1,
                "backup_gigabytes": -1,
                "per_volume_gigabytes": -1,
            },
        )
    ]
    assert network.calls == [
        (
            "update_quota",
            "project-1",
            {
                "network": -1,
                "subnet": -1,
                "port": -1,
                "router": -1,
                "floatingip": -1,
                "security_group": -1,
                "security_group_rule": -1,
            },
        )
    ]
    assert result["compute"]["cores"] == -1
    assert result["block_storage"]["gigabytes"] == -1
    assert result["network"]["port"] == -1


def test_set_unlimited_project_quotas_records_unavailable_services():
    conn = SimpleNamespace()

    result = set_unlimited_project_quotas(conn, "project-1")

    assert result == {
        "compute": {"unavailable": "quota update API not available"},
        "block_storage": {"unavailable": "quota update API not available"},
        "network": {"unavailable": "quota update API not available"},
    }
