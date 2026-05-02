from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from soak_tester.util import read_json


ORDER = {
    "server": 10,
    "volume_attachment": 20,
    "volume": 30,
    "floating_ip": 40,
    "router_interface": 50,
    "port": 60,
    "security_group": 70,
    "router": 80,
    "subnet": 90,
    "network": 100,
    "keypair": 110,
    "quota": 115,
    "user": 120,
    "project": 130,
}


def cleanup_order(resource_type: str) -> int:
    return ORDER.get(resource_type, 999)


def dry_run_cleanup(resources: dict[str, Any]) -> list[dict[str, Any]]:
    return _ordered_actions(_actions_from_resources(resources))


def _actions_from_resources(resources: dict[str, Any]) -> list[dict[str, Any]]:
    return [_resource_to_action(item) for item in resources.get("resources", [])]


def _resource_to_action(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "delete",
        "type": item.get("type"),
        "id": item.get("id"),
        "name": item.get("name"),
        "extra": item.get("extra", {}),
    }


def _action_sort_key(action: dict[str, Any]) -> tuple[int, str]:
    return (cleanup_order(action.get("type", "")), str(action.get("id", "")))


def _ordered_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(_dedupe_actions(actions), key=_action_sort_key)


def _dedupe_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for action in actions:
        key = (str(action.get("type")), str(action.get("id")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)
    return deduped


def _load_run_identity(run_dir: Path, resources: dict[str, Any]) -> dict[str, str | None]:
    state = read_json(run_dir / "state.json", default={}) or {}
    project = next(
        (item for item in resources.get("resources", []) if item.get("type") == "project"),
        {},
    )
    return {
        "run_id": state.get("run_id"),
        "project_name": state.get("project_name") or project.get("name"),
        "project_id": project.get("id"),
    }


def load_resources(run_dir: Path) -> dict[str, Any]:
    return read_json(run_dir / "resources.json", default={"resources": []})


def cleanup_run(
    run_dir: Path,
    conn: Any | None = None,
    *,
    dry_run: bool = False,
    max_passes: int = 3,
    settle_seconds: float = 2.0,
) -> list[dict[str, Any]]:
    resources = load_resources(run_dir)
    actions = cleanup_actions(run_dir, resources, conn)
    if dry_run or conn is None:
        return actions
    executed: list[dict[str, Any]] = []
    for cleanup_pass in range(max(1, max_passes)):
        pass_actions = actions if cleanup_pass == 0 else cleanup_actions(run_dir, resources, conn)
        if not pass_actions:
            break
        for action in pass_actions:
            recorded = dict(action)
            try:
                _delete_resource(conn, recorded)
            except Exception as exc:
                recorded["error"] = str(exc)
            executed.append(recorded)
        if cleanup_pass < max_passes - 1 and settle_seconds > 0:
            time.sleep(settle_seconds)
    return executed


def cleanup_actions(
    run_dir: Path,
    resources: dict[str, Any],
    conn: Any | None = None,
) -> list[dict[str, Any]]:
    actions = _actions_from_resources(resources)
    if conn is not None:
        actions.extend(_discover_neutron_actions(conn, _load_run_identity(run_dir, resources)))
    return _ordered_actions(actions)


def _discover_neutron_actions(conn: Any, identity: dict[str, str | None]) -> list[dict[str, Any]]:
    run_id = identity.get("run_id")
    project_name = identity.get("project_name")
    project_id = identity.get("project_id")
    if not run_id and not project_name and not project_id:
        return []

    actions: list[dict[str, Any]] = []
    networks = [
        network
        for network in _list_resources(conn.network, "networks")
        if _resource_matches_run(network, run_id, project_name, project_id)
    ]
    for network in networks:
        network_id = _attr(network, "id")
        network_name = _attr(network, "name") or network_id
        if not network_id:
            continue
        for subnet in _list_resources(conn.network, "subnets", network_id=network_id):
            subnet_id = _attr(subnet, "id")
            if subnet_id:
                actions.append(
                    _action("subnet", subnet_id, _attr(subnet, "name") or subnet_id)
                )
        for port in _list_resources(conn.network, "ports", network_id=network_id):
            port_id = _attr(port, "id")
            if not port_id:
                continue
            if _is_router_interface_port(port):
                for subnet_id in _port_subnet_ids(port):
                    actions.append(
                        _action(
                            "router_interface",
                            f"{_attr(port, 'device_id')}:{subnet_id}",
                            port_id,
                            extra={
                                "router_id": _attr(port, "device_id"),
                                "subnet_id": subnet_id,
                                "port_id": port_id,
                            },
                        )
                    )
                continue
            actions.append(_action("port", port_id, _attr(port, "name") or port_id))
        actions.append(_action("network", network_id, network_name))

    for router in _list_resources(conn.network, "routers"):
        if _resource_matches_run(router, run_id, project_name, project_id):
            router_id = _attr(router, "id")
            if router_id:
                actions.append(_action("router", router_id, _attr(router, "name") or router_id))
    return actions


def _action(
    resource_type: str,
    resource_id: str,
    name: str,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "action": "delete",
        "type": resource_type,
        "id": resource_id,
        "name": name,
        "extra": extra or {},
    }


def _list_resources(service: Any, method_name: str, **query: Any) -> list[Any]:
    method = getattr(service, method_name, None)
    if method is None:
        return []
    try:
        return list(method(**query))
    except TypeError:
        if query:
            return []
        return list(method())
    except Exception:
        return []


def _resource_matches_run(
    resource: Any,
    run_id: str | None,
    project_name: str | None,
    project_id: str | None,
) -> bool:
    if project_id and _attr(resource, "project_id") == project_id:
        return True
    name = _attr(resource, "name") or ""
    if project_name and name.startswith(project_name):
        return True
    tags = _attr(resource, "tags") or []
    if run_id and f"soak_tester_run_id={run_id}" in tags:
        return True
    metadata = _attr(resource, "metadata") or {}
    return bool(run_id and metadata.get("soak_tester_run_id") == run_id)


def _is_router_interface_port(port: Any) -> bool:
    owner = _attr(port, "device_owner") or ""
    return "router_interface" in owner


def _port_subnet_ids(port: Any) -> list[str]:
    subnet_ids: list[str] = []
    for fixed_ip in _attr(port, "fixed_ips") or []:
        subnet_id = fixed_ip.get("subnet_id")
        if subnet_id:
            subnet_ids.append(subnet_id)
    return subnet_ids


def _attr(resource: Any, name: str) -> Any:
    if isinstance(resource, dict):
        return resource.get(name)
    return getattr(resource, name, None)


def _delete_resource(conn: Any, action: dict[str, Any]) -> None:
    resource_type = action["type"]
    resource_id = action["id"]
    if resource_type == "server":
        conn.compute.delete_server(resource_id, ignore_missing=True)
    elif resource_type == "router_interface":
        extra = action.get("extra", {})
        router_id = extra.get("router_id")
        subnet_id = extra.get("subnet_id")
        port_id = extra.get("port_id")
        if router_id:
            try:
                router = conn.network.get_router(router_id)
            except Exception:
                return
            if port_id:
                try:
                    conn.network.remove_interface_from_router(router, port_id=port_id)
                    return
                except Exception:
                    if not subnet_id:
                        raise
            if subnet_id:
                conn.network.remove_interface_from_router(router, subnet_id=subnet_id)
    elif resource_type == "volume":
        conn.block_storage.delete_volume(resource_id, ignore_missing=True)
    elif resource_type == "floating_ip":
        conn.network.delete_ip(resource_id, ignore_missing=True)
    elif resource_type == "port":
        conn.network.delete_port(resource_id, ignore_missing=True)
    elif resource_type == "security_group":
        conn.network.delete_security_group(resource_id, ignore_missing=True)
    elif resource_type == "router":
        try:
            router = conn.network.get_router(resource_id)
        except Exception:
            router = None
        if router is not None:
            try:
                conn.network.update_router(router, external_gateway_info={})
            except Exception:
                pass
        conn.network.delete_router(resource_id, ignore_missing=True)
    elif resource_type == "subnet":
        conn.network.delete_subnet(resource_id, ignore_missing=True)
    elif resource_type == "network":
        conn.network.delete_network(resource_id, ignore_missing=True)
    elif resource_type == "keypair":
        conn.compute.delete_keypair(resource_id, ignore_missing=True)
    elif resource_type == "quota":
        return
    elif resource_type == "user":
        conn.identity.delete_user(resource_id, ignore_missing=True)
    elif resource_type == "project":
        conn.identity.delete_project(resource_id, ignore_missing=True)
