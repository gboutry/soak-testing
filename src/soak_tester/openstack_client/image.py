from __future__ import annotations

from typing import Any


def require_image(conn: Any, name: str) -> Any:
    image = conn.image.find_image(name)
    if image is None:
        raise RuntimeError(f"image not found: {name}")
    return image
