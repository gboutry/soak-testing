from __future__ import annotations

import base64
import concurrent.futures
import threading
import uuid
import secrets
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from soak_tester.config import SoakConfig
from soak_tester.openstack_client.compute import placement_record, server_private_ip
from soak_tester.openstack_client.identity import create_project_user, get_role
from soak_tester.openstack_client.network import create_security_group_rules
from soak_tester.openstack_client.quota import set_unlimited_project_quotas
from soak_tester.openstack_client.resources import (
    owned_metadata,
    resource_record,
    tags_from_metadata,
)
from soak_tester.planner import SoakPlan
from soak_tester.run.collect import wait_for_worker_heartbeats
from soak_tester.run.controller import controller_cloud_init
from soak_tester.util import atomic_write_json, atomic_write_text, duration_seconds, ensure_dir
from soak_tester.workloads.cloudinit import worker_cloud_init


class ResourceTracker:
    def __init__(self, run_dir: Path, *, stop_requested=lambda: False):
        self.run_dir = run_dir
        self.stop_requested = stop_requested
        self.path = run_dir / "resources.json"
        if self.path.exists():
            raise FileExistsError(f"Refusing to overwrite the resource manifest: {self.path}")
        self.resources: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.flush()

    def add(self, record: dict[str, Any]) -> None:
        with self._lock:
            self.resources.append(record)
            self._flush_unlocked()
        if self.stop_requested():
            raise InterruptedError("Stop requested after recording created resource")

    def flush(self) -> None:
        with self._lock:
            self._flush_unlocked()

    def _flush_unlocked(self) -> None:
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


def provision_openstack_run(
    *,
    run_dir: Path,
    run_id: str,
    project_name: str,
    config: SoakConfig,
    plan: SoakPlan,
    admin_conn: Any,
    duration: str,
    stop_requested=lambda: False,
) -> dict[str, Any]:
    tracker = ResourceTracker(run_dir, stop_requested=stop_requested)
    control_token = secrets.token_urlsafe(32)
    atomic_write_text(run_dir / "controller-token", control_token, mode=0o600)
    description = f"soak-tester owned run {run_id} {uuid.uuid4()}"
    atomic_write_json(
        run_dir / "ownership-intent.json", {"name": project_name, "description": description}
    )
    project_metadata = owned_metadata(run_id, "control", "project", project_name)

    def record_identity(kind: str, resource: Any) -> None:
        tracker.add(
            resource_record(
                kind,
                resource.id,
                resource.name,
                plane="control",
                role=kind,
                metadata=project_metadata,
            )
        )

    identity = create_project_user(
        admin_conn,
        project_name=project_name,
        username=project_name,
        role_name=config.openstack.member_role,
        record_created=record_identity,
        description=description,
        project_domain_name=config.openstack.project_domain_name,
        user_domain_name=config.openstack.user_domain_name,
    )
    project = identity["project"]
    user = identity["user"]
    password = identity["password"]
    project_id = project.id
    if _plan_uses_real_host_placement(plan) or config.workloads.api.engine == "rally":
        admin_role = get_role(admin_conn, config.openstack.admin_role)
        admin_conn.identity.assign_project_role_to_user(
            project=project.id,
            user=user.id,
            role=admin_role.id,
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
    network = _create_private_network(
        project_conn,
        tracker,
        project_name=project_name,
        project_id=project_id,
        run_id=run_id,
        config=config,
        metadata=control_meta,
    )

    subnet = project_conn.network.create_subnet(
        name=f"{project_name}-subnet",
        network_id=network.id,
        ip_version=4,
        cidr=config.resources.network_cidr,
        project_id=project_id,
    )
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
    _tag(project_conn, subnet, control_meta)

    external = (
        project_conn.network.find_network(config.openstack.external_network)
        if config.resources.create_router
        else None
    )
    router = None
    if config.resources.create_router:
        router = project_conn.network.create_router(
            name=f"{project_name}-router",
            external_gateway_info={"network_id": external.id},
            project_id=project_id,
        )
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
        _tag(project_conn, router, control_meta)
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
    _tag(project_conn, security_group, control_meta)
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
    placement_lock = threading.Lock()
    controller = _boot_server(
        project_conn,
        name=f"{project_name}-controller",
        image_id=image.id,
        flavor_name=config.flavors.controller,
        network_id=network.id,
        security_group_name=security_group.name,
        key_name=keypair.name,
        user_data=controller_cloud_init(
            token=control_token, port=config.management.controller_status_port
        ),
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
    worker_controller_url = (
        f"http://{controller_private_ip}:{config.management.controller_status_port}"
    )
    controller_url = worker_controller_url

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
        controller_url = (
            f"http://{fip.floating_ip_address}:{config.management.controller_status_port}"
        )

    placement.append(placement_record("controller", controller, plane="control", role="controller"))
    atomic_write_json(run_dir / "placement.json", placement)

    duration_s = duration_seconds(duration)
    worker_duration_s = duration_s + 3600
    receiver_ip_by_worker_id: dict[str, str] = {}
    receiver_servers = _run_boot_tasks(
        [
            (
                f"network-receiver-{index:03d}",
                lambda index=index: _create_worker(
                    project_conn,
                    tracker,
                    placement,
                    placement_lock,
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
                    worker_controller_url,
                    worker_duration_s,
                    duration_s,
                    receiver_ports=[
                        flow.receiver_port
                        for flow in plan.network.flow_assignments
                        if flow.receiver_worker_id == f"network-receiver-{index:03d}"
                    ],
                    planned_host=plan.network.receiver_worker_hosts.get(
                        f"network-receiver-{index:03d}"
                    ),
                ),
            )
            for index in range(plan.network.receiver_count)
        ],
        concurrency=config.boot_concurrency,
    )
    for index, server in enumerate(receiver_servers):
        worker_id = f"network-receiver-{index:03d}"
        if ip := server_private_ip(server):
            receiver_ip_by_worker_id[worker_id] = ip

    if plan.network.sender_count and plan.network.receiver_count:
        _wait_for_network_receivers(
            controller_url,
            plan.network.receiver_count,
            diagnostics_path=run_dir / "readiness-receivers.jsonl",
            stop_requested=stop_requested,
        )

    _run_boot_tasks(
        [
            (
                f"network-sender-{index:03d}",
                lambda index=index: _create_worker(
                    project_conn,
                    tracker,
                    placement,
                    placement_lock,
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
                    worker_controller_url,
                    worker_duration_s,
                    duration_s,
                    flow_assignments=[
                        {
                            **flow.model_dump(mode="json"),
                            "receiver_private_ip": receiver_ip_by_worker_id.get(
                                flow.receiver_worker_id
                            ),
                        }
                        for flow in plan.network.flow_assignments
                        if flow.sender_worker_id == f"network-sender-{index:03d}"
                    ],
                    planned_host=plan.network.sender_worker_hosts.get(
                        f"network-sender-{index:03d}"
                    ),
                ),
            )
            for index in range(plan.network.sender_count)
        ],
        concurrency=config.boot_concurrency,
    )

    compute_index = 0
    compute_tasks: list[tuple[str, Callable[[], Any]]] = []
    compute_groups = plan.compute.worker_groups or [
        {"flavor": config.flavors.compute_worker, "count": plan.compute.worker_count}
    ]
    for group in compute_groups:
        for _ in range(int(group["count"])):
            index = compute_index
            flavor = str(group["flavor"])
            compute_tasks.append(
                (
                    f"compute-{index:03d}",
                    lambda index=index, flavor=flavor: _create_worker(
                        project_conn,
                        tracker,
                        placement,
                        placement_lock,
                        run_id,
                        project_name,
                        config,
                        image.id,
                        network.id,
                        security_group.name,
                        keypair.name,
                        "compute",
                        "compute",
                        index,
                        worker_controller_url,
                        worker_duration_s,
                        duration_s,
                        flavor_name_override=flavor,
                    ),
                )
            )
            compute_index += 1
    _run_boot_tasks(compute_tasks, concurrency=config.boot_concurrency)

    _run_boot_tasks(
        [
            (
                f"storage-{index:03d}",
                lambda index=index: _create_storage_worker(
                    project_conn,
                    tracker,
                    placement,
                    placement_lock,
                    run_id,
                    project_name,
                    config,
                    image.id,
                    network.id,
                    security_group.name,
                    keypair.name,
                    index,
                    worker_controller_url,
                    worker_duration_s,
                    duration_s,
                ),
            )
            for index in range(plan.storage.worker_count)
        ],
        concurrency=config.boot_concurrency,
    )

    atomic_write_json(run_dir / "placement.json", placement)
    return {"controller_url": controller_url, "project_conn": project_conn, "tracker": tracker}


def _create_private_network(
    conn: Any,
    tracker: ResourceTracker,
    *,
    project_name: str,
    project_id: str,
    run_id: str,
    config: SoakConfig,
    metadata: dict[str, str] | None = None,
) -> Any:
    metadata = metadata or owned_metadata(run_id, "control", "controller", project_name)
    payload: dict[str, Any] = {
        "name": f"{project_name}-net",
        "project_id": project_id,
        "description": f"soak-tester network for {run_id}",
    }
    if config.resources.network_mtu is not None:
        payload["mtu"] = config.resources.network_mtu
    network = conn.network.create_network(**payload)
    tracker.add(
        resource_record(
            "network",
            network.id,
            network.name,
            plane="network",
            role="private-network",
            metadata=metadata,
            extra={"mtu": config.resources.network_mtu},
        )
    )
    if config.resources.network_mtu is not None:
        actual_mtu = getattr(network, "mtu", None)
        if actual_mtu is not None and int(actual_mtu) != config.resources.network_mtu:
            raise RuntimeError(
                f"created network MTU {actual_mtu} does not match requested "
                f"{config.resources.network_mtu}"
            )
    _tag(conn, network, metadata)
    return network


def _create_worker(
    conn: Any,
    tracker: ResourceTracker,
    placement: list[dict[str, Any]],
    placement_lock: threading.Lock,
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
    workload_duration_seconds_value: int | None = None,
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
    }[plane]
    flavor_name = flavor_name_override or flavor_name
    availability_zone = _availability_zone_for_host(planned_host)
    port_id = None
    if plane == "network" and config.workloads.network.vnic_type == "direct":
        port = _create_direct_worker_port(
            conn,
            tracker,
            config=config,
            name=f"{project_name}-{worker_id}-port",
            network_id=network_id,
            security_group_name=security_group_name,
            metadata=metadata,
            role=role,
        )
        port_id = port.id
    server = _boot_server(
        conn,
        name=f"{project_name}-{worker_id}",
        image_id=image_id,
        flavor_name=flavor_name,
        network_id=network_id,
        port_id=port_id,
        security_group_name=security_group_name,
        key_name=key_name,
        user_data=worker_cloud_init(
            config=config,
            control_token=(tracker.run_dir / "controller-token").read_text()
            if (tracker.run_dir / "controller-token").exists()
            else "",
            role=role,
            worker_id=worker_id,
            controller_url=controller_url,
            duration_seconds=duration_seconds_value,
            workload_duration_seconds=workload_duration_seconds_value,
            flow_assignments=flow_assignments,
            receiver_ports=receiver_ports,
        ),
        metadata=metadata,
        availability_zone=availability_zone,
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
    with placement_lock:
        placement.append(placement_record(worker_id, server, plane=plane, role=role))
        atomic_write_json(tracker.run_dir / "placement.json", placement)
    return server


def _create_direct_worker_port(
    conn: Any,
    tracker: ResourceTracker,
    *,
    config: SoakConfig,
    name: str,
    network_id: str,
    security_group_name: str,
    metadata: dict[str, str],
    role: str,
) -> Any:
    port_payload: dict[str, Any] = {
        "name": name,
        "network_id": network_id,
        "binding_vnic_type": "direct",
    }
    if config.workloads.network.hardware_offload_type is not None:
        port_payload["hardware_offload_type"] = config.workloads.network.hardware_offload_type
    if config.workloads.network.port_security_enabled:
        security_group = conn.network.find_security_group(security_group_name)
        port_payload["security_group_ids"] = (
            [security_group.id] if security_group is not None else []
        )
    else:
        port_payload["port_security_enabled"] = False
        port_payload["security_group_ids"] = []
    port = conn.network.create_port(**port_payload)
    tracker.add(
        resource_record(
            "port",
            port.id,
            port.name,
            plane="network",
            role=role,
            metadata=metadata,
            extra={
                "vnic_type": "direct",
                "hardware_offload_type": config.workloads.network.hardware_offload_type,
                "port_security_enabled": config.workloads.network.port_security_enabled,
            },
        )
    )
    _tag(conn, port, metadata)
    return port


def _create_storage_worker(
    conn: Any,
    tracker: ResourceTracker,
    placement: list[dict[str, Any]],
    placement_lock: threading.Lock,
    run_id: str,
    project_name: str,
    config: SoakConfig,
    image_id: str,
    network_id: str,
    security_group_name: str,
    key_name: str,
    index: int,
    controller_url: str,
    duration_seconds_value: int,
    workload_duration_seconds_value: int | None = None,
) -> Any:
    worker = _create_worker(
        conn,
        tracker,
        placement,
        placement_lock,
        run_id,
        project_name,
        config,
        image_id,
        network_id,
        security_group_name,
        key_name,
        "storage",
        "storage",
        index,
        controller_url,
        duration_seconds_value,
        workload_duration_seconds_value,
    )
    storage_meta = owned_metadata(run_id, "storage", "volume", project_name)
    volume = conn.block_storage.create_volume(
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
    volume = conn.block_storage.wait_for_status(
        volume, status="available", failures=["error"], wait=900
    )
    conn.compute.create_volume_attachment(worker, volumeId=volume.id)
    return worker


def _wait_for_network_receivers(
    controller_url: str | None,
    receiver_count: int,
    *,
    diagnostics_path: Path | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> None:
    if not controller_url:
        raise RuntimeError("A reachable controller is required for network receiver readiness")
    if not wait_for_worker_heartbeats(
        controller_url,
        expected_worker_ids={f"network-receiver-{index:03d}" for index in range(receiver_count)},
        timeout_seconds=300,
        **({"stop_requested": stop_requested} if stop_requested is not None else {}),
        **({"diagnostics_path": diagnostics_path} if diagnostics_path is not None else {}),
    ):
        raise RuntimeError("Network receivers did not become ready before sender boot")


def _run_boot_tasks(
    tasks: list[tuple[str, Callable[[], Any]]],
    *,
    concurrency: int,
) -> list[Any]:
    if not tasks:
        return []
    if concurrency <= 1 or len(tasks) == 1:
        return [task() for _, task in tasks]
    results: list[Any] = [None] * len(tasks)
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_index = {executor.submit(task): index for index, (_, task) in enumerate(tasks)}
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            try:
                results[index] = future.result()
            except BaseException as exc:
                for pending in future_to_index:
                    pending.cancel()
                if isinstance(exc, (InterruptedError, KeyboardInterrupt)):
                    raise
                name = tasks[index][0]
                raise RuntimeError(f"worker boot failed for {name}") from exc
    return results


def _boot_server(
    conn: Any,
    *,
    name: str,
    image_id: str,
    flavor_name: str,
    network_id: str,
    port_id: str | None = None,
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
        "networks": [{"port": port_id}] if port_id else [{"uuid": network_id}],
        "key_name": key_name,
        "user_data": base64.b64encode(user_data.encode()).decode(),
        "metadata": metadata,
    }
    if not port_id:
        payload["security_groups"] = [{"name": security_group_name}]
    if availability_zone:
        payload["availability_zone"] = availability_zone
    return conn.compute.create_server(**payload)


def _availability_zone_for_host(host: str | None) -> str | None:
    if not host:
        return None
    return f"nova:{host}"


def _plan_uses_real_host_placement(plan: SoakPlan) -> bool:
    hosts = set(plan.network.receiver_worker_hosts.values())
    hosts.update(plan.network.sender_worker_hosts.values())
    return bool(hosts)


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
