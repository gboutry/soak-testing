from __future__ import annotations

from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from ipaddress import ip_network
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, FilePath, field_validator, model_validator

from soak_tester.util import parse_duration


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class OpenStackConfig(ConfigModel):
    external_network: str | None = None
    image: str
    member_role: str = "member"
    admin_role: str = "admin"
    user_domain_name: str = "Default"
    project_domain_name: str = "Default"


class ManagementConfig(ConfigModel):
    allowed_cidrs: list[str] = Field(min_length=1)

    @field_validator("allowed_cidrs")
    @classmethod
    def validate_cidrs(cls, values):
        for value in values:
            if ip_network(value).version != 4:
                raise ValueError("management ingress currently supports IPv4 CIDRs")
        return values

    controller_status_port: int = Field(default=8080, ge=1, le=65535)


class FloatingIPConfig(ConfigModel):
    controller: bool = True


class ResourcesConfig(ConfigModel):
    project_prefix: str = "soak-tester"
    create_router: bool = True
    network_cidr: str = "10.250.0.0/24"
    network_mtu: int | None = Field(default=None, ge=576)
    floating_ips: FloatingIPConfig = Field(default_factory=FloatingIPConfig)


class FlavorsConfig(ConfigModel):
    controller: str
    compute_worker: str | None = None
    network_worker: str | None = None
    storage_worker: str | None = None


class ComputeCapacity(ConfigModel):
    vcpus: int = Field(gt=0)
    ram_gib: float = Field(gt=0)


class NetworkCapacity(ConfigModel):
    gbps: float = Field(gt=0)
    scope: Literal["aggregate", "per_host"] = "aggregate"


class StorageCapacity(ConfigModel):
    read_gbps: float = Field(default=0, ge=0)
    write_gbps: float = Field(default=0, ge=0)
    iops: int = Field(default=0, ge=0)


class ApiCapacity(ConfigModel):
    ops_per_second: float = Field(gt=0)


class TargetsConfig(ConfigModel):
    compute: ComputeCapacity | None = None
    network: NetworkCapacity | None = None
    storage: StorageCapacity | None = None
    api: ApiCapacity | None = None


class ApiWorkloadConfig(ConfigModel):
    enabled: bool = True
    engine: Literal["native", "rally"] = "native"
    operation_mix: dict[str, int] = Field(default_factory=dict)
    create_delete_volume_size_gib: int = Field(default=1, ge=1)
    rally_mode: Literal["single_task", "saturation"] = "saturation"
    rally_concurrency: int = Field(default=1, ge=1)
    rally_error_rate_max: float = Field(default=0.0, ge=0, le=100)

    @model_validator(mode="after")
    def validate_engine(self) -> ApiWorkloadConfig:
        if not self.enabled:
            return self
        incompatible = (
            {key for key in self.model_fields_set if key.startswith("rally_")}
            if self.engine == "native"
            else self.model_fields_set & {"operation_mix", "create_delete_volume_size_gib"}
        )
        if (
            self.engine == "native"
            and "create_delete_volume_size_gib" in self.model_fields_set
            and "create_delete_volumes" not in self.operation_mix
        ):
            raise ValueError(
                "create_delete_volume_size_gib requires create_delete_volumes in operation_mix"
            )
        if incompatible:
            raise ValueError(
                f"inapplicable to {self.engine} API engine: {', '.join(sorted(incompatible))}"
            )
        return self

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
        non_positive = [
            name for name, weight in value.items() if not isinstance(weight, int) or weight <= 0
        ]
        if non_positive:
            raise ValueError("API operation weights must be positive integer values")
        return value


class ComputeWorkloadConfig(ConfigModel):
    enabled: bool = True
    cpu_load_percent: int = Field(default=95, ge=1, le=100)
    memory_load_percent: int = Field(default=70, ge=1, le=100)
    worker_count: int | None = Field(default=None, ge=1)
    flavor_mix: list[str] = Field(default_factory=list)


class NetworkWorkloadConfig(ConfigModel):
    enabled: bool = True
    protocol: Literal["tcp", "udp"] = "tcp"
    udp_datagram_size: int | None = Field(default=None, ge=1, le=65507)
    capture_receiver_intervals: bool = False
    guest_interrupt_coalesce_usecs: int | None = Field(default=None, ge=0, le=1000)
    guest_large_receive_offload: bool | None = None
    guest_tx_qdisc: Literal["mq_fq"] | None = None
    vnic_type: Literal["normal", "direct"] = "normal"
    guest_combined_queues: int | None = Field(default=None, ge=1)
    guest_rx_ring_size: int | None = Field(default=None, ge=1)
    hardware_offload_type: Literal["switchdev"] | None = None
    port_security_enabled: bool = True
    offered_load: Literal["target", "unlimited"] = "target"
    connect_timeout_ms: int | None = Field(default=None, ge=1)
    pacing_timer_us: int | None = Field(default=None, ge=1)
    topology: Literal["paired", "full_mesh"] = "paired"
    flow_duration: str = "60s"
    omit_seconds: int = Field(default=0, ge=0)
    parallel_streams_per_flow: int = Field(default=4, ge=1)
    target_gbps_per_flow: float = Field(default=10, gt=0)
    sender_worker_target_gbps: float = Field(default=10, gt=0)
    receiver_worker_target_gbps: float = Field(default=10, gt=0)
    tcp_window: str | None = None
    zerocopy: bool = False
    reverse: bool = False
    bidirectional: bool = False
    hosts: list[str] | None = None
    receiver_port_base: int = Field(default=5201, ge=1, le=65535)
    max_flows: int = Field(default=10000, ge=1)
    max_workers: int = Field(default=1000, ge=2)

    @field_validator("flow_duration")
    @classmethod
    def validate_flow_duration(cls, value: str) -> str:
        if parse_duration(value).total_seconds() <= 0:
            raise ValueError("duration must be positive")
        return value

    @model_validator(mode="after")
    def validate_direction(self) -> NetworkWorkloadConfig:
        if self.guest_large_receive_offload is not None and self.protocol != "tcp":
            raise ValueError("guest_large_receive_offload requires TCP")
        if self.udp_datagram_size is not None and self.protocol != "udp":
            raise ValueError("udp_datagram_size requires UDP")
        if self.protocol == "udp" and (self.reverse or self.bidirectional):
            raise ValueError(
                "UDP currently requires forward flows; use opposite host pairs for both directions"
            )
        if self.hosts is not None and (
            len(self.hosts) < 2 or len(set(self.hosts)) != len(self.hosts)
        ):
            raise ValueError("hosts must contain at least two unique hostnames")
        if self.hardware_offload_type is not None and self.vnic_type != "direct":
            raise ValueError("hardware_offload_type requires a direct interface")
        if self.guest_combined_queues is not None and self.vnic_type != "normal":
            raise ValueError("guest_combined_queues requires a normal virtio interface")
        if self.guest_rx_ring_size is not None and self.vnic_type != "normal":
            raise ValueError("guest_rx_ring_size requires a normal virtio interface")
        if self.vnic_type == "normal" and "port_security_enabled" in self.model_fields_set:
            raise ValueError("port_security_enabled override requires a direct interface")
        if self.tcp_window is not None:
            import re

            if not re.fullmatch(r"[1-9][0-9]*[KMGkmg]?", self.tcp_window):
                raise ValueError("tcp_window must be a positive size such as 16M")
        if self.reverse and self.bidirectional:
            raise ValueError("workloads.network.reverse and bidirectional are mutually exclusive")
        return self


class StorageWorkloadConfig(ConfigModel):
    enabled: bool = True
    volume_type: str | None = None
    volume_size_gib: int = Field(default=100, ge=1)
    profile: Literal[
        "mixed-randrw", "random-read", "random-write", "sequential-read", "sequential-write"
    ] = "mixed-randrw"
    block_size: str = "4k"
    iodepth: int = Field(default=32, ge=1)
    numjobs: int = Field(default=4, ge=1)
    rwmixread: int = Field(default=70, ge=0, le=100)
    worker_count: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_profile(self):
        if self.enabled and self.profile != "mixed-randrw" and "rwmixread" in self.model_fields_set:
            raise ValueError("rwmixread applies only to mixed-randrw")
        return self


class WorkloadsConfig(ConfigModel):
    api: ApiWorkloadConfig = Field(default_factory=ApiWorkloadConfig)
    compute: ComputeWorkloadConfig = Field(default_factory=ComputeWorkloadConfig)
    network: NetworkWorkloadConfig = Field(default_factory=NetworkWorkloadConfig)
    storage: StorageWorkloadConfig = Field(default_factory=StorageWorkloadConfig)


class PrometheusAuthConfig(ConfigModel):
    type: Literal["none", "bearer", "basic"] = "none"
    token_file: str | None = None
    username: str | None = None
    password_env: str | None = None

    @model_validator(mode="after")
    def validate_auth(self) -> PrometheusAuthConfig:
        required = {"none": set(), "bearer": {"token_file"}, "basic": {"username", "password_env"}}[
            self.type
        ]
        provided = {k for k in ["token_file", "username", "password_env"] if getattr(self, k)}
        if provided != required:
            raise ValueError(f"{self.type} auth requires exactly {sorted(required)}")
        return self


class PrometheusTLSConfig(ConfigModel):
    verify: bool = True
    ca_file: str | None = None

    @model_validator(mode="after")
    def validate_tls(self):
        if not self.verify and self.ca_file:
            raise ValueError("ca_file requires TLS verification")
        return self


class PrometheusNetworkConfig(ConfigModel):
    device_regex: str | None = None
    exclude_device_regex: str = "lo|docker.*|veth.*|tap.*|qbr.*|qvo.*|qvb.*"


class PrometheusConfig(ConfigModel):
    enabled: bool = False
    url: str | None = None
    timeout: str = "30s"
    query_step: str = "15s"
    auth: PrometheusAuthConfig = Field(default_factory=PrometheusAuthConfig)
    tls: PrometheusTLSConfig = Field(default_factory=PrometheusTLSConfig)
    network: PrometheusNetworkConfig = Field(default_factory=PrometheusNetworkConfig)
    query_packs: list[str] = Field(default_factory=lambda: ["node-exporter-network"])

    @field_validator("timeout", "query_step")
    @classmethod
    def validate_duration(cls, value: str) -> str:
        if parse_duration(value).total_seconds() <= 0:
            raise ValueError("duration must be positive")
        return value

    @model_validator(mode="after")
    def validate_enabled(self) -> PrometheusConfig:
        if self.enabled and (not self.url or not self.url.startswith(("http://", "https://"))):
            raise ValueError("enabled Prometheus requires an HTTP(S) url")
        if self.enabled and not self.network.device_regex:
            raise ValueError("enabled Prometheus requires an explicit network.device_regex")
        if not self.enabled and self.model_fields_set - {"enabled"}:
            raise ValueError("disabled Prometheus accepts only enabled: false")
        if set(self.query_packs) - {"node-exporter-network"}:
            raise ValueError("unsupported Prometheus query pack")
        return self


class ObservabilityConfig(ConfigModel):
    prometheus: PrometheusConfig = Field(default_factory=PrometheusConfig)


class RunConfig(ConfigModel):
    keep_resources: bool = False


class SoakConfig(ConfigModel):
    openstack: OpenStackConfig
    boot_concurrency: int = Field(default=1, ge=1)
    management: ManagementConfig
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    flavors: FlavorsConfig
    targets: TargetsConfig = Field(default_factory=TargetsConfig)
    workloads: WorkloadsConfig = Field(default_factory=WorkloadsConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    run: RunConfig = Field(default_factory=RunConfig)

    @model_validator(mode="after")
    def validate_active_planes(self) -> SoakConfig:
        for plane in ["compute", "network", "storage", "api"]:
            workload = getattr(self.workloads, plane)
            target = getattr(self.targets, plane)
            if not workload.enabled:
                if workload.model_fields_set - {"enabled"}:
                    raise ValueError(f"disabled workloads.{plane} accepts only enabled: false")
                if target is not None:
                    raise ValueError(f"targets.{plane} is inapplicable to a disabled workload")
                if plane != "api" and getattr(self.flavors, plane + "_worker") is not None:
                    raise ValueError(
                        f"flavors.{plane}_worker is inapplicable to a disabled workload"
                    )
                continue
            if plane == "compute" and workload.flavor_mix:
                if self.flavors.compute_worker is not None:
                    raise ValueError("use compute_worker or flavor_mix, not both")
            if (
                plane != "api"
                and getattr(self.flavors, plane + "_worker") is None
                and not (plane == "compute" and workload.flavor_mix)
            ):
                raise ValueError(f"enabled {plane} workload requires flavors.{plane}_worker")
            requires_target = plane in {"compute", "network"} or (
                plane == "api" and workload.engine == "native"
            )
            if requires_target and target is None:
                raise ValueError(f"enabled {plane} workload requires targets.{plane}")
            if plane == "api" and workload.engine == "rally" and target is not None:
                raise ValueError("Rally uses explicit concurrency, not targets.api")
        network = self.workloads.network
        if network.enabled:
            if self.targets.network.scope == "aggregate" and network.model_fields_set & {
                "sender_worker_target_gbps",
                "receiver_worker_target_gbps",
            }:
                raise ValueError("worker target rates require per_host network planning")
            expected = "full_mesh" if self.targets.network.scope == "per_host" else "paired"
            if network.topology != expected:
                raise ValueError(
                    f"{self.targets.network.scope} network target requires {expected} topology"
                )
            if self.targets.network.scope != "per_host" and network.hosts is not None:
                raise ValueError("explicit hosts require per_host network planning")
        if not any(
            getattr(self.workloads, p).enabled for p in ["compute", "network", "storage", "api"]
        ):
            raise ValueError("at least one workload must be enabled")
        if self.resources.create_router and not self.openstack.external_network:
            raise ValueError("create_router requires openstack.external_network")
        if not self.resources.create_router and self.openstack.external_network is not None:
            raise ValueError("openstack.external_network is inapplicable without create_router")
        if self.resources.floating_ips.controller and not self.resources.create_router:
            raise ValueError("controller floating IP requires create_router")
        return self


def load_config(path: str | Path) -> SoakConfig:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return SoakConfig.model_validate(payload)


def dump_config(config: SoakConfig) -> str:
    payload = config.model_dump(mode="json")
    for plane, workload in payload["workloads"].items():
        if not workload["enabled"]:
            payload["workloads"][plane] = {"enabled": False}
        elif plane == "storage" and workload["profile"] != "mixed-randrw":
            workload.pop("rwmixread", None)
        elif plane == "api":
            if (
                workload["engine"] == "native"
                and "create_delete_volumes" not in workload["operation_mix"]
            ):
                workload.pop("create_delete_volume_size_gib", None)
            for key in list(workload):
                if workload["engine"] == "native" and key.startswith("rally_"):
                    workload.pop(key)
                elif workload["engine"] == "rally" and key in {
                    "operation_mix",
                    "create_delete_volume_size_gib",
                }:
                    workload.pop(key)
    if (
        payload["workloads"]["network"].get("enabled")
        and payload["workloads"]["network"]["vnic_type"] == "normal"
    ):
        payload["workloads"]["network"].pop("port_security_enabled", None)
    if (payload["targets"].get("network") or {}).get("scope") == "aggregate":
        for key in ["sender_worker_target_gbps", "receiver_worker_target_gbps"]:
            payload["workloads"]["network"].pop(key, None)
    payload["targets"] = {k: v for k, v in payload["targets"].items() if v is not None}
    payload["flavors"] = {k: v for k, v in payload["flavors"].items() if v is not None}
    if not payload["observability"]["prometheus"]["enabled"]:
        payload["observability"]["prometheus"] = {"enabled": False}
    return yaml.safe_dump(payload, sort_keys=False)


class CampaignItem(ConfigModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    config: FilePath
    duration: str

    @field_validator("duration")
    @classmethod
    def validate_duration(cls, value):
        if parse_duration(value).total_seconds() <= 0:
            raise ValueError("campaign duration must be positive")
        return value


class CampaignConfig(ConfigModel):
    timezone: str
    os_cloud: str
    target: float = Field(ge=1, le=100)
    latest_finish: datetime
    campaigns: list[CampaignItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_campaigns(self):
        ZoneInfo(self.timezone)
        if self.latest_finish.tzinfo is None:
            raise ValueError("latest_finish must include a timezone offset")
        names = [item.name for item in self.campaigns]
        if len(names) != len(set(names)):
            raise ValueError("campaign names must be unique")
        return self
