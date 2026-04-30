from pathlib import Path

import json

from soak_tester.calibration import build_calibration_profile, discover_cloud_capacity
from soak_tester.config import load_config
from soak_tester.planner import FlavorSpec


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


def test_profile_uses_feasible_flavor_packing_as_exercisable_compute_capacity():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.capacity.compute.per_host_ram_gib = 2048
    config.workloads.compute.flavor_mix = ["m1.big", "m1.small"]
    cloud = discover_cloud_capacity(FakeConnection(), config)
    flavors = _flavors()

    profile, tuned_config = build_calibration_profile(
        config=config,
        cloud_calibration=cloud,
        flavor_specs=flavors,
        target_percent=80,
    )

    assert profile["inventory_capacity"]["compute"]["vcpus"] == 1248
    assert profile["exercisable_capacity"]["compute"]["vcpus"] <= 1248
    assert profile["exercisable_capacity"]["compute"]["ram_gib"] <= 6144
    assert tuned_config["capacity"]["compute"]["cpu_resource_class"] == "PCPU"
    assert tuned_config["workloads"]["api"]["run_from"] == "controller"


def test_profile_derates_missed_validation_planes_once(tmp_path):
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.capacity.network.scope = "per_host"
    config.capacity.network.per_host_gbps = 200
    config.workloads.network.topology = "full_mesh"
    config.workloads.network.host_count = 3
    cloud = discover_cloud_capacity(FakeConnection(), config)
    report_dir = tmp_path / "validation"
    report_dir.mkdir()
    (report_dir / "final-report.json").write_text(
        json.dumps(
            {
                "achieved": {
                    "iperf.goodput_gbps_avg": 240,
                    "fio.read_gbps_avg": 80,
                    "fio.write_gbps_avg": 40,
                    "fio.read_iops_avg": 250000,
                    "api.success_ops_per_second_avg": 60,
                    "api.errors_avg": 0,
                }
            }
        ),
        encoding="utf-8",
    )

    profile, tuned_config = build_calibration_profile(
        config=config,
        cloud_calibration=cloud,
        flavor_specs=_flavors(),
        target_percent=80,
        validation_runs=[report_dir],
    )

    assert profile["plane_results"]["network"]["status"] == "missed"
    assert profile["exercisable_capacity"]["network"]["gbps"] == 300
    assert profile["exercisable_capacity"]["network"]["per_host_gbps"] == 100
    assert profile["exercisable_capacity"]["api"]["ops_per_second"] == 75
    assert tuned_config["capacity"]["network"]["gbps"] == 300
    assert tuned_config["capacity"]["network"]["per_host_gbps"] == 100


def _flavors() -> dict[str, FlavorSpec]:
    return {
        "m1.soak-compute": FlavorSpec(name="m1.soak-compute", vcpus=8, ram_gib=16),
        "m1.soak-net": FlavorSpec(name="m1.soak-net", vcpus=4, ram_gib=8),
        "m1.soak-fio": FlavorSpec(name="m1.soak-fio", vcpus=4, ram_gib=8),
        "m1.soak-controller": FlavorSpec(name="m1.soak-controller", vcpus=2, ram_gib=4),
        "m1.soak-api": FlavorSpec(name="m1.soak-api", vcpus=2, ram_gib=4),
        "m1.big": FlavorSpec(name="m1.big", vcpus=16, ram_gib=64),
        "m1.small": FlavorSpec(name="m1.small", vcpus=4, ram_gib=8),
    }
