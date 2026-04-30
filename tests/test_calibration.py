from pathlib import Path

from soak_tester.calibration import discover_cloud_capacity
from soak_tester.config import load_config


class FakePlacement:
    def get(self, path):
        if path == "/resource_providers?limit=1000":
            return {
                "resource_providers": [
                    {"uuid": "rp-0", "name": "compute-0"},
                    {"uuid": "rp-1", "name": "compute-1"},
                    {"uuid": "rp-2", "name": "compute-2"},
                ]
            }
        provider = path.split("/")[2]
        pcpu = {"rp-0": 416, "rp-1": 416, "rp-2": 416}[provider]
        return {
            "inventories": {
                "PCPU": {"total": pcpu, "reserved": 0, "allocation_ratio": 1.0},
                "VCPU": {"total": 0, "reserved": 0, "allocation_ratio": 1.0},
                "MEMORY_MB": {"total": 786432, "reserved": 0, "allocation_ratio": 1.0},
            }
        }


class FakeCompute:
    def hypervisors(self, details=True):
        del details
        return [
            {
                "hypervisor_hostname": "compute-0",
                "state": "up",
                "status": "enabled",
                "vcpus": 0,
                "memory_mb": 786432,
            },
            {
                "hypervisor_hostname": "compute-1",
                "state": "up",
                "status": "enabled",
                "vcpus": 0,
                "memory_mb": 786432,
            },
            {
                "hypervisor_hostname": "compute-2",
                "state": "up",
                "status": "enabled",
                "vcpus": 0,
                "memory_mb": 786432,
            },
        ]


class FakeNetwork:
    def find_network(self, name):
        return {
            "id": "net-0",
            "name": name,
            "mtu": 8942,
            "provider:network_type": "geneve",
        }


class FakeBlockStorage:
    def backend_pools(self, details=True):
        del details
        return [
            {
                "name": "ceph#pool",
                "capabilities": {
                    "volume_backend_name": "ceph",
                    "total_capacity_gb": 1000,
                    "free_capacity_gb": 750,
                },
            }
        ]


class FakeConnection:
    placement = FakePlacement()
    compute = FakeCompute()
    network = FakeNetwork()
    block_storage = FakeBlockStorage()


def test_cloud_calibration_prefers_pcpu_when_vcpu_inventory_is_zero():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")

    calibration = discover_cloud_capacity(FakeConnection(), config)

    selected_cpu = calibration["compute"]["cpu"]["selected"]
    assert selected_cpu["resource_class"] == "PCPU"
    assert selected_cpu["allocatable"] == 1248
    assert calibration["generated_capacity"]["compute"]["vcpus"] == 1248
    assert calibration["generated_capacity"]["compute"]["cpu_resource_class"] == "PCPU"
    assert calibration["generated_capacity"]["compute"]["ram_gib"] == 2304


def test_cloud_calibration_preserves_config_assumptions_for_non_advertised_perf():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")

    calibration = discover_cloud_capacity(FakeConnection(), config)

    assert calibration["network"]["configured_capacity"]["gbps"] == 600
    assert calibration["storage"]["total_capacity_gib"] == 1000
    assert calibration["api"]["ops_per_second"] == 100


def test_cloud_calibration_can_use_configured_hugepage_memory_envelope():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.capacity.compute.per_host_ram_gib = 2048

    calibration = discover_cloud_capacity(FakeConnection(), config)

    memory = calibration["compute"]["memory"]
    assert memory["source"] == "configured_per_host_envelope"
    assert memory["per_host_ram_gib"] == 2048
    assert memory["allocatable_gib"] == 6144
    assert calibration["generated_capacity"]["compute"]["ram_gib"] == 6144
