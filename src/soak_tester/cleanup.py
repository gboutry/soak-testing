from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from openstack.exceptions import ResourceNotFound

from soak_tester.util import atomic_write_json, read_json, utcnow_iso


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
    try:
        return _cleanup_run(
            run_dir, conn, dry_run=dry_run, max_passes=max_passes, settle_seconds=settle_seconds
        )
    except Exception as exc:
        if not dry_run:
            atomic_write_json(
                run_dir / "cleanup.json",
                {
                    "checked_at": utcnow_iso(),
                    "status": "failed",
                    "errors": [str(exc)],
                    "remaining": None,
                },
            )
        raise


def _cleanup_run(
    run_dir: Path,
    conn: Any | None = None,
    *,
    dry_run: bool = False,
    max_passes: int = 3,
    settle_seconds: float = 2.0,
) -> list[dict[str, Any]]:
    resources = load_resources(run_dir)
    if (
        not dry_run
        and not (run_dir / "resources.json").exists()
        and (read_json(run_dir / "state.json", {}) or {})
        .get("timeline", {})
        .get("provisioning_started_at")
    ):
        raise RuntimeError(
            "Provisioning began but the resource manifest is missing; ownership is unverified"
        )
    if not dry_run and conn is not None:
        _recover_identity(run_dir, resources, conn)
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
                if recorded["type"] in {"project", "user"}:
                    inventory = remaining_resources(conn, run_dir, check_identity=False)
                    if inventory["errors"] or any(inventory["remaining"].values()):
                        raise RuntimeError(
                            "Tenant resource absence is not verified; retaining identity"
                        )
                _delete_resource(conn, recorded)
            except Exception as exc:
                recorded["error"] = str(exc)
            executed.append(recorded)
        if cleanup_pass < max_passes - 1 and settle_seconds > 0:
            time.sleep(settle_seconds)
    verification = remaining_resources(conn, run_dir)
    receipt = {
        "checked_at": utcnow_iso(),
        "status": "passed"
        if not verification["errors"] and not any(verification["remaining"].values())
        else "failed",
        "actions": executed,
        **verification,
    }
    atomic_write_json(run_dir / "cleanup.json", receipt)
    return executed


def cleanup_actions(
    run_dir: Path,
    resources: dict[str, Any],
    conn: Any | None = None,
) -> list[dict[str, Any]]:
    actions = _actions_from_resources(resources)
    if conn is not None:
        identity = _load_run_identity(run_dir, resources)
        actions.extend(_discover_neutron_actions(conn, identity))
        if identity["project_id"]:
            for service, method, kind, query in _tenant_queries(
                conn,
                identity["project_id"],
                next(
                    (r["id"] for r in resources.get("resources", []) if r["type"] == "user"), None
                ),
            ):
                if service is None or not hasattr(service, method):
                    continue
                for resource in _list_resources(service, method, **query):
                    if kind == "keypair" or _resource_matches_run(
                        resource,
                        identity["run_id"],
                        identity["project_name"],
                        identity["project_id"],
                    ):
                        actions.append(
                            _action(
                                kind,
                                _attr(resource, "name")
                                if kind == "keypair"
                                else _attr(resource, "id"),
                                _attr(resource, "name") or _attr(resource, "id"),
                            )
                        )
    user_id = next((r["id"] for r in resources.get("resources", []) if r["type"] == "user"), None)
    for action in actions:
        if action["type"] == "keypair" and user_id:
            action["extra"] = {**action.get("extra", {}), "user_id": user_id}
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
                actions.append(_action("subnet", subnet_id, _attr(subnet, "name") or subnet_id))
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
    return list(method(**query))


def _resource_matches_run(
    resource: Any,
    run_id: str | None,
    project_name: str | None,
    project_id: str | None,
) -> bool:
    if project_id:
        # An explicit tenant boundary takes precedence over names or tags.
        return _attr(resource, "project_id") == project_id
    # Names alone cannot establish ownership in an admin-wide inventory.
    tags = _attr(resource, "tags") or []
    if run_id and f"soak_tester_run_id={run_id}" in tags and "soak_tester_owned=true" in tags:
        return True
    metadata = _attr(resource, "metadata") or {}
    return bool(
        run_id
        and metadata.get("soak_tester_run_id") == run_id
        and metadata.get("soak_tester_owned") == "true"
    )


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
        conn.compute.delete_keypair(
            resource_id,
            ignore_missing=True,
            **(
                {"user_id": action["extra"]["user_id"]}
                if action.get("extra", {}).get("user_id")
                else {}
            ),
        )
    elif resource_type == "quota":
        return
    elif resource_type == "user":
        conn.identity.delete_user(resource_id, ignore_missing=True)
    elif resource_type == "project":
        conn.identity.delete_project(resource_id, ignore_missing=True)
    else:
        raise ValueError(f"unsupported resource type: {resource_type}")


def _tenant_queries(conn, project_id, user_id=None):
    return [
        *(
            [(getattr(conn, "compute", None), "keypairs", "keypair", {"user_id": user_id})]
            if user_id
            else []
        ),
        (
            getattr(conn, "compute", None),
            "servers",
            "server",
            {"all_projects": True, "project_id": project_id},
        ),
        (
            getattr(conn, "block_storage", None),
            "volumes",
            "volume",
            {"all_projects": True, "project_id": project_id},
        ),
        *[
            (getattr(conn, "network", None), method, kind, {"project_id": project_id})
            for method, kind in [
                ("networks", "network"),
                ("subnets", "subnet"),
                ("routers", "router"),
                ("ports", "port"),
                ("ips", "floating_ip"),
                ("security_groups", "security_group"),
            ]
        ],
    ]


def remaining_resources(conn, run_dir: Path, *, check_identity: bool = True) -> dict[str, Any]:
    """Fail closed when any owned-resource inventory cannot be read."""
    resources = load_resources(run_dir)
    identity = _load_run_identity(run_dir, resources)
    remaining, errors = {}, []
    project_id = identity["project_id"]
    user_id = next((r["id"] for r in resources.get("resources", []) if r["type"] == "user"), None)
    if project_id:
        for service, method, kind, query in _tenant_queries(conn, project_id, user_id):
            try:
                remaining[kind] = [
                    str(_attr(item, "id") or _attr(item, "name"))
                    for item in getattr(service, method)(**query)
                ]
            except Exception as exc:
                errors.append(f"{kind} inventory: {exc}")
    # Read every recorded object by ID as well as inventory, including identity and user-scoped keypairs.
    getters = {
        "server": ("compute", "get_server"),
        "volume": ("block_storage", "get_volume"),
        "floating_ip": ("network", "get_ip"),
        "port": ("network", "get_port"),
        "network": ("network", "get_network"),
        "subnet": ("network", "get_subnet"),
        "router": ("network", "get_router"),
        "security_group": ("network", "get_security_group"),
        "project": ("identity", "get_project"),
        "user": ("identity", "get_user"),
        "keypair": ("compute", "get_keypair"),
    }
    for item in resources.get("resources", []):
        kind, resource_id = item.get("type"), item.get("id")
        if kind == "quota":
            continue
        if kind == "router_interface":
            extra = item.get("extra", {})
            try:
                router_id = extra.get("router_id")
                subnet_id = extra.get("subnet_id")
                if not router_id or not subnet_id:
                    raise ValueError("router interface identity is incomplete")
                if conn.network.get_router(router_id) is not None:
                    ports = list(conn.network.ports(device_id=router_id))
                    if any(subnet_id in _port_subnet_ids(port) for port in ports):
                        remaining.setdefault(kind, []).append(str(resource_id))
            except ResourceNotFound:
                pass
            except Exception as exc:
                errors.append(f"router_interface {resource_id}: {exc}")
            continue
        if kind not in getters:
            errors.append(f"Unsupported recorded resource type: {kind}")
            continue
        if not check_identity and kind in {"project", "user"}:
            continue
        service, method = getters[kind]
        try:
            kwargs = {"user_id": user_id} if kind == "keypair" and user_id else {}
            found = getattr(getattr(conn, service), method)(resource_id, **kwargs)
            if found is not None:
                remaining.setdefault(kind, []).append(str(resource_id))
        except ResourceNotFound:
            pass
        except Exception as exc:
            errors.append(f"{kind} {resource_id}: {exc}")
    if not project_id and read_json(run_dir / "ownership-intent.json"):
        # No ID can mean an uncertain identity create response, rather than no creation.
        intent = read_json(run_dir / "ownership-intent.json")
        try:
            for kind, method in [("project", "projects"), ("user", "users")]:
                remaining[kind] = [
                    str(_attr(r, "id"))
                    for r in getattr(conn.identity, method)(name=intent["name"])
                    if _attr(r, "description") == intent["description"]
                ]
        except Exception as exc:
            errors.append(f"identity intent verification: {exc}")
    return {"remaining": {k: sorted(set(v)) for k, v in remaining.items()}, "errors": errors}


def _recover_identity(run_dir, resources, conn):
    intent = read_json(run_dir / "ownership-intent.json")
    if not intent:
        return
    for kind, method in [("project", "projects"), ("user", "users")]:
        for item in getattr(conn.identity, method)(name=intent["name"]):
            if _attr(item, "description") != intent["description"]:
                continue
            if not any(
                r["type"] == kind and r["id"] == _attr(item, "id")
                for r in resources.get("resources", [])
            ):
                resources.setdefault("resources", []).append(
                    {"type": kind, "id": _attr(item, "id"), "name": _attr(item, "name")}
                )
    atomic_write_json(run_dir / "resources.json", resources)
