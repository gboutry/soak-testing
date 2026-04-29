from __future__ import annotations

from typing import Any


def require_external_network(conn: Any, name: str) -> Any:
    network = conn.network.find_network(name)
    if network is None:
        raise RuntimeError(f"external network not found: {name}")
    if not (getattr(network, "is_router_external", False) or getattr(network, "router:external", False)):
        raise RuntimeError(f"network is not router-external: {name}")
    return network


def create_security_group_rules(conn: Any, security_group: Any, *, cidr: str, allowed_cidrs: list[str], port: int) -> None:
    conn.network.create_security_group_rule(
        security_group_id=security_group.id,
        direction="ingress",
        ethertype="IPv4",
        remote_ip_prefix=cidr,
    )
    conn.network.create_security_group_rule(
        security_group_id=security_group.id,
        direction="ingress",
        ethertype="IPv4",
        protocol="icmp",
    )
    for allowed in allowed_cidrs:
        conn.network.create_security_group_rule(
            security_group_id=security_group.id,
            direction="ingress",
            ethertype="IPv4",
            protocol="tcp",
            port_range_min=22,
            port_range_max=22,
            remote_ip_prefix=allowed,
        )
        conn.network.create_security_group_rule(
            security_group_id=security_group.id,
            direction="ingress",
            ethertype="IPv4",
            protocol="tcp",
            port_range_min=port,
            port_range_max=port,
            remote_ip_prefix=allowed,
        )
