from __future__ import annotations

from typing import Any


def admin_connection(os_cloud: str) -> Any:
    import openstack

    return openstack.connect(cloud=os_cloud)


def project_connection(**auth_kwargs: Any) -> Any:
    import openstack

    return openstack.connection.Connection(**auth_kwargs)
