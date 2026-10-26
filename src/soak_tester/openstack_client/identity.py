from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any


def get_role(conn: Any, role_name: str) -> Any:
    role = conn.identity.find_role(role_name)
    if role is None:
        raise RuntimeError(f"required role not found: {role_name}")
    return role


def create_project_user(
    conn: Any,
    *,
    project_name: str,
    username: str,
    role_name: str,
    record_created: Callable[[str, Any], None],
    description: str = "",
    project_domain_name: str | None = None,
    user_domain_name: str | None = None,
) -> dict[str, Any]:
    role = get_role(conn, role_name)
    domains = {}
    for kind, name in [("project", project_domain_name), ("user", user_domain_name)]:
        if name is not None:
            domain = conn.identity.find_domain(name)
            if domain is None:
                raise RuntimeError(f"required {kind} domain not found: {name}")
            domains[kind] = {"domain_id": domain.id}
    project = conn.identity.create_project(
        name=project_name, enabled=True, description=description, **domains.get("project", {})
    )
    record_created("project", project)
    password = secrets.token_urlsafe(32)
    user = conn.identity.create_user(
        name=username,
        password=password,
        enabled=True,
        description=description,
        **domains.get("user", {}),
    )
    record_created("user", user)
    conn.identity.assign_project_role_to_user(project=project.id, user=user.id, role=role.id)
    return {"project": project, "user": user, "password": password, "role": role}
