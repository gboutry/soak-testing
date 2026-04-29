from __future__ import annotations

from typing import Any

from soak_tester.planner import FlavorSpec, flavor_spec_from_sdk


def require_flavor(conn: Any, name: str) -> FlavorSpec:
    flavor = conn.compute.find_flavor(name)
    if flavor is None:
        raise RuntimeError(f"flavor not found: {name}")
    return flavor_spec_from_sdk(flavor)


def wait_for_server(conn: Any, server: Any, *, status: str = "ACTIVE", timeout: int = 900) -> Any:
    return conn.compute.wait_for_server(server, status=status, failures=["ERROR"], wait=timeout)


def server_private_ip(server: Any) -> str | None:
    addresses = getattr(server, "addresses", None) or {}
    for network_addresses in addresses.values():
        for address in network_addresses:
            if address.get("OS-EXT-IPS:type") == "fixed":
                return address.get("addr")
    return None


def placement_record(worker_id: str, server: Any, *, plane: str, role: str) -> dict[str, Any]:
    return {
        "worker_id": worker_id,
        "server_id": getattr(server, "id", None),
        "server_name": getattr(server, "name", None),
        "plane": plane,
        "role": role,
        "private_ip": server_private_ip(server),
        "availability_zone": getattr(server, "availability_zone", None),
        "hypervisor_hostname": getattr(server, "hypervisor_hostname", None)
        or getattr(server, "OS-EXT-SRV-ATTR:hypervisor_hostname", None),
        "host": getattr(server, "host", None) or getattr(server, "OS-EXT-SRV-ATTR:host", None),
    }


def list_hypervisor_names(conn: Any) -> list[str]:
    try:
        hypervisors = conn.compute.hypervisors(details=True)
    except TypeError:
        hypervisors = conn.compute.hypervisors()
    names: list[str] = []
    for hypervisor in hypervisors:
        state = _resource_value(hypervisor, "state")
        status = _resource_value(hypervisor, "status")
        if state and str(state).lower() != "up":
            continue
        if status and str(status).lower() != "enabled":
            continue
        name = (
            _resource_value(hypervisor, "hypervisor_hostname")
            or _resource_value(hypervisor, "name")
        )
        if name:
            names.append(str(name))
    return sorted(set(names))


def _resource_value(resource: Any, key: str) -> Any:
    value = getattr(resource, key, None)
    if value is not None:
        return value
    if hasattr(resource, "get"):
        return resource.get(key)
    return None
