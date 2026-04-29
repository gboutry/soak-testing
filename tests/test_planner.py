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


def test_planner_models_per_host_full_mesh_network_capacity():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.capacity.network.scope = "per_host"
    config.capacity.network.per_host_gbps = 200
    config.workloads.network.topology = "full_mesh"
    config.workloads.network.target_gbps_per_flow = 5
    config.workloads.network.sender_worker_target_gbps = 20
    config.workloads.network.receiver_worker_target_gbps = 20
    config.workloads.network.host_count = 3
    flavors = {
        "m1.soak-compute": FlavorSpec(name="m1.soak-compute", vcpus=8, ram_gib=16),
        "m1.soak-net": FlavorSpec(name="m1.soak-net", vcpus=4, ram_gib=8),
        "m1.soak-fio": FlavorSpec(name="m1.soak-fio", vcpus=4, ram_gib=8),
        "m1.soak-controller": FlavorSpec(name="m1.soak-controller", vcpus=2, ram_gib=4),
        "m1.soak-api": FlavorSpec(name="m1.soak-api", vcpus=2, ram_gib=4),
    }

    plan = create_plan(config, target_percent=80, flavor_specs=flavors)

    assert plan.network.scope == "per_host"
    assert plan.network.target_gbps_per_host == 160
    assert plan.network.target_gbps == 480
    assert plan.network.target_gbps_per_pair == 80
    assert plan.network.receiver_count == 24
    assert plan.network.sender_count == 24
    assert plan.network.flows == 96
    assert {flow.source_host for flow in plan.network.flow_assignments} == {
        "host-000",
        "host-001",
        "host-002",
    }
    assert {flow.dest_host for flow in plan.network.flow_assignments} == {
        "host-000",
        "host-001",
        "host-002",
    }
    assert all(flow.source_host != flow.dest_host for flow in plan.network.flow_assignments)
    assert len({flow.receiver_port for flow in plan.network.flow_assignments}) == 96


def test_planner_uses_all_receivers_in_aggregate_network_plan():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.capacity.network.gbps = 200
    config.workloads.network.target_gbps_per_flow = 5
    flavors = {
        "m1.soak-compute": FlavorSpec(name="m1.soak-compute", vcpus=8, ram_gib=16),
        "m1.soak-net": FlavorSpec(name="m1.soak-net", vcpus=4, ram_gib=8),
        "m1.soak-fio": FlavorSpec(name="m1.soak-fio", vcpus=4, ram_gib=8),
        "m1.soak-controller": FlavorSpec(name="m1.soak-controller", vcpus=2, ram_gib=4),
        "m1.soak-api": FlavorSpec(name="m1.soak-api", vcpus=2, ram_gib=4),
    }

    plan = create_plan(config, target_percent=25, flavor_specs=flavors)

    receivers = {flow.receiver_worker_id for flow in plan.network.flow_assignments}
    assert receivers == {f"network-receiver-{index:03d}" for index in range(plan.network.receiver_count)}
    assert len({flow.receiver_port for flow in plan.network.flow_assignments}) == plan.network.flows


def test_planner_can_mix_compute_flavors_to_match_cpu_and_memory_targets():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.capacity.compute.vcpus = 1200
    config.capacity.compute.ram_gib = 2048
    config.flavors.compute_worker = "m1.benchmark-big"
    config.workloads.compute.flavor_mix = ["m1.benchmark-big", "m1.benchmark-4c"]
    flavors = {
        "m1.benchmark-big": FlavorSpec(name="m1.benchmark-big", vcpus=16, ram_gib=32),
        "m1.benchmark-4c": FlavorSpec(name="m1.benchmark-4c", vcpus=4, ram_gib=2),
        "m1.soak-net": FlavorSpec(name="m1.soak-net", vcpus=4, ram_gib=8),
        "m1.soak-fio": FlavorSpec(name="m1.soak-fio", vcpus=4, ram_gib=8),
        "m1.soak-controller": FlavorSpec(name="m1.soak-controller", vcpus=2, ram_gib=4),
        "m1.soak-api": FlavorSpec(name="m1.soak-api", vcpus=2, ram_gib=4),
    }

    plan = create_plan(config, target_percent=80, flavor_specs=flavors)

    assert plan.compute.target_vcpus == 960
    assert plan.compute.target_ram_gib == 1638.4
    assert plan.compute.planned_vcpus == 960
    assert plan.compute.planned_ram_gib == 1632
    assert plan.compute.worker_groups == [
        {"flavor": "m1.benchmark-big", "count": 48, "vcpus": 768, "ram_gib": 1536},
        {"flavor": "m1.benchmark-4c", "count": 48, "vcpus": 192, "ram_gib": 96},
    ]
