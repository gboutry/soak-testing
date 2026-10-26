from pathlib import Path

import pytest

from soak_tester.config import load_config
from soak_tester.util import parse_duration


FIXTURE = Path(__file__).parent / "fixtures" / "config.yaml"


@pytest.mark.parametrize("sources", [[], ["n1"], ["n1", "n1"], ["unknown"]])
def test_retired_network_source_hosts_are_rejected(sources):
    from soak_tester.config import NetworkWorkloadConfig

    with pytest.raises(ValueError, match="flow_source_hosts"):
        NetworkWorkloadConfig(hosts=["n1", "n2"], topology="full_mesh", flow_source_hosts=sources)


def test_retired_source_hosts_are_rejected_without_explicit_full_mesh():
    from soak_tester.config import NetworkWorkloadConfig

    with pytest.raises(ValueError, match="flow_source_hosts"):
        NetworkWorkloadConfig(topology="full_mesh", flow_source_hosts=["n1"])
    with pytest.raises(ValueError, match="flow_source_hosts"):
        NetworkWorkloadConfig(hosts=["n1", "n2"], flow_source_hosts=["n1"])


def test_load_config_accepts_example_schema():
    config = load_config(FIXTURE)

    assert config.boot_concurrency == 1
    assert config.workloads.api.engine == "native"
    assert config.openstack.external_network == "ext-net"
    assert config.targets.compute.vcpus == 960
    assert config.targets.network.gbps == 600
    assert config.resources.network_cidr == "10.250.0.0/24"
    assert config.resources.network_mtu is None
    assert config.workloads.network.vnic_type == "normal"
    assert config.workloads.storage.volume_type == "ceph-ssd"
    assert config.observability.prometheus.query_packs == ["node-exporter-network"]


def test_load_config_accepts_global_boot_concurrency(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
boot_concurrency: 12
management:
  allowed_cidrs: [198.51.100.10/32]
openstack:
  external_network: ext-net
  image: ubuntu
flavors:
  controller: m1.small
  compute_worker: m1.small
  network_worker: m1.small
  storage_worker: m1.small
targets:
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


def test_load_config_accepts_optional_network_mtu(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
management:
  allowed_cidrs: [198.51.100.10/32]
openstack:
  external_network: ext-net
  image: ubuntu
flavors:
  controller: m1.small
  compute_worker: m1.small
  network_worker: m1.small
  storage_worker: m1.small
resources:
  network_mtu: 8942
targets:
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

    assert config.resources.network_mtu == 8942


def test_api_operation_mix_rejects_unknown_operations(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
management:
  allowed_cidrs: [198.51.100.10/32]
openstack:
  external_network: ext-net
  image: ubuntu
flavors:
  controller: m1.small
  compute_worker: m1.small
  network_worker: m1.small
  storage_worker: m1.small
targets:
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
management:
  allowed_cidrs: [198.51.100.10/32]
openstack:
  external_network: ext-net
  image: ubuntu
flavors:
  controller: m1.small
  compute_worker: m1.small
  network_worker: m1.small
  storage_worker: m1.small
targets:
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


def test_api_workload_accepts_rally_engine(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
management:
  allowed_cidrs: [198.51.100.10/32]
openstack:
  external_network: ext-net
  image: ubuntu
flavors:
  controller: m1.small
  compute_worker: m1.small
  network_worker: m1.small
  storage_worker: m1.small
targets:
  compute:
    vcpus: 1
    ram_gib: 1
  network:
    gbps: 1
  storage:
    read_gbps: 1
    write_gbps: 1
    iops: 1
workloads:
  api:
    engine: rally
    rally_mode: single_task
    rally_concurrency: 12
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.workloads.api.engine == "rally"
    assert config.workloads.api.rally_mode == "single_task"
    assert config.workloads.api.rally_concurrency == 12


def test_compute_workload_accepts_explicit_worker_count(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
management:
  allowed_cidrs: [198.51.100.10/32]
openstack:
  external_network: ext-net
  image: ubuntu
flavors:
  controller: m1.small
  compute_worker: m1.small
  network_worker: m1.small
  storage_worker: m1.small
targets:
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
  compute:
    worker_count: 282
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.workloads.compute.worker_count == 282


def test_network_reverse_and_bidirectional_are_mutually_exclusive():
    config = load_config(FIXTURE)
    from soak_tester.config import dump_config
    import yaml

    payload = yaml.safe_load(dump_config(config))
    payload["workloads"]["network"]["reverse"] = True
    payload["workloads"]["network"]["bidirectional"] = True

    with pytest.raises(ValueError, match="mutually exclusive"):
        type(config).model_validate(payload)


def test_network_workload_accepts_direct_vnic_type(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
management:
  allowed_cidrs: [198.51.100.10/32]
openstack:
  external_network: ext-net
  image: ubuntu
flavors:
  controller: m1.small
  compute_worker: m1.small
  network_worker: m1.small
  storage_worker: m1.small
targets:
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
  network:
    vnic_type: direct
    hardware_offload_type: switchdev
    port_security_enabled: false
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.workloads.network.vnic_type == "direct"
    assert config.workloads.network.hardware_offload_type == "switchdev"
    assert config.workloads.network.port_security_enabled is False


def test_load_config_accepts_targets_without_legacy_capacity(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
management:
  allowed_cidrs: [198.51.100.10/32]
openstack:
  external_network: ext-net
  image: ubuntu
flavors:
  controller: m1.small
  compute_worker: m1.small
  network_worker: m1.small
  storage_worker: m1.small
targets:
  compute:
    vcpus: 1128
    ram_gib: 4512
  network:
    gbps: 100
    scope: per_host
  storage:
    read_gbps: 4
    write_gbps: 2
    iops: 100000
  api:
    ops_per_second: 32
workloads:
  network:
    topology: full_mesh
  compute:
    worker_count: 282
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.targets.compute.vcpus == 1128
    assert config.workloads.compute.worker_count == 282


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("5m", 300), ("8h", 28800), ("1d", 86400)],
)
def test_parse_duration_supported_units(text, seconds):
    assert parse_duration(text).total_seconds() == seconds


def test_parse_duration_rejects_invalid_value():
    with pytest.raises(ValueError):
        parse_duration("forever")


@pytest.mark.parametrize("queues,vnic_type", [(0, "normal"), (16, "direct")])
def test_guest_queue_tuning_rejects_invalid_requests(queues, vnic_type):
    from soak_tester.config import NetworkWorkloadConfig

    with pytest.raises(ValueError):
        NetworkWorkloadConfig(guest_combined_queues=queues, vnic_type=vnic_type)


@pytest.mark.parametrize("size,vnic_type", [(0, "normal"), (1024, "direct")])
def test_guest_rx_ring_tuning_rejects_invalid_requests(size, vnic_type):
    from soak_tester.config import NetworkWorkloadConfig

    with pytest.raises(ValueError):
        NetworkWorkloadConfig(guest_rx_ring_size=size, vnic_type=vnic_type)


@pytest.mark.parametrize("field,value", [("capacity", {}), ("monitoring", {}), ("calibration", {})])
def test_retired_top_level_fields_are_rejected(field, value):
    import yaml
    from soak_tester.config import SoakConfig

    payload = yaml.safe_load(FIXTURE.read_text())
    payload[field] = value
    with pytest.raises(ValueError, match="Extra inputs"):
        SoakConfig.model_validate(payload)


def test_unknown_nested_field_is_rejected():
    import yaml
    from soak_tester.config import SoakConfig

    payload = yaml.safe_load(FIXTURE.read_text())
    payload["workloads"]["network"]["paralell_streams_per_flow"] = 99
    with pytest.raises(ValueError, match="paralell_streams_per_flow"):
        SoakConfig.model_validate(payload)


def test_disabled_planes_need_no_placeholders_and_roundtrip():
    import yaml
    from soak_tester.config import SoakConfig, dump_config

    config = load_config(Path(__file__).parents[1] / "configs/network-sriov.yaml")
    assert config.targets.compute is None
    assert config.flavors.compute_worker is None
    assert SoakConfig.model_validate(yaml.safe_load(dump_config(config))) == config
    payload = yaml.safe_load(dump_config(config))
    payload["workloads"]["compute"]["worker_count"] = 0
    with pytest.raises(ValueError):
        SoakConfig.model_validate(payload)


@pytest.mark.parametrize(
    "values",
    [{"flow_duration": "0s"}, {"hosts": ["n1", "n1"]}, {"hardware_offload_type": "switchdev"}],
)
def test_unsupported_network_settings_are_rejected(values):
    from soak_tester.config import NetworkWorkloadConfig

    with pytest.raises(ValueError):
        NetworkWorkloadConfig(**values)


@pytest.mark.parametrize(
    "name",
    ["compute", "network-virtio", "network-sriov", "storage", "api-native", "api-rally", "mixed"],
)
def test_maintained_profiles_are_strict_and_roundtrip(name):
    import yaml
    from soak_tester.config import SoakConfig, dump_config

    config = load_config(Path(__file__).parents[1] / f"configs/{name}.yaml")
    assert SoakConfig.model_validate(yaml.safe_load(dump_config(config))) == config


@pytest.mark.parametrize(
    "profile",
    ["sequential-write", "sequential-read", "random-write", "random-read", "mixed-randrw"],
)
def test_active_storage_profiles_effective_config_roundtrip(profile):
    import yaml
    from soak_tester.config import SoakConfig, dump_config

    config = load_config(Path(__file__).parents[1] / "configs/storage.yaml")
    config.workloads.storage.profile = profile
    payload = yaml.safe_load(dump_config(config))
    assert ("rwmixread" in payload["workloads"]["storage"]) == (profile == "mixed-randrw")
    assert SoakConfig.model_validate(payload) == config


def test_campaign_schema_rejects_unknown_fields_duplicate_names_and_unsafe_names(tmp_path):
    from soak_tester.config import CampaignConfig

    payload = {
        "timezone": "UTC",
        "latest_finish": "2099-01-01T00:00:00Z",
        "os_cloud": "test",
        "target": 100,
        "campaigns": [{"name": "network", "config": str(FIXTURE), "duration": "1m"}],
    }
    assert len(CampaignConfig.model_validate(payload).campaigns) == 1
    for key, value in [
        ("typo", True),
        ("campaigns", [payload["campaigns"][0]] * 2),
        ("campaigns", [{"name": "../outside", "config": str(FIXTURE), "duration": "1m"}]),
    ]:
        with pytest.raises(ValueError):
            CampaignConfig.model_validate({**payload, key: value})


@pytest.mark.parametrize("sources", [[], ["n1", "n1"], ["unknown"]])
def test_network_source_hosts_reject_invalid_selection(sources):
    from soak_tester.config import NetworkWorkloadConfig

    with pytest.raises(ValueError, match="flow_source_hosts"):
        NetworkWorkloadConfig(hosts=["n1", "n2"], topology="full_mesh", flow_source_hosts=sources)


def test_network_source_hosts_require_explicit_full_mesh_hosts():
    from soak_tester.config import NetworkWorkloadConfig

    with pytest.raises(ValueError, match="flow_source_hosts"):
        NetworkWorkloadConfig(host_count=2, topology="full_mesh", flow_source_hosts=["n1"])
    with pytest.raises(ValueError, match="flow_source_hosts"):
        NetworkWorkloadConfig(hosts=["n1", "n2"], flow_source_hosts=["n1"])


@pytest.mark.parametrize("usecs", [-1, 1001])
def test_guest_interrupt_coalescing_rejects_out_of_range_delays(usecs):
    from soak_tester.config import NetworkWorkloadConfig

    with pytest.raises(ValueError):
        NetworkWorkloadConfig(guest_interrupt_coalesce_usecs=usecs)


@pytest.mark.parametrize("usecs", [None, 0, 50, 1000])
def test_guest_interrupt_coalescing_accepts_unset_zero_and_fixed_delays(usecs):
    from soak_tester.config import NetworkWorkloadConfig

    assert (
        NetworkWorkloadConfig(guest_interrupt_coalesce_usecs=usecs).guest_interrupt_coalesce_usecs
        == usecs
    )


def test_udp_configuration_rejects_unsupported_direction_and_invalid_datagrams():
    import pytest
    from pydantic import ValidationError
    from soak_tester.config import NetworkWorkloadConfig

    assert NetworkWorkloadConfig(protocol="udp", udp_datagram_size=8192).protocol == "udp"
    for kwargs in [
        dict(udp_datagram_size=8192),
        dict(protocol="udp", reverse=True),
        dict(protocol="udp", bidirectional=True),
        dict(protocol="udp", udp_datagram_size=65508),
    ]:
        with pytest.raises(ValidationError):
            NetworkWorkloadConfig(**kwargs)


def test_large_receive_offload_defaults_unchanged_and_requires_tcp():
    from soak_tester.config import NetworkWorkloadConfig

    assert NetworkWorkloadConfig().guest_large_receive_offload is None
    assert NetworkWorkloadConfig(guest_large_receive_offload=True).guest_large_receive_offload
    with pytest.raises(ValueError, match="requires TCP"):
        NetworkWorkloadConfig(protocol="udp", guest_large_receive_offload=True)


def test_guest_tx_qdisc_is_opt_in_and_rejects_unknown_policies():
    from pydantic import ValidationError
    from soak_tester.config import NetworkWorkloadConfig

    assert NetworkWorkloadConfig().guest_tx_qdisc is None
    assert NetworkWorkloadConfig(guest_tx_qdisc="mq_fq").guest_tx_qdisc == "mq_fq"
    with pytest.raises(ValidationError):
        NetworkWorkloadConfig(guest_tx_qdisc="cake")


@pytest.mark.parametrize("protocol", ["tcp", "udp"])
def test_integrated_network_options_roundtrip(protocol):
    import yaml
    from soak_tester.config import SoakConfig, dump_config

    cfg = load_config(Path(__file__).parents[1] / "configs/network-sriov.yaml")
    cfg.workloads.network.protocol = protocol
    cfg.workloads.network.guest_interrupt_coalesce_usecs = 50
    cfg.workloads.network.guest_tx_qdisc = "mq_fq"
    cfg.workloads.network.capture_receiver_intervals = True
    if protocol == "udp":
        cfg.workloads.network.udp_datagram_size = 8192
    else:
        cfg.workloads.network.guest_large_receive_offload = True
    assert SoakConfig.model_validate(yaml.safe_load(dump_config(cfg))) == cfg
