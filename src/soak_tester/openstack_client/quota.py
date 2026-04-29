from __future__ import annotations

from typing import Any

UNLIMITED_COMPUTE_QUOTAS = {
    "cores": -1,
    "instances": -1,
    "ram": -1,
    "server_groups": -1,
    "server_group_members": -1,
}

UNLIMITED_BLOCK_STORAGE_QUOTAS = {
    "volumes": -1,
    "snapshots": -1,
    "gigabytes": -1,
    "backups": -1,
    "backup_gigabytes": -1,
    "per_volume_gigabytes": -1,
}

UNLIMITED_NETWORK_QUOTAS = {
    "network": -1,
    "subnet": -1,
    "port": -1,
    "router": -1,
    "floatingip": -1,
    "security_group": -1,
    "security_group_rule": -1,
}


def read_quotas(conn: Any, project_id: str | None = None) -> dict[str, object]:
    quotas: dict[str, object] = {}
    for name, proxy in (
        ("compute", getattr(conn, "compute", None)),
        ("network", getattr(conn, "network", None)),
        ("volume", getattr(conn, "block_storage", None)),
    ):
        if proxy is None or not hasattr(proxy, "get_quota_set"):
            continue
        try:
            quotas[name] = proxy.get_quota_set(project_id) if project_id else proxy.get_quota_set()
        except Exception as exc:  # best-effort SDK compatibility
            quotas[name] = {"unavailable": str(exc)}
    return quotas


def set_unlimited_project_quotas(conn: Any, project_id: str) -> dict[str, object]:
    results: dict[str, object] = {}
    results["compute"] = _set_quota_set(
        getattr(conn, "compute", None),
        project_id,
        UNLIMITED_COMPUTE_QUOTAS,
    )
    results["block_storage"] = _set_quota_set(
        getattr(conn, "block_storage", None),
        project_id,
        UNLIMITED_BLOCK_STORAGE_QUOTAS,
    )
    results["network"] = _set_network_quota(
        getattr(conn, "network", None),
        project_id,
        UNLIMITED_NETWORK_QUOTAS,
    )
    return results


def _set_quota_set(proxy: Any, project_id: str, quotas: dict[str, int]) -> dict[str, object]:
    if proxy is None or not hasattr(proxy, "update_quota_set"):
        return {"unavailable": "quota update API not available"}
    try:
        proxy.update_quota_set(project_id, **quotas)
        return dict(quotas)
    except Exception as exc:
        return {"unavailable": str(exc)}


def _set_network_quota(proxy: Any, project_id: str, quotas: dict[str, int]) -> dict[str, object]:
    if proxy is None:
        return {"unavailable": "quota update API not available"}
    try:
        if hasattr(proxy, "update_quota"):
            proxy.update_quota(project_id, **quotas)
        elif hasattr(proxy, "update_quota_set"):
            proxy.update_quota_set(project_id, **quotas)
        else:
            return {"unavailable": "quota update API not available"}
        return dict(quotas)
    except Exception as exc:
        return {"unavailable": str(exc)}
