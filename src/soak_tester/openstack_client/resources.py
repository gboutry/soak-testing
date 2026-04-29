from __future__ import annotations

from typing import Any


def owned_metadata(run_id: str, plane: str, role: str, project_name: str) -> dict[str, str]:
    return {
        "soak_tester_run_id": run_id,
        "soak_tester_owned": "true",
        "soak_tester_plane": plane,
        "soak_tester_role": role,
        "soak_tester_project": project_name,
    }


def tags_from_metadata(metadata: dict[str, str]) -> list[str]:
    return [f"{key}={value}" for key, value in metadata.items()]


def resource_record(
    resource_type: str,
    resource_id: str,
    name: str,
    *,
    plane: str,
    role: str,
    metadata: dict[str, str],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": resource_type,
        "id": resource_id,
        "name": name,
        "plane": plane,
        "role": role,
        "metadata": metadata,
        "extra": extra or {},
    }
