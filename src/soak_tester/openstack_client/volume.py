from __future__ import annotations

from typing import Any


def require_volume_type(conn: Any, name: str | None) -> Any | None:
    if not name:
        return None
    volume_type = conn.block_storage.find_type(name)
    if volume_type is None:
        raise RuntimeError(f"volume type not found: {name}")
    return volume_type
