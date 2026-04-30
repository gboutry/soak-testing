from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from soak_tester.util import parse_duration


class OpenStackConfig(BaseModel):
    external_network: str
    image: str
    ssh_user: str = "ubuntu"
    member_role: str = "member"
    admin_role: str = "admin"
    user_domain_name: str = "Default"
    project_domain_name: str = "Default"


class ManagementConfig(BaseModel):
    mode: Literal["floating_ip", "private"] = "floating_ip"
    controller_floating_ip: bool = True
    allowed_cidrs: list[str] = Field(default_factory=lambda: ["0.0.0.0/0"])
    ssh_private_key_path: str | None = None
    controller_status_port: int = 8080


class FloatingIPConfig(BaseModel):
    controller: bool = True
    workers: bool = False


class ResourcesConfig(BaseModel):
    project_prefix: str = "soak-tester"
    create_project: bool = True
    create_project_user: bool = True
    create_router: bool = True
    network_cidr: str = "10.250.0.0/24"
    floating_ips: FloatingIPConfig = Field(default_factory=FloatingIPConfig)


class FlavorsConfig(BaseModel):
    controller: str
    compute_worker: str
    network_worker: str
    storage_worker: str
    api_worker: str


class ComputeCapacity(BaseModel):
    vcpus: int
    ram_gib: float
    cpu_resource_class: Literal["auto", "VCPU", "PCPU"] = "auto"
    ram_resource_class: str = "MEMORY_MB"
    per_host_ram_gib: float | None = None


class NetworkCapacity(BaseModel):
    gbps: float
    scope: Literal["aggregate", "per_host"] = "aggregate"
    per_host_gbps: float | None = None


class StorageCapacity(BaseModel):
    read_gbps: float
    write_gbps: float
    iops: int


class ApiCapacity(BaseModel):
    ops_per_second: float


class CapacityConfig(BaseModel):
    compute: ComputeCapacity
    network: NetworkCapacity
    storage: StorageCapacity
    api: ApiCapacity


class ApiWorkloadConfig(BaseModel):
    enabled: bool = True
    engine: Literal["native", "rally"] = "native"
    run_from: Literal["local", "controller"] = "local"
    operation_mix: dict[str, int] = Field(default_factory=dict)
    create_delete_volume_size_gib: int = 1
    rally_concurrency: int = Field(default=1, ge=1)
    rally_times: int | None = Field(default=None, ge=1)
    rally_include_create_delete: bool = False
    rally_error_rate_max: float = Field(default=0.0, ge=0, le=100)

    @field_validator("operation_mix")
    @classmethod
    def validate_operation_mix(cls, value: dict[str, int]) -> dict[str, int]:
        allowed = {
            "list_servers",
            "list_ports",
            "list_volumes",
            "show_server",
            "show_volume",
            "create_delete_ports",
            "create_delete_volumes",
        }
        unsupported = sorted(set(value) - allowed)
        if unsupported:
            raise ValueError(f"unsupported API operations: {', '.join(unsupported)}")
        non_positive = [name for name, weight in value.items() if not isinstance(weight, int) or weight <= 0]
        if non_positive:
            raise ValueError("API operation weights must be positive integer values")
        return value


class ComputeWorkloadConfig(BaseModel):
    enabled: bool = True
    cpu_load_percent: int = 95
    memory_load_percent: int = 70
    worker_count: int | None = Field(default=None, ge=0)
    flavor_mix: list[str] = Field(default_factory=list)


class NetworkWorkloadConfig(BaseModel):
    enabled: bool = True
    protocol: Literal["tcp"] = "tcp"
    tool: Literal["iperf3"] = "iperf3"
    topology: Literal["paired", "full_mesh"] = "paired"
    flow_duration: str = "60s"
    parallel_streams_per_flow: int = 4
    target_gbps_per_flow: float = 10
    sender_worker_target_gbps: float = 10
    receiver_worker_target_gbps: float = 10
    hosts: list[str] | None = None
    host_count: int | None = None
    receiver_port_base: int = 5201
    max_flows: int = 10000
    max_workers: int = 1000

    @field_validator("flow_duration")
    @classmethod
    def validate_flow_duration(cls, value: str) -> str:
        parse_duration(value)
        return value


class StorageWorkloadConfig(BaseModel):
    enabled: bool = True
    volume_type: str | None = None
    volume_size_gib: int = 100
    profile: str = "mixed-randrw"
    block_size: str = "4k"
    iodepth: int = 32
    numjobs: int = 4
    rwmixread: int = 70
    worker_count: int = 1


class WorkloadsConfig(BaseModel):
    api: ApiWorkloadConfig = Field(default_factory=ApiWorkloadConfig)
    compute: ComputeWorkloadConfig = Field(default_factory=ComputeWorkloadConfig)
    network: NetworkWorkloadConfig = Field(default_factory=NetworkWorkloadConfig)
    storage: StorageWorkloadConfig = Field(default_factory=StorageWorkloadConfig)


class LiveStatusConfig(BaseModel):
    enabled: bool = True
    refresh_interval: str = "5s"


class WorkerMetricsConfig(BaseModel):
    enabled: bool = True
    interval: str = "5s"


class MonitoringConfig(BaseModel):
    live_status: LiveStatusConfig = Field(default_factory=LiveStatusConfig)
    worker_metrics: WorkerMetricsConfig = Field(default_factory=WorkerMetricsConfig)


class PrometheusAuthConfig(BaseModel):
    type: Literal["none", "bearer", "basic"] = "none"
    token_file: str | None = None
    username: str | None = None
    password_env: str | None = None


class PrometheusTLSConfig(BaseModel):
    verify: bool = True
    ca_file: str | None = None


class PrometheusLabelsConfig(BaseModel):
    host_label: str = "instance"
    hostname_label: str = "hostname"


class PrometheusNetworkConfig(BaseModel):
    enabled: bool = True
    device_regex: str = "bond.*|eno.*|ens.*|eth.*"
    exclude_device_regex: str = "lo|docker.*|veth.*|tap.*|qbr.*|qvo.*|qvb.*"


class PrometheusConfig(BaseModel):
    enabled: bool = False
    url: str = "http://prometheus.example.internal:9090"
    timeout: str = "30s"
    query_step: str = "15s"
    auth: PrometheusAuthConfig = Field(default_factory=PrometheusAuthConfig)
    tls: PrometheusTLSConfig = Field(default_factory=PrometheusTLSConfig)
    labels: PrometheusLabelsConfig = Field(default_factory=PrometheusLabelsConfig)
    network: PrometheusNetworkConfig = Field(default_factory=PrometheusNetworkConfig)
    query_packs: list[str] = Field(default_factory=lambda: ["node-exporter-network"])

    @field_validator("timeout", "query_step")
    @classmethod
    def validate_duration(cls, value: str) -> str:
        parse_duration(value)
        return value


class ObservabilityConfig(BaseModel):
    enabled: bool = False
    prometheus: PrometheusConfig = Field(default_factory=PrometheusConfig)


class CalibrationConfig(BaseModel):
    pass_ratio: float = Field(default=0.95, gt=0, le=1)
    ramp_duration: str = "3m"
    validation_duration: str = "10m"
    derate_policy: Literal["auto_once", "fail"] = "auto_once"
    network_direction: Literal["egress"] = "egress"

    @field_validator("ramp_duration", "validation_duration")
    @classmethod
    def validate_duration(cls, value: str) -> str:
        parse_duration(value)
        return value


class RunConfig(BaseModel):
    warmup: str = "5m"
    cooldown: str = "2m"
    failure_budget: int = 5
    stop_on_error: bool = False
    leave_running: bool = False
    cleanup_on_success: bool = True

    @field_validator("warmup", "cooldown")
    @classmethod
    def validate_duration(cls, value: str) -> str:
        parse_duration(value)
        return value


class SoakConfig(BaseModel):
    openstack: OpenStackConfig
    boot_concurrency: int = Field(default=1, ge=1)
    management: ManagementConfig = Field(default_factory=ManagementConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    flavors: FlavorsConfig
    capacity: CapacityConfig
    workloads: WorkloadsConfig = Field(default_factory=WorkloadsConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
    run: RunConfig = Field(default_factory=RunConfig)


def load_config(path: str | Path) -> SoakConfig:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return SoakConfig.model_validate(payload)


def dump_config(config: SoakConfig) -> str:
    return yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
