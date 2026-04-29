from __future__ import annotations

from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from soak_tester.config import SoakConfig
from soak_tester.openstack_client.resources import owned_metadata, resource_record
from soak_tester.util import atomic_write_json, ensure_dir


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
