from __future__ import annotations

import secrets
from typing import Any


def get_role(conn: Any, role_name: str) -> Any:
    role = conn.identity.find_role(role_name)
    if role is None:
        raise RuntimeError(f"required role not found: {role_name}")
    return role


def create_project_user(conn: Any, *, project_name: str, username: str, role_name: str) -> dict[str, Any]:
    project = conn.identity.create_project(name=project_name, enabled=True)
    password = secrets.token_urlsafe(32)
    user = conn.identity.create_user(name=username, password=password, enabled=True)
    role = get_role(conn, role_name)
    conn.identity.assign_project_role_to_user(project=project.id, user=user.id, role=role.id)
    return {"project": project, "user": user, "password": password, "role": role}
