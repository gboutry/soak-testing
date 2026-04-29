from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from soak_tester.config import SoakConfig
from soak_tester.openstack_client.compute import placement_record, server_private_ip
from soak_tester.openstack_client.identity import create_project_user
from soak_tester.openstack_client.network import create_security_group_rules
from soak_tester.openstack_client.quota import set_unlimited_project_quotas
from soak_tester.openstack_client.resources import owned_metadata, resource_record, tags_from_metadata
from soak_tester.planner import SoakPlan
from soak_tester.run.controller import controller_cloud_init
from soak_tester.util import atomic_write_json, duration_seconds, ensure_dir
from soak_tester.workloads.cloudinit import worker_cloud_init


class ResourceTracker:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.path = run_dir / "resources.json"
        self.resources: list[dict[str, Any]] = []
        self.flush()

    def add(self, record: dict[str, Any]) -> None:
        self.resources.append(record)
        self.flush()

    def flush(self) -> None:
        atomic_write_json(self.path, {"resources": self.resources})


def generate_ssh_keypair(run_dir: Path) -> tuple[str, str]:
    ssh_dir = ensure_dir(run_dir / "ssh")
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    private_path = ssh_dir / "id_ed25519"
    public_path = ssh_dir / "id_ed25519.pub"
    private_path.write_bytes(private_bytes)
    private_path.chmod(0o600)
    public_path.write_bytes(public_bytes + b"\n")
    return private_path.read_text(encoding="utf-8"), public_path.read_text(encoding="utf-8").strip()


def record_auth(run_dir: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(run_dir / "auth.json", payload, mode=0o600)


def provision_skeleton(
    *,
    run_dir: Path,
    run_id: str,
    project_name: str,
    config: SoakConfig,
    conn: Any | None = None,
) -> ResourceTracker:
    tracker = ResourceTracker(run_dir)
    metadata = owned_metadata(run_id, "control", "controller", project_name)
    tracker.add(
        resource_record(
            "project",
            project_name,
            project_name,
            plane="control",
            role="project",
            metadata=metadata,
        )
    )
    generate_ssh_keypair(run_dir)
    record_auth(
        run_dir,
        {
            "project_name": project_name,
            "username": project_name,
            "password": "<generated-at-runtime>",
            "user_domain_name": config.openstack.user_domain_name,
            "project_domain_name": config.openstack.project_domain_name,
        },
    )
    return tracker


def provision_openstack_run(
    *,
    run_dir: Path,
    run_id: str,
    project_name: str,
    config: SoakConfig,
    plan: SoakPlan,
    admin_conn: Any,
    duration: str,
) -> dict[str, Any]:
    tracker = ResourceTracker(run_dir)
    identity = create_project_user(
        admin_conn,
        project_name=project_name,
        username=project_name,
        role_name=config.openstack.member_role,
    )
    project = identity["project"]
    user = identity["user"]
    password = identity["password"]
    project_id = project.id
    project_metadata = owned_metadata(run_id, "control", "project", project_name)
    tracker.add(
        resource_record(
            "project",
            project.id,
            project.name,
            plane="control",
            role="project",
            metadata=project_metadata,
        )
    )
    tracker.add(
        resource_record(
            "user",
            user.id,
            user.name,
            plane="control",
            role="user",
            metadata=project_metadata,
        )
    )
    quota_result = set_unlimited_project_quotas(admin_conn, project_id)
    tracker.add(
        resource_record(
            "quota",
            project_id,
            f"{project_name}-quotas",
            plane="control",
            role="quota",
            metadata=project_metadata,
            extra={"quotas": quota_result},
        )
    )
    record_auth(
        run_dir,
        {
            "auth_url": admin_conn.config.auth.get("auth_url"),
            "project_name": project_name,
            "project_id": project_id,
            "username": user.name,
            "user_id": user.id,
            "password": password,
            "user_domain_name": config.openstack.user_domain_name,
            "project_domain_name": config.openstack.project_domain_name,
        },
    )

    auth = {
        "auth_url": admin_conn.config.auth.get("auth_url"),
        "username": user.name,
        "password": password,
        "project_name": project_name,
        "user_domain_name": config.openstack.user_domain_name,
        "project_domain_name": config.openstack.project_domain_name,
    }
    import openstack

    project_conn = openstack.connection.Connection(
        auth=auth,
        region_name=getattr(admin_conn.config, "region_name", None),
        interface=getattr(admin_conn.config, "interface", None),
        identity_api_version="3",
    )
    project_conn.authorize()

    private_key, public_key = generate_ssh_keypair(run_dir)
    del private_key

    control_meta = owned_metadata(run_id, "control", "controller", project_name)
    network = project_conn.network.create_network(
        name=f"{project_name}-net",
        project_id=project_id,
        description=f"soak-tester network for {run_id}",
    )
    _tag(project_conn, network, control_meta)
    tracker.add(
        resource_record(
            "network",
            network.id,
            network.name,
            plane="network",
            role="private-network",
            metadata=control_meta,
        )
    )

    subnet = project_conn.network.create_subnet(
        name=f"{project_name}-subnet",
        network_id=network.id,
        ip_version=4,
        cidr=config.resources.network_cidr,
        project_id=project_id,
    )
    _tag(project_conn, subnet, control_meta)
    tracker.add(
        resource_record(
            "subnet",
            subnet.id,
            subnet.name,
            plane="network",
            role="private-subnet",
            metadata=control_meta,
        )
    )

    external = project_conn.network.find_network(config.openstack.external_network)
    router = None
    if config.resources.create_router:
        router = project_conn.network.create_router(
            name=f"{project_name}-router",
            external_gateway_info={"network_id": external.id},
            project_id=project_id,
        )
        _tag(project_conn, router, control_meta)
        tracker.add(
            resource_record(
                "router",
                router.id,
                router.name,
                plane="network",
                role="router",
                metadata=control_meta,
            )
        )
        project_conn.network.add_interface_to_router(router, subnet_id=subnet.id)
        tracker.add(
            resource_record(
                "router_interface",
                f"{router.id}:{subnet.id}",
                f"{router.name}:{subnet.name}",
                plane="network",
                role="router-interface",
                metadata=control_meta,
                extra={"router_id": router.id, "subnet_id": subnet.id},
            )
        )

    security_group = project_conn.network.create_security_group(
        name=f"{project_name}-sg",
        description=f"soak-tester security group for {run_id}",
        project_id=project_id,
    )
    _tag(project_conn, security_group, control_meta)
    tracker.add(
        resource_record(
            "security_group",
            security_group.id,
            security_group.name,
            plane="network",
            role="security-group",
            metadata=control_meta,
        )
    )
    create_security_group_rules(
        project_conn,
        security_group,
        cidr=config.resources.network_cidr,
        allowed_cidrs=config.management.allowed_cidrs,
        port=config.management.controller_status_port,
    )

    keypair = project_conn.compute.create_keypair(name=f"{project_name}-key", public_key=public_key)
    tracker.add(
        resource_record(
            "keypair",
            keypair.name,
            keypair.name,
            plane="control",
            role="keypair",
            metadata=control_meta,
        )
    )

    image = project_conn.image.find_image(config.openstack.image)
    placement: list[dict[str, Any]] = []
    controller = _boot_server(
        project_conn,
        name=f"{project_name}-controller",
        image_id=image.id,
        flavor_name=config.flavors.controller,
        network_id=network.id,
        security_group_name=security_group.name,
        key_name=keypair.name,
        user_data=controller_cloud_init(),
        metadata=control_meta,
    )
    tracker.add(
        resource_record(
            "server",
            controller.id,
            controller.name,
            plane="control",
            role="controller",
            metadata=control_meta,
        )
    )
    controller = project_conn.compute.wait_for_server(
        controller, status="ACTIVE", failures=["ERROR"], wait=900
    )
    controller_private_ip = server_private_ip(controller)
    controller_url = f"http://{controller_private_ip}:{config.management.controller_status_port}"

    if config.resources.floating_ips.controller:
        fip = project_conn.network.create_ip(
            floating_network_id=external.id,
            project_id=project_id,
            description=f"soak-tester controller floating IP for {run_id}",
        )
        tracker.add(
            resource_record(
                "floating_ip",
                fip.id,
                fip.floating_ip_address,
                plane="control",
                role="controller",
                metadata=control_meta,
            )
        )
        controller_port = _server_port(project_conn, controller.id)
        project_conn.network.update_ip(fip, port_id=controller_port.id)
        controller_url = f"http://{fip.floating_ip_address}:{config.management.controller_status_port}"

    placement.append(placement_record("controller", controller, plane="control", role="controller"))

    duration_s = duration_seconds(duration)
    receiver_ips: list[str] = []
    receiver_ip_by_worker_id: dict[str, str] = {}
    receiver_servers = []
    for index in range(plan.network.receiver_count):
        worker_id = f"network-receiver-{index:03d}"
        server = _create_worker(
            project_conn,
            tracker,
            placement,
            run_id,
            project_name,
            config,
            image.id,
            network.id,
            security_group.name,
            keypair.name,
            "network",
            "network-receiver",
            index,
            controller_url,
            duration_s,
            receiver_ports=[
                flow.receiver_port
                for flow in plan.network.flow_assignments
                if flow.receiver_worker_id == worker_id
            ],
            planned_host=plan.network.receiver_worker_hosts.get(worker_id),
        )
        receiver_servers.append(server)
        if ip := server_private_ip(server):
            receiver_ips.append(ip)
            receiver_ip_by_worker_id[worker_id] = ip

    for index in range(plan.network.sender_count):
        worker_id = f"network-sender-{index:03d}"
        _create_worker(
            project_conn,
            tracker,
            placement,
            run_id,
            project_name,
            config,
            image.id,
            network.id,
            security_group.name,
            keypair.name,
            "network",
            "network-sender",
            index,
            controller_url,
            duration_s,
            peers=receiver_ips,
            flow_assignments=[
                {
                    **flow.model_dump(mode="json"),
                    "receiver_private_ip": receiver_ip_by_worker_id.get(flow.receiver_worker_id),
                }
                for flow in plan.network.flow_assignments
                if flow.sender_worker_id == worker_id
            ],
            planned_host=plan.network.sender_worker_hosts.get(worker_id),
        )

    compute_index = 0
    compute_groups = plan.compute.worker_groups or [
        {"flavor": config.flavors.compute_worker, "count": plan.compute.worker_count}
    ]
    for group in compute_groups:
        for _ in range(int(group["count"])):
            _create_worker(
                project_conn,
                tracker,
                placement,
                run_id,
                project_name,
                config,
                image.id,
                network.id,
                security_group.name,
                keypair.name,
                "compute",
                "compute",
                compute_index,
                controller_url,
                duration_s,
                flavor_name_override=str(group["flavor"]),
            )
            compute_index += 1

    for index in range(plan.storage.worker_count):
        worker = _create_worker(
            project_conn,
            tracker,
            placement,
            run_id,
            project_name,
            config,
            image.id,
            network.id,
            security_group.name,
            keypair.name,
            "storage",
            "storage",
            index,
            controller_url,
            duration_s,
        )
        storage_meta = owned_metadata(run_id, "storage", "volume", project_name)
        volume = project_conn.block_storage.create_volume(
            name=f"{project_name}-storage-volume-{index:03d}",
            size=config.workloads.storage.volume_size_gib,
            volume_type=config.workloads.storage.volume_type,
            metadata=storage_meta,
        )
        tracker.add(
            resource_record(
                "volume",
                volume.id,
                volume.name,
                plane="storage",
                role="volume",
                metadata=storage_meta,
            )
        )
        volume = project_conn.block_storage.wait_for_status(
            volume, status="available", failures=["error"], wait=900
        )
        project_conn.compute.create_volume_attachment(worker, volumeId=volume.id)

    atomic_write_json(run_dir / "placement.json", placement)
    return {"controller_url": controller_url, "project_conn": project_conn, "tracker": tracker}


def _create_worker(
    conn: Any,
    tracker: ResourceTracker,
    placement: list[dict[str, Any]],
    run_id: str,
    project_name: str,
    config: SoakConfig,
    image_id: str,
    network_id: str,
    security_group_name: str,
    key_name: str,
    plane: str,
    role: str,
    index: int,
    controller_url: str,
    duration_seconds_value: int,
    peers: list[str] | None = None,
    flow_assignments: list[dict[str, Any]] | None = None,
    receiver_ports: list[int] | None = None,
    planned_host: str | None = None,
    flavor_name_override: str | None = None,
) -> Any:
    worker_id = f"{role}-{index:03d}"
    metadata = owned_metadata(run_id, plane, "worker", project_name)
    flavor_name = {
        "compute": config.flavors.compute_worker,
        "network": config.flavors.network_worker,
        "storage": config.flavors.storage_worker,
        "api": config.flavors.api_worker,
    }[plane]
    flavor_name = flavor_name_override or flavor_name
    server = _boot_server(
        conn,
        name=f"{project_name}-{worker_id}",
        image_id=image_id,
        flavor_name=flavor_name,
        network_id=network_id,
        security_group_name=security_group_name,
        key_name=key_name,
        user_data=worker_cloud_init(
            config=config,
            role=role,
            worker_id=worker_id,
            controller_url=controller_url,
            duration_seconds=duration_seconds_value,
            peers=peers,
            flow_assignments=flow_assignments,
            receiver_ports=receiver_ports,
        ),
        metadata=metadata,
        availability_zone=_availability_zone_for_host(planned_host),
    )
    tracker.add(
        resource_record(
            "server",
            server.id,
            server.name,
            plane=plane,
            role=role,
            metadata=metadata,
        )
    )
    server = conn.compute.wait_for_server(server, status="ACTIVE", failures=["ERROR"], wait=900)
    placement.append(placement_record(worker_id, server, plane=plane, role=role))
    return server


def _boot_server(
    conn: Any,
    *,
    name: str,
    image_id: str,
    flavor_name: str,
    network_id: str,
    security_group_name: str,
    key_name: str,
    user_data: str,
    metadata: dict[str, str],
    availability_zone: str | None = None,
) -> Any:
    flavor = conn.compute.find_flavor(flavor_name)
    payload = {
        "name": name,
        "image_id": image_id,
        "flavor_id": flavor.id,
        "networks": [{"uuid": network_id}],
        "security_groups": [{"name": security_group_name}],
        "key_name": key_name,
        "user_data": base64.b64encode(user_data.encode()).decode(),
        "metadata": metadata,
    }
    if availability_zone:
        payload["availability_zone"] = availability_zone
    return conn.compute.create_server(**payload)


def _availability_zone_for_host(host: str | None) -> str | None:
    if not host or host.startswith("host-"):
        return None
    return f"nova:{host}"


def _server_port(conn: Any, server_id: str) -> Any:
    ports = list(conn.network.ports(device_id=server_id))
    if not ports:
        raise RuntimeError(f"no Neutron port found for server {server_id}")
    return ports[0]


def _tag(conn: Any, resource: Any, metadata: dict[str, str]) -> None:
    if not hasattr(conn.network, "set_tags"):
        return
    try:
        conn.network.set_tags(resource, tags_from_metadata(metadata))
    except Exception:
        return
