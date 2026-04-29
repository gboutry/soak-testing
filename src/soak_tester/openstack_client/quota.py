from __future__ import annotations

from typing import Any


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
