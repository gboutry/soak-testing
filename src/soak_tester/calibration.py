from __future__ import annotations

import copy
import math
from typing import Any

from soak_tester.config import SoakConfig
from soak_tester.openstack_client.compute import list_hypervisor_names


def discover_cloud_capacity(conn: Any, config: SoakConfig) -> dict[str, Any]:
    """Discover OpenStack-advertised capacity and produce config-ready values.

    Placement is authoritative for modern Nova CPU inventories. In dedicated CPU
    clouds, compute nodes may expose zero VCPU and all schedulable guest cores as
    PCPU. We keep that resource-class provenance in the calibration report while
    writing the selected guest-core count back to capacity.compute.vcpus.
    """

    hypervisor_names = list_hypervisor_names(conn)
    placement = discover_placement_inventory(conn, hypervisor_names=hypervisor_names)
    hypervisors = discover_hypervisor_inventory(conn)
    cpu = select_compute_cpu_capacity(
        placement=placement,
        hypervisors=hypervisors,
        requested_resource_class=config.capacity.compute.cpu_resource_class,
    )
    memory = select_memory_capacity(
        placement=placement,
        hypervisors=hypervisors,
        configured_per_host_ram_gib=config.capacity.compute.per_host_ram_gib,
        host_count=len(hypervisor_names),
    )
    network = discover_network_inventory(conn, config)
    storage = discover_storage_inventory(conn)
    generated_config = build_calibrated_config(
        config=config,
        cpu_vcpus=cpu["selected"]["allocatable"],
        memory_ram_gib=memory["allocatable_gib"],
        cpu_resource_class=cpu["selected"]["resource_class"],
    )

    return {
        "schema_version": 1,
        "mode": "hybrid",
        "generated_config_file": "calibrated-capacity.yaml",
        "compute": {
            "source": cpu["selected"]["source"],
            "cpu": cpu,
            "memory": memory,
            "hypervisors": hypervisors,
            "placement": placement,
        },
        "network": network,
        "storage": storage,
        "api": {
            "source": "config_assumption",
            "ops_per_second": config.capacity.api.ops_per_second,
            "note": "OpenStack does not advertise API throughput capacity; use an API probe to replace this assumption.",
        },
        "generated_capacity": generated_config["capacity"],
    }


def build_calibrated_config(
    *,
    config: SoakConfig,
    cpu_vcpus: int,
    memory_ram_gib: float,
    cpu_resource_class: str,
) -> dict[str, Any]:
    payload = copy.deepcopy(config.model_dump(mode="json"))
    payload["capacity"]["compute"]["vcpus"] = cpu_vcpus
    payload["capacity"]["compute"]["ram_gib"] = memory_ram_gib
    payload["capacity"]["compute"]["cpu_resource_class"] = cpu_resource_class
    network = payload["capacity"]["network"]
    workload_hosts = payload["workloads"]["network"].get("hosts")
    host_count = len(workload_hosts or []) or payload["workloads"]["network"].get("host_count")
    if network.get("scope") == "per_host" and network.get("per_host_gbps") and host_count:
        network["gbps"] = float(network["per_host_gbps"]) * int(host_count)
    return payload


def discover_placement_inventory(
    conn: Any,
    *,
    hypervisor_names: list[str],
) -> dict[str, Any]:
    try:
        providers = _placement_resource_providers(conn)
    except Exception as exc:
        return {
            "source": "placement",
            "error": str(exc),
            "hypervisor_names": hypervisor_names,
            "providers": [],
            "resource_classes": {
                "PCPU": _empty_resource_summary(),
                "VCPU": _empty_resource_summary(),
                "MEMORY_MB": _empty_resource_summary(),
            },
        }
    hypervisor_name_set = set(hypervisor_names)
    candidate_providers = [
        provider
        for provider in providers
        if not hypervisor_name_set or str(provider.get("name")) in hypervisor_name_set
    ]
    if not candidate_providers and hypervisor_name_set:
        candidate_providers = providers

    resource_classes = {
        "PCPU": _empty_resource_summary(),
        "VCPU": _empty_resource_summary(),
        "MEMORY_MB": _empty_resource_summary(),
    }
    selected_providers: list[dict[str, Any]] = []
    for provider in candidate_providers:
        provider_uuid = str(provider.get("uuid") or provider.get("id"))
        inventories = _placement_inventories(conn, provider_uuid)
        resources = inventories.get("inventories", inventories)
        provider_summary: dict[str, Any] = {
            "uuid": provider_uuid,
            "name": provider.get("name"),
            "resource_classes": {},
        }
        include_provider = False
        for resource_class in resource_classes:
            inventory = resources.get(resource_class)
            if not inventory:
                continue
            summary = _inventory_summary(inventory)
            provider_summary["resource_classes"][resource_class] = summary
            _add_resource_summary(resource_classes[resource_class], summary)
            include_provider = True
        if include_provider:
            selected_providers.append(provider_summary)

    return {
        "source": "placement",
        "hypervisor_names": hypervisor_names,
        "providers": selected_providers,
        "resource_classes": resource_classes,
    }


def discover_hypervisor_inventory(conn: Any) -> dict[str, Any]:
    try:
        hypervisors = list(conn.compute.hypervisors(details=True))
    except TypeError:
        hypervisors = list(conn.compute.hypervisors())
    except Exception as exc:
        return {"source": "nova_hypervisors", "error": str(exc), "hypervisors": []}

    records: list[dict[str, Any]] = []
    totals = {
        "vcpus": 0,
        "vcpus_used": 0,
        "memory_mb": 0,
        "memory_mb_used": 0,
        "local_gb": 0,
        "local_gb_used": 0,
    }
    for hypervisor in hypervisors:
        record = {
            "name": _resource_value(hypervisor, "hypervisor_hostname")
            or _resource_value(hypervisor, "name"),
            "state": _resource_value(hypervisor, "state"),
            "status": _resource_value(hypervisor, "status"),
            "vcpus": _int_value(_resource_value(hypervisor, "vcpus")),
            "vcpus_used": _int_value(_resource_value(hypervisor, "vcpus_used")),
            "memory_mb": _int_value(_resource_value(hypervisor, "memory_mb")),
            "memory_mb_used": _int_value(_resource_value(hypervisor, "memory_mb_used")),
            "local_gb": _int_value(_resource_value(hypervisor, "local_gb")),
            "local_gb_used": _int_value(_resource_value(hypervisor, "local_gb_used")),
        }
        if record["state"] and str(record["state"]).lower() != "up":
            records.append(record | {"included": False})
            continue
        if record["status"] and str(record["status"]).lower() != "enabled":
            records.append(record | {"included": False})
            continue
        record["included"] = True
        records.append(record)
        for key in totals:
            totals[key] += int(record[key] or 0)

    return {"source": "nova_hypervisors", "totals": totals, "hypervisors": records}


def select_compute_cpu_capacity(
    *,
    placement: dict[str, Any],
    hypervisors: dict[str, Any],
    requested_resource_class: str,
) -> dict[str, Any]:
    classes = placement.get("resource_classes", {})
    pcpu = classes.get("PCPU", _empty_resource_summary())
    vcpu = classes.get("VCPU", _empty_resource_summary())
    if requested_resource_class == "PCPU":
        selected_class = "PCPU"
    elif requested_resource_class == "VCPU":
        selected_class = "VCPU"
    elif pcpu.get("allocatable", 0) > 0 and vcpu.get("allocatable", 0) == 0:
        selected_class = "PCPU"
    else:
        selected_class = "VCPU" if vcpu.get("allocatable", 0) > 0 else "PCPU"

    selected = classes.get(selected_class, _empty_resource_summary()).copy()
    source = "placement"
    if selected.get("allocatable", 0) <= 0:
        nova_vcpus = int(hypervisors.get("totals", {}).get("vcpus", 0))
        selected = {
            "total": nova_vcpus,
            "reserved": 0,
            "allocation_ratio": 1.0,
            "allocatable": nova_vcpus,
            "provider_count": len(hypervisors.get("hypervisors", [])),
        }
        selected_class = "VCPU"
        source = "nova_hypervisors_fallback"

    return {
        "selected": {"resource_class": selected_class, "source": source, **selected},
        "PCPU": pcpu,
        "VCPU": vcpu,
        "note": (
            "PCPU is selected when Placement exposes dedicated CPU inventory and VCPU is absent. "
            "capacity.compute.vcpus remains the guest-core count used by the planner."
        ),
    }


def select_memory_capacity(
    *,
    placement: dict[str, Any],
    hypervisors: dict[str, Any],
    configured_per_host_ram_gib: float | None = None,
    host_count: int = 0,
) -> dict[str, Any]:
    if configured_per_host_ram_gib and host_count:
        allocatable_gib = configured_per_host_ram_gib * host_count
        return {
            "source": "configured_per_host_envelope",
            "total_gib": round(allocatable_gib, 3),
            "reserved_gib": 0,
            "allocatable_gib": round(allocatable_gib, 3),
            "allocation_ratio": 1.0,
            "host_count": host_count,
            "per_host_ram_gib": configured_per_host_ram_gib,
            "note": (
                "Using configured per-host memory envelope. This is useful when benchmark "
                "flavors consume hugepage-backed memory but Placement exposes only generic MEMORY_MB."
            ),
        }
    memory = placement.get("resource_classes", {}).get("MEMORY_MB", _empty_resource_summary())
    if memory.get("allocatable", 0) > 0:
        return {
            "source": "placement",
            "total_gib": round(float(memory["total"]) / 1024, 3),
            "reserved_gib": round(float(memory["reserved"]) / 1024, 3),
            "allocatable_gib": round(float(memory["allocatable"]) / 1024, 3),
            "allocation_ratio": memory["allocation_ratio"],
        }
    memory_mb = int(hypervisors.get("totals", {}).get("memory_mb", 0))
    return {
        "source": "nova_hypervisors_fallback",
        "total_gib": round(memory_mb / 1024, 3),
        "reserved_gib": 0,
        "allocatable_gib": round(memory_mb / 1024, 3),
        "allocation_ratio": 1.0,
    }


def discover_network_inventory(conn: Any, config: SoakConfig) -> dict[str, Any]:
    network = None
    try:
        network = conn.network.find_network(config.openstack.external_network)
    except Exception:
        network = None
    return {
        "source": "openstack_inventory_and_config_assumption",
        "configured_capacity": config.capacity.network.model_dump(mode="json"),
        "external_network": _network_record(network) if network else None,
        "note": (
            "Neutron does not advertise tenant overlay bandwidth. Use network workload "
            "calibration and optional Prometheus evidence to replace configured assumptions."
        ),
    }


def discover_storage_inventory(conn: Any) -> dict[str, Any]:
    pools: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        raw_pools = list(conn.block_storage.backend_pools(details=True))
    except AttributeError:
        try:
            raw_pools = list(conn.block_storage.pools(details=True))
        except Exception as exc:
            errors.append(str(exc))
            raw_pools = []
    except Exception as exc:
        errors.append(str(exc))
        raw_pools = []

    total_gib = 0.0
    free_gib = 0.0
    for pool in raw_pools:
        capabilities = _resource_value(pool, "capabilities") or {}
        record = {
            "name": _resource_value(pool, "name"),
            "volume_backend_name": capabilities.get("volume_backend_name"),
            "total_capacity_gib": _float_value(capabilities.get("total_capacity_gb")),
            "free_capacity_gib": _float_value(capabilities.get("free_capacity_gb")),
            "thin_provisioning_support": capabilities.get("thin_provisioning_support"),
            "multiattach": capabilities.get("multiattach"),
        }
        total_gib += record["total_capacity_gib"] or 0.0
        free_gib += record["free_capacity_gib"] or 0.0
        pools.append(record)

    return {
        "source": "cinder_scheduler_stats",
        "total_capacity_gib": round(total_gib, 3),
        "free_capacity_gib": round(free_gib, 3),
        "pools": pools,
        "errors": errors,
        "note": (
            "Cinder pool stats describe storage capacity, not fio read/write/IOPS capability. "
            "Use storage workload calibration to replace configured performance assumptions."
        ),
    }


def _placement_resource_providers(conn: Any) -> list[dict[str, Any]]:
    placement = getattr(conn, "placement", None)
    if placement is not None and hasattr(placement, "resource_providers"):
        return [_resource_to_dict(provider) for provider in placement.resource_providers()]
    payload = _placement_get(conn, "/resource_providers?limit=1000")
    providers = payload.get("resource_providers", payload if isinstance(payload, list) else [])
    return [_resource_to_dict(provider) for provider in providers]


def _placement_inventories(conn: Any, provider_uuid: str) -> dict[str, Any]:
    placement = getattr(conn, "placement", None)
    if placement is not None and hasattr(placement, "resource_provider_inventories"):
        provider = _find_resource_provider(placement, provider_uuid)
        inventories = {}
        for inventory in placement.resource_provider_inventories(provider):
            record = _resource_to_dict(inventory)
            resource_class = record.get("resource_class")
            if resource_class:
                inventories[resource_class] = record
        return {"inventories": inventories}
    return _placement_get(conn, f"/resource_providers/{provider_uuid}/inventories")


def _placement_get(conn: Any, path: str) -> Any:
    placement = getattr(conn, "placement", None)
    if placement is not None and hasattr(placement, "get"):
        response = placement.get(path)
    else:
        response = conn.session.get(path, endpoint_filter={"service_type": "placement"})
    if hasattr(response, "json"):
        return response.json()
    return response


def _find_resource_provider(placement: Any, provider_uuid: str) -> Any:
    try:
        return placement.get_resource_provider(provider_uuid)
    except Exception:
        for provider in placement.resource_providers():
            if str(_resource_value(provider, "id") or _resource_value(provider, "uuid")) == provider_uuid:
                return provider
        raise


def _inventory_summary(inventory: dict[str, Any]) -> dict[str, Any]:
    total = _int_value(inventory.get("total"))
    reserved = _int_value(inventory.get("reserved"))
    allocation_ratio = _float_value(inventory.get("allocation_ratio")) or 1.0
    return {
        "total": total,
        "reserved": reserved,
        "allocation_ratio": allocation_ratio,
        "allocatable": math.floor(max(total - reserved, 0) * allocation_ratio),
    }


def _empty_resource_summary() -> dict[str, Any]:
    return {
        "total": 0,
        "reserved": 0,
        "allocation_ratio": 1.0,
        "allocatable": 0,
        "provider_count": 0,
    }


def _add_resource_summary(target: dict[str, Any], summary: dict[str, Any]) -> None:
    target["total"] += summary["total"]
    target["reserved"] += summary["reserved"]
    target["allocatable"] += summary["allocatable"]
    target["provider_count"] += 1
    if target["provider_count"]:
        target["allocation_ratio"] = target["allocatable"] / max(
            target["total"] - target["reserved"], 1
        )


def _network_record(network: Any) -> dict[str, Any]:
    return {
        "id": _resource_value(network, "id"),
        "name": _resource_value(network, "name"),
        "mtu": _resource_value(network, "mtu"),
        "is_router_external": _resource_value(network, "is_router_external")
        or _resource_value(network, "router:external"),
        "provider_network_type": _resource_value(network, "provider_network_type")
        or _resource_value(network, "provider:network_type"),
        "provider_physical_network": _resource_value(network, "provider_physical_network")
        or _resource_value(network, "provider:physical_network"),
        "provider_segmentation_id": _resource_value(network, "provider_segmentation_id")
        or _resource_value(network, "provider:segmentation_id"),
    }


def _resource_value(resource: Any, key: str) -> Any:
    value = getattr(resource, key, None)
    if value is not None:
        return value
    if hasattr(resource, "get"):
        return resource.get(key)
    return None


def _resource_to_dict(resource: Any) -> dict[str, Any]:
    if hasattr(resource, "to_dict"):
        return dict(resource.to_dict())
    return dict(resource)


def _int_value(value: Any) -> int:
    if value in (None, "unknown", "infinite"):
        return 0
    return int(float(value))


def _float_value(value: Any) -> float:
    if value in (None, "unknown", "infinite"):
        return 0.0
    return float(value)
