from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from soak_tester.config import SoakConfig


class FlavorSpec(BaseModel):
    name: str
    vcpus: int = Field(gt=0)
    ram_gib: float = Field(gt=0)
    extra_specs: dict[str, str] = Field(default_factory=dict)


class ComputePlan(BaseModel):
    target_vcpus: float
    target_ram_gib: float
    worker_count: int
    worker_flavor: str | None
    planned_vcpus: int
    planned_ram_gib: float
    worker_groups: list[dict[str, float | int | str]] = Field(default_factory=list)


class NetworkPlan(BaseModel):
    scope: str = "aggregate"
    topology: str = "paired"
    host_count: int | None = None
    target_gbps: float
    offered_load: str = "target"
    offered_gbps: float | None = 0
    total_streams: int = 0
    target_gbps_per_host: float | None = None
    target_gbps_per_pair: float | None = None
    selected_hosts: list[str] = Field(default_factory=list)
    receiver_count: int
    sender_count: int
    receiver_worker_hosts: dict[str, str] = Field(default_factory=dict)
    sender_worker_hosts: dict[str, str] = Field(default_factory=dict)
    receiver_workers_per_host: int | None = None
    sender_workers_per_host: int | None = None
    flows: int
    target_gbps_per_flow: float
    parallel_streams_per_flow: int
    flow_assignments: list[NetworkFlowAssignment] = Field(default_factory=list)


class NetworkFlowAssignment(BaseModel):
    flow_id: str
    sender_worker_id: str
    receiver_worker_id: str
    receiver_private_ip: str | None = None
    receiver_port: int
    source_host: str | None = None
    dest_host: str | None = None
    target_gbps: float
    parallel_streams: int


class StoragePlan(BaseModel):
    target_read_gbps: float
    target_write_gbps: float
    target_iops: float
    worker_count: int
    volume_size_gib: int
    volume_type: str | None


class ApiPlan(BaseModel):
    target_ops_per_second: float
    run_from: str
    operation_mix: dict[str, int]


class SoakPlan(BaseModel):
    run_id: str
    target_percent: float
    compute: ComputePlan
    network: NetworkPlan
    storage: StoragePlan
    api: ApiPlan


def create_plan(
    config: SoakConfig,
    *,
    target_percent: float,
    flavor_specs: dict[str, FlavorSpec],
    run_id: str = "planned",
) -> SoakPlan:
    if not 0 < target_percent <= 100:
        raise ValueError("target_percent must be in (0, 100]")
    target_vcpus = (
        config.targets.compute.vcpus * target_percent / 100
        if config.workloads.compute.enabled
        else 0
    )
    target_ram_gib = (
        config.targets.compute.ram_gib * target_percent / 100
        if config.workloads.compute.enabled
        else 0
    )
    compute_plan = _create_compute_plan(
        config,
        target_vcpus=target_vcpus,
        target_ram_gib=target_ram_gib,
        flavor_specs=flavor_specs,
    )

    if config.workloads.compute.enabled and compute_plan.worker_count == 0:
        raise ValueError("Compute target cannot fit a worker of the selected flavor")
    network_plan = _create_network_plan(config, target_percent)
    if config.workloads.network.enabled:
        direction_count = 2 if config.workloads.network.bidirectional else 1
        network_plan.offered_load = config.workloads.network.offered_load
        network_plan.offered_gbps = (
            sum(f.target_gbps for f in network_plan.flow_assignments) * direction_count
            if network_plan.offered_load == "target"
            else None
        )
        network_plan.total_streams = (
            sum(f.parallel_streams for f in network_plan.flow_assignments) * direction_count
        )

    storage_enabled = config.workloads.storage.enabled
    storage_workers = config.workloads.storage.worker_count if storage_enabled else 0
    api_target = (
        config.targets.api.ops_per_second * target_percent / 100
        if config.workloads.api.enabled and config.targets.api is not None
        else 0
    )

    return SoakPlan(
        run_id=run_id,
        target_percent=target_percent,
        compute=compute_plan,
        network=network_plan,
        storage=StoragePlan(
            target_read_gbps=config.targets.storage.read_gbps * target_percent / 100
            if storage_enabled and config.targets.storage is not None
            else 0,
            target_write_gbps=config.targets.storage.write_gbps * target_percent / 100
            if storage_enabled and config.targets.storage is not None
            else 0,
            target_iops=config.targets.storage.iops * target_percent / 100
            if storage_enabled and config.targets.storage is not None
            else 0,
            worker_count=storage_workers,
            volume_size_gib=config.workloads.storage.volume_size_gib,
            volume_type=config.workloads.storage.volume_type,
        ),
        api=ApiPlan(
            target_ops_per_second=api_target,
            run_from="local",
            operation_mix=config.workloads.api.operation_mix,
        ),
    )


def write_plan(path: Path, plan: SoakPlan) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(plan.model_dump(mode="json"), sort_keys=False), encoding="utf-8")


def read_plan(path: Path) -> SoakPlan:
    return SoakPlan.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _create_compute_plan(
    config: SoakConfig,
    *,
    target_vcpus: float,
    target_ram_gib: float,
    flavor_specs: dict[str, FlavorSpec],
) -> ComputePlan:
    if not config.workloads.compute.enabled:
        return ComputePlan(
            target_vcpus=target_vcpus,
            target_ram_gib=target_ram_gib,
            worker_count=0,
            worker_flavor=config.flavors.compute_worker,
            planned_vcpus=0,
            planned_ram_gib=0,
        )
    flavor_names = config.workloads.compute.flavor_mix or [config.flavors.compute_worker]
    flavor_names = list(dict.fromkeys(flavor_names))
    explicit_worker_count = config.workloads.compute.worker_count
    if explicit_worker_count is not None:
        if len(flavor_names) != 1:
            raise ValueError("workloads.compute.worker_count requires exactly one compute flavor")
        compute_flavor = flavor_specs[flavor_names[0]]
        return ComputePlan(
            target_vcpus=target_vcpus,
            target_ram_gib=target_ram_gib,
            worker_count=explicit_worker_count,
            worker_flavor=compute_flavor.name,
            planned_vcpus=explicit_worker_count * compute_flavor.vcpus,
            planned_ram_gib=explicit_worker_count * compute_flavor.ram_gib,
            worker_groups=[_compute_worker_group(compute_flavor, explicit_worker_count)]
            if explicit_worker_count
            else [],
        )
    if len(flavor_names) == 1:
        compute_flavor = flavor_specs[flavor_names[0]]
        by_cpu = math.floor(target_vcpus / compute_flavor.vcpus)
        by_ram = math.floor(target_ram_gib / compute_flavor.ram_gib)
        compute_workers = max(0, min(by_cpu, by_ram))
        return ComputePlan(
            target_vcpus=target_vcpus,
            target_ram_gib=target_ram_gib,
            worker_count=compute_workers,
            worker_flavor=compute_flavor.name,
            planned_vcpus=compute_workers * compute_flavor.vcpus,
            planned_ram_gib=compute_workers * compute_flavor.ram_gib,
            worker_groups=[_compute_worker_group(compute_flavor, compute_workers)]
            if compute_workers
            else [],
        )
    specs = [flavor_specs[name] for name in flavor_names]
    counts = _best_compute_mix(specs, target_vcpus, target_ram_gib)
    groups = [
        _compute_worker_group(spec, count)
        for spec, count in zip(specs, counts, strict=True)
        if count
    ]
    return ComputePlan(
        target_vcpus=target_vcpus,
        target_ram_gib=target_ram_gib,
        worker_count=sum(counts),
        worker_flavor="mixed",
        planned_vcpus=sum(int(group["vcpus"]) for group in groups),
        planned_ram_gib=sum(float(group["ram_gib"]) for group in groups),
        worker_groups=groups,
    )


def _best_compute_mix(
    specs: list[FlavorSpec],
    target_vcpus: float,
    target_ram_gib: float,
) -> list[int]:
    max_counts = [
        max(
            math.floor(target_vcpus / spec.vcpus),
            math.floor(target_ram_gib / spec.ram_gib),
        )
        for spec in specs
    ]
    best_counts = [0 for _ in specs]
    best_key = (-1.0, -1.0, 0, 0.0)

    def visit(index: int, counts: list[int]) -> None:
        nonlocal best_counts, best_key
        if index == len(specs):
            vcpus = sum(count * spec.vcpus for count, spec in zip(counts, specs, strict=True))
            ram = sum(count * spec.ram_gib for count, spec in zip(counts, specs, strict=True))
            if vcpus > target_vcpus or ram > target_ram_gib:
                return
            cpu_ratio = vcpus / target_vcpus if target_vcpus else 1
            ram_ratio = ram / target_ram_gib if target_ram_gib else 1
            balance = min(cpu_ratio, ram_ratio)
            utilization = cpu_ratio + ram_ratio
            worker_penalty = -sum(counts)
            ram_shortfall = -abs(target_ram_gib - ram)
            key = (balance, utilization, worker_penalty, ram_shortfall)
            if key > best_key:
                best_key = key
                best_counts = counts.copy()
            return
        for count in range(max_counts[index] + 1):
            counts.append(count)
            visit(index + 1, counts)
            counts.pop()

    visit(0, [])
    return best_counts


def _compute_worker_group(spec: FlavorSpec, count: int) -> dict[str, float | int | str]:
    return {
        "flavor": spec.name,
        "count": count,
        "vcpus": count * spec.vcpus,
        "ram_gib": count * spec.ram_gib,
    }


def _create_network_plan(config: SoakConfig, target_percent: float) -> NetworkPlan:
    workload = config.workloads.network
    per_flow = workload.target_gbps_per_flow
    if not workload.enabled or per_flow <= 0:
        return NetworkPlan(
            target_gbps=0,
            receiver_count=0,
            sender_count=0,
            flows=0,
            target_gbps_per_flow=per_flow,
            parallel_streams_per_flow=workload.parallel_streams_per_flow,
        )
    if config.targets.network.scope == "per_host":
        return _create_per_host_network_plan(config, target_percent)
    target_gbps = config.targets.network.gbps * target_percent / 100
    flows = math.ceil(target_gbps / per_flow)
    receiver_count = math.ceil(flows / 2)
    sender_count = max(1, flows - receiver_count)
    _check_network_budget(workload, flows, sender_count + receiver_count)
    return NetworkPlan(
        scope="aggregate",
        topology=workload.topology,
        target_gbps=target_gbps,
        receiver_count=receiver_count,
        sender_count=sender_count,
        flows=flows,
        target_gbps_per_flow=per_flow,
        parallel_streams_per_flow=workload.parallel_streams_per_flow,
        flow_assignments=_paired_flow_assignments(
            sender_count=sender_count,
            receiver_count=receiver_count,
            flows=flows,
            port_base=workload.receiver_port_base,
            target_gbps_per_flow=per_flow,
            parallel_streams=workload.parallel_streams_per_flow,
        ),
    )


def _create_per_host_network_plan(config: SoakConfig, target_percent: float) -> NetworkPlan:
    workload = config.workloads.network
    host_names = workload.hosts or []
    host_count = len(host_names)
    if host_count is None or host_count < 2:
        raise ValueError(
            "per-host network planning requires at least two explicit or discovered hosts"
        )
    if workload.topology != "full_mesh":
        raise ValueError("per-host network planning requires workloads.network.topology: full_mesh")
    per_host_capacity = config.targets.network.gbps
    target_per_host = per_host_capacity * target_percent / 100
    target_per_pair = target_per_host / (host_count - 1)
    flows_per_pair = math.ceil(target_per_pair / workload.target_gbps_per_flow)
    sender_workers_per_host = math.ceil(target_per_host / workload.sender_worker_target_gbps)
    receiver_workers_per_host = math.ceil(target_per_host / workload.receiver_worker_target_gbps)
    sender_count = sender_workers_per_host * host_count
    receiver_count = receiver_workers_per_host * host_count
    flows = host_count * (host_count - 1) * flows_per_pair
    workers = sender_count + receiver_count
    _check_network_budget(workload, flows, workers)
    assignments = _full_mesh_flow_assignments(
        host_names=host_names,
        sender_workers_per_host=sender_workers_per_host,
        receiver_workers_per_host=receiver_workers_per_host,
        flows_per_pair=flows_per_pair,
        port_base=workload.receiver_port_base,
        target_gbps_per_flow=workload.target_gbps_per_flow,
        parallel_streams=workload.parallel_streams_per_flow,
    )
    return NetworkPlan(
        scope="per_host",
        topology="full_mesh",
        host_count=host_count,
        selected_hosts=host_names,
        target_gbps=target_per_host * host_count,
        target_gbps_per_host=target_per_host,
        target_gbps_per_pair=target_per_pair,
        receiver_count=receiver_count,
        sender_count=sender_count,
        receiver_worker_hosts=_worker_hosts(
            "network-receiver",
            host_names,
            receiver_workers_per_host,
        ),
        sender_worker_hosts=_worker_hosts(
            "network-sender",
            host_names,
            sender_workers_per_host,
        ),
        receiver_workers_per_host=receiver_workers_per_host,
        sender_workers_per_host=sender_workers_per_host,
        flows=flows,
        target_gbps_per_flow=workload.target_gbps_per_flow,
        parallel_streams_per_flow=workload.parallel_streams_per_flow,
        flow_assignments=assignments,
    )


def _paired_flow_assignments(
    *,
    sender_count: int,
    receiver_count: int,
    flows: int,
    port_base: int,
    target_gbps_per_flow: float,
    parallel_streams: int,
) -> list[NetworkFlowAssignment]:
    return [
        NetworkFlowAssignment(
            flow_id=f"network-flow-{index:05d}",
            sender_worker_id=f"network-sender-{index % sender_count:03d}",
            receiver_worker_id=f"network-receiver-{index % receiver_count:03d}",
            receiver_port=port_base + index,
            target_gbps=target_gbps_per_flow,
            parallel_streams=parallel_streams,
        )
        for index in range(flows)
    ]


def _full_mesh_flow_assignments(
    *,
    host_names: list[str],
    sender_workers_per_host: int,
    receiver_workers_per_host: int,
    flows_per_pair: int,
    port_base: int,
    target_gbps_per_flow: float,
    parallel_streams: int,
) -> list[NetworkFlowAssignment]:
    assignments: list[NetworkFlowAssignment] = []
    flow_index = 0
    host_count = len(host_names)
    for source_host_index in range(host_count):
        for dest_host_index in range(host_count):
            if source_host_index == dest_host_index:
                continue
            for pair_flow_index in range(flows_per_pair):
                sender_index = (
                    source_host_index * sender_workers_per_host
                    + pair_flow_index % sender_workers_per_host
                )
                receiver_index = (
                    dest_host_index * receiver_workers_per_host
                    + pair_flow_index % receiver_workers_per_host
                )
                assignments.append(
                    NetworkFlowAssignment(
                        flow_id=f"network-flow-{flow_index:05d}",
                        sender_worker_id=f"network-sender-{sender_index:03d}",
                        receiver_worker_id=f"network-receiver-{receiver_index:03d}",
                        receiver_port=port_base + flow_index,
                        source_host=host_names[source_host_index],
                        dest_host=host_names[dest_host_index],
                        target_gbps=target_gbps_per_flow,
                        parallel_streams=parallel_streams,
                    )
                )
                flow_index += 1
    return assignments


def _worker_hosts(prefix: str, host_names: list[str], workers_per_host: int) -> dict[str, str]:
    result = {}
    for host_index, host_name in enumerate(host_names):
        for worker_index in range(workers_per_host):
            index = host_index * workers_per_host + worker_index
            result[f"{prefix}-{index:03d}"] = host_name
    return result


def flavor_spec_from_sdk(flavor: Any) -> FlavorSpec:
    ram_mib = getattr(flavor, "ram", None) or flavor.get("ram")
    vcpus = getattr(flavor, "vcpus", None) or flavor.get("vcpus")
    name = getattr(flavor, "name", None) or flavor.get("name")
    return FlavorSpec(
        name=name,
        vcpus=int(vcpus),
        ram_gib=float(ram_mib) / 1024,
        extra_specs=getattr(flavor, "extra_specs", None) or {},
    )


def _check_network_budget(workload, flows: int, workers: int) -> None:
    if flows > workload.max_flows:
        raise ValueError(
            f"network plan requires {flows} flows, above max_flows={workload.max_flows}"
        )
    if workers > workload.max_workers:
        raise ValueError(
            f"network plan requires {workers} workers, above max_workers={workload.max_workers}"
        )
    if workload.receiver_port_base + flows - 1 > 65535:
        raise ValueError("network flow receiver ports exceed 65535")
