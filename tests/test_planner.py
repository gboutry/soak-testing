from pathlib import Path

from soak_tester.config import load_config
from soak_tester.planner import FlavorSpec, create_plan


def test_planner_converts_target_to_resource_counts():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    flavors = {
        "m1.soak-compute": FlavorSpec(name="m1.soak-compute", vcpus=8, ram_gib=16),
        "m1.soak-net": FlavorSpec(name="m1.soak-net", vcpus=4, ram_gib=8),
        "m1.soak-fio": FlavorSpec(name="m1.soak-fio", vcpus=4, ram_gib=8),
        "m1.soak-controller": FlavorSpec(name="m1.soak-controller", vcpus=2, ram_gib=4),
        "m1.soak-api": FlavorSpec(name="m1.soak-api", vcpus=2, ram_gib=4),
    }

    plan = create_plan(config, target_percent=80, flavor_specs=flavors)

    assert plan.target_percent == 80
    assert plan.compute.target_vcpus == 768
    assert plan.compute.target_ram_gib == 2457.6
    assert plan.compute.worker_count == 96
    assert plan.network.target_gbps == 480
    assert plan.network.flows == 48
    assert plan.network.receiver_count == 24
    assert plan.network.sender_count == 24
    assert plan.storage.target_iops == 400000
    assert plan.api.target_ops_per_second == 80
