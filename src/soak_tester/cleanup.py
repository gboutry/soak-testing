from __future__ import annotations

from pathlib import Path
from typing import Any

from soak_tester.util import read_json


ORDER = {
    "server": 10,
    "volume_attachment": 20,
    "volume": 30,
    "floating_ip": 40,
    "port": 50,
    "security_group": 60,
    "router_interface": 70,
    "router": 80,
    "subnet": 90,
    "network": 100,
    "keypair": 110,
    "user": 120,
    "project": 130,
}


def cleanup_order(resource_type: str) -> int:
    return ORDER.get(resource_type, 999)


def dry_run_cleanup(resources: dict[str, Any]) -> list[dict[str, Any]]:
    items = resources.get("resources", [])
    ordered = sorted(items, key=lambda item: cleanup_order(item.get("type", "")))
    return [
        {
            "action": "delete",
            "type": item.get("type"),
            "id": item.get("id"),
            "name": item.get("name"),
        }
        for item in ordered
    ]


def load_resources(run_dir: Path) -> dict[str, Any]:
    return read_json(run_dir / "resources.json", default={"resources": []})


def cleanup_run(run_dir: Path, conn: Any | None = None, *, dry_run: bool = False) -> list[dict[str, Any]]:
    resources = load_resources(run_dir)
    actions = dry_run_cleanup(resources)
    if dry_run or conn is None:
        return actions
    for action in actions:
        _delete_resource(conn, action)
    return actions


def _delete_resource(conn: Any, action: dict[str, Any]) -> None:
    resource_type = action["type"]
    resource_id = action["id"]
    if resource_type == "server":
        conn.compute.delete_server(resource_id, ignore_missing=True)
    elif resource_type == "volume":
        conn.block_storage.delete_volume(resource_id, ignore_missing=True)
    elif resource_type == "floating_ip":
        conn.network.delete_ip(resource_id, ignore_missing=True)
    elif resource_type == "port":
        conn.network.delete_port(resource_id, ignore_missing=True)
    elif resource_type == "security_group":
        conn.network.delete_security_group(resource_id, ignore_missing=True)
    elif resource_type == "router":
        conn.network.delete_router(resource_id, ignore_missing=True)
    elif resource_type == "subnet":
        conn.network.delete_subnet(resource_id, ignore_missing=True)
    elif resource_type == "network":
        conn.network.delete_network(resource_id, ignore_missing=True)
    elif resource_type == "keypair":
        conn.compute.delete_keypair(resource_id, ignore_missing=True)
    elif resource_type == "user":
        conn.identity.delete_user(resource_id, ignore_missing=True)
    elif resource_type == "project":
        conn.identity.delete_project(resource_id, ignore_missing=True)
