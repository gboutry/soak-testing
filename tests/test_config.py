from pathlib import Path

import pytest

from soak_tester.config import load_config
from soak_tester.util import parse_duration


FIXTURE = Path(__file__).parent / "fixtures" / "config.yaml"


def test_load_config_accepts_example_schema():
    config = load_config(FIXTURE)

    assert config.boot_concurrency == 1
    assert config.openstack.external_network == "ext-net"
    assert config.capacity.compute.cpu_resource_class == "auto"
    assert config.capacity.compute.ram_resource_class == "MEMORY_MB"
    assert config.capacity.compute.per_host_ram_gib is None
    assert config.calibration.pass_ratio == 0.95
    assert config.calibration.network_direction == "egress"
    assert config.resources.network_cidr == "10.250.0.0/24"
    assert config.workloads.storage.volume_type == "ceph-ssd"
    assert config.observability.prometheus.query_packs == ["node-exporter-network"]


def test_load_config_accepts_global_boot_concurrency(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
boot_concurrency: 12
openstack:
  external_network: ext-net
  image: ubuntu
flavors:
  controller: m1.small
  compute_worker: m1.small
  network_worker: m1.small
  storage_worker: m1.small
  api_worker: m1.small
capacity:
  compute:
    vcpus: 1
    ram_gib: 1
  network:
    gbps: 1
  storage:
    read_gbps: 1
    write_gbps: 1
    iops: 1
  api:
    ops_per_second: 1
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.boot_concurrency == 12


def test_api_operation_mix_rejects_unknown_operations(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
openstack:
  external_network: ext-net
  image: ubuntu
flavors:
  controller: m1.small
  compute_worker: m1.small
  network_worker: m1.small
  storage_worker: m1.small
  api_worker: m1.small
capacity:
  compute:
    vcpus: 1
    ram_gib: 1
  network:
    gbps: 1
  storage:
    read_gbps: 1
    write_gbps: 1
    iops: 1
  api:
    ops_per_second: 1
workloads:
  api:
    operation_mix:
      list_servers: 1
      delete_everything: 1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported API operations"):
        load_config(path)


def test_api_operation_mix_rejects_non_positive_weights(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
openstack:
  external_network: ext-net
  image: ubuntu
flavors:
  controller: m1.small
  compute_worker: m1.small
  network_worker: m1.small
  storage_worker: m1.small
  api_worker: m1.small
capacity:
  compute:
    vcpus: 1
    ram_gib: 1
  network:
    gbps: 1
  storage:
    read_gbps: 1
    write_gbps: 1
    iops: 1
  api:
    ops_per_second: 1
workloads:
  api:
    operation_mix:
      list_servers: 0
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="positive integer"):
        load_config(path)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("5m", 300), ("8h", 28800), ("1d", 86400)],
)
def test_parse_duration_supported_units(text, seconds):
    assert parse_duration(text).total_seconds() == seconds


def test_parse_duration_rejects_invalid_value():
    with pytest.raises(ValueError):
        parse_duration("forever")
