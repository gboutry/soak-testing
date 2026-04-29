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
