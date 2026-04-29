from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from soak_tester.config import SoakConfig


class FlavorSpec(BaseModel):
    name: str
    vcpus: int
    ram_gib: float


class ComputePlan(BaseModel):
    target_vcpus: float
    target_ram_gib: float
    worker_count: int
    worker_flavor: str
    planned_vcpus: int
    planned_ram_gib: float


class NetworkPlan(BaseModel):
    target_gbps: float
    receiver_count: int
    sender_count: int
    flows: int
    target_gbps_per_flow: float
    parallel_streams_per_flow: int


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
    compute_flavor = flavor_specs[config.flavors.compute_worker]
    target_vcpus = config.capacity.compute.vcpus * target_percent / 100
    target_ram_gib = config.capacity.compute.ram_gib * target_percent / 100
    by_cpu = math.floor(target_vcpus / compute_flavor.vcpus)
    by_ram = math.floor(target_ram_gib / compute_flavor.ram_gib)
    compute_workers = max(0, min(by_cpu, by_ram)) if config.workloads.compute.enabled else 0

    target_gbps = config.capacity.network.gbps * target_percent / 100
    per_flow = config.workloads.network.target_gbps_per_flow
    flows = math.ceil(target_gbps / per_flow) if config.workloads.network.enabled and per_flow > 0 else 0
    receiver_count = math.ceil(flows / 2) if flows else 0
    sender_count = flows - receiver_count if flows else 0

    storage_workers = config.workloads.storage.worker_count if config.workloads.storage.enabled else 0

    return SoakPlan(
        run_id=run_id,
        target_percent=target_percent,
        compute=ComputePlan(
            target_vcpus=target_vcpus,
            target_ram_gib=target_ram_gib,
            worker_count=compute_workers,
            worker_flavor=config.flavors.compute_worker,
            planned_vcpus=compute_workers * compute_flavor.vcpus,
            planned_ram_gib=compute_workers * compute_flavor.ram_gib,
        ),
        network=NetworkPlan(
            target_gbps=target_gbps,
            receiver_count=receiver_count,
            sender_count=sender_count,
            flows=flows,
            target_gbps_per_flow=per_flow,
            parallel_streams_per_flow=config.workloads.network.parallel_streams_per_flow,
        ),
        storage=StoragePlan(
            target_read_gbps=config.capacity.storage.read_gbps * target_percent / 100,
            target_write_gbps=config.capacity.storage.write_gbps * target_percent / 100,
            target_iops=config.capacity.storage.iops * target_percent / 100,
            worker_count=storage_workers,
            volume_size_gib=config.workloads.storage.volume_size_gib,
            volume_type=config.workloads.storage.volume_type,
        ),
        api=ApiPlan(
            target_ops_per_second=config.capacity.api.ops_per_second * target_percent / 100,
            run_from=config.workloads.api.run_from,
            operation_mix=config.workloads.api.operation_mix,
        ),
    )


def write_plan(path: Path, plan: SoakPlan) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(plan.model_dump(mode="json"), sort_keys=False), encoding="utf-8")


def read_plan(path: Path) -> SoakPlan:
    return SoakPlan.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def default_flavor_specs(config: SoakConfig) -> dict[str, FlavorSpec]:
    names = set(config.flavors.model_dump().values())
    return {name: FlavorSpec(name=name, vcpus=1, ram_gib=1) for name in names}


def flavor_spec_from_sdk(flavor: Any) -> FlavorSpec:
    ram_mib = getattr(flavor, "ram", None) or flavor.get("ram")
    vcpus = getattr(flavor, "vcpus", None) or flavor.get("vcpus")
    name = getattr(flavor, "name", None) or flavor.get("name")
    return FlavorSpec(name=name, vcpus=int(vcpus), ram_gib=float(ram_mib) / 1024)
