import ast
import json
import os
import subprocess
import stat
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from soak_tester.config import load_config
from soak_tester.run.controller import controller_cloud_init
from soak_tester.workloads.cloudinit import worker_cloud_init


def test_compute_worker_cloud_init_posts_guest_system_metrics():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")

    script = worker_cloud_init(
        config=config,
        role="compute",
        worker_id="compute-000",
        controller_url="http://controller:8080",
        duration_seconds=60,
    )

    assert "guest.cpu_percent" in script
    assert "guest.memory_used_percent" in script
    assert "sample_system_metrics()" in script


def test_compute_worker_cloud_init_stresses_root_disk_with_stress_ng():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")

    script = worker_cloud_init(
        config=config,
        role="compute",
        worker_id="compute-000",
        controller_url="http://controller:8080",
        duration_seconds=60,
    )

    assert "'--hdd', '1'" in script
    assert "'--hdd-bytes', '10G'" in script
    assert "'--hdd-opts', 'direct,sync'" in script
    assert "'--iostat', '1'" in script


def test_worker_cloud_init_separates_agent_and_workload_duration():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")

    script = worker_cloud_init(
        config=config,
        role="storage",
        worker_id="storage-000",
        controller_url="http://controller:8080",
        duration_seconds=3660,
        workload_duration_seconds=60,
    )

    assert "AGENT_DURATION = 3660" in script
    assert "WORKLOAD_DURATION = 60" in script
    namespace = worker_namespace(script)
    command = namespace["FIO_COMMAND"]
    assert command[command.index("--runtime") + 1] == "60"


def worker_namespace(script):
    source = script.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    tree = ast.parse(source)
    tree.body = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef))
    ] + [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, (ast.Constant, ast.List, ast.Dict))
    ]
    namespace = {}
    exec(compile(tree, "guest-worker", "exec"), namespace)
    return namespace


def storage_namespace():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    return worker_namespace(
        worker_cloud_init(
            config=config,
            role="storage",
            worker_id="storage-000",
            controller_url="http://controller:8080",
            duration_seconds=60,
        )
    )


def test_storage_waits_for_attached_block_device():
    namespace = storage_namespace()
    namespace["os"] = SimpleNamespace(
        stat=Mock(
            side_effect=[
                FileNotFoundError(),
                FileNotFoundError(),
                SimpleNamespace(st_mode=stat.S_IFBLK),
            ]
        ),
        path=SimpleNamespace(realpath=lambda value: value),
    )
    namespace["time"] = SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 1]), sleep=Mock())
    namespace["read_text"] = lambda _: "/dev/vda1 / ext4 rw 0 0"
    assert namespace["wait_for_storage_device"]() == "/dev/vdb"
    namespace["time"].sleep.assert_called_once_with(1)


def test_storage_refuses_mounted_device():
    import pytest

    namespace = storage_namespace()
    namespace["os"] = SimpleNamespace(
        stat=lambda _: SimpleNamespace(st_mode=stat.S_IFBLK),
        path=SimpleNamespace(realpath=lambda value: value),
    )
    namespace["read_text"] = lambda _: "/dev/vdb1 / ext4 rw 0 0"
    with pytest.raises(RuntimeError, match="Refusing fio on mounted device"):
        namespace["wait_for_storage_device"]()


def test_guest_fio_failure_is_not_reported_as_completed(tmp_path):
    namespace = storage_namespace()
    path = tmp_path / "fio.json"
    path.write_text(json.dumps({"jobs": [{"error": 19}]}))
    namespace["open"] = lambda *args: path.open()
    namespace["os"] = SimpleNamespace(path=SimpleNamespace(exists=lambda _: True))
    posts = []
    namespace["post"] = lambda endpoint, payload: posts.append(payload)
    namespace["post_many"] = lambda endpoint, payload: posts.extend(payload)
    namespace["parse_fio_output"]()
    assert any(item.get("event") == "fio_failed" for item in posts)
    assert not any(item.get("event") == "fio_completed" for item in posts)


def test_network_worker_cloud_init_includes_iperf_tuning_options():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.workloads.network.omit_seconds = 10
    config.workloads.network.tcp_window = "16M"
    config.workloads.network.zerocopy = True
    config.workloads.network.bidirectional = True

    script = worker_cloud_init(
        config=config,
        role="network-sender",
        worker_id="network-sender-000",
        controller_url="http://controller:8080",
        duration_seconds=60,
        flow_assignments=[
            {
                "flow_id": "network-flow-00000",
                "receiver_private_ip": "10.0.0.2",
                "receiver_port": 5201,
                "target_gbps": 50,
                "parallel_streams": 32,
            }
        ],
    )

    namespace = worker_namespace(script)
    command = namespace["IPERF_COMMAND_TEMPLATE"]
    assert command[command.index("--omit") + 1] == "10"
    assert command[command.index("--window") + 1] == "16M"
    assert "--zerocopy" in command
    assert "--bidir" in command
    assert namespace["IPERF_BIDIRECTIONAL"] is True


def test_network_worker_cloud_init_omits_unset_iperf_window():
    namespace = storage_namespace()
    assert "--window" not in namespace["IPERF_COMMAND_TEMPLATE"]


def test_network_worker_cloud_init_reports_guest_network_validation():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.workloads.network.vnic_type = "direct"

    script = worker_cloud_init(
        config=config,
        role="network-sender",
        worker_id="network-sender-000",
        controller_url="http://controller:8080",
        duration_seconds=60,
    )

    assert "collect_network_interface_info()" in script
    assert "network_interface_validation" in script
    assert "'driver'" in script
    assert "'speed_mbps'" in script
    assert "'mtu'" in script
    assert "'rx_queues'" in script
    assert "'tx_queues'" in script
    assert "'pci_id'" in script


def queue_namespace(requested=16):
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.workloads.network.guest_combined_queues = requested
    namespace = worker_namespace(
        worker_cloud_init(
            config=config,
            role="network-sender",
            worker_id="network-sender-000",
            controller_url="http://controller:8080",
            duration_seconds=60,
        )
    )
    namespace["first_network_interface"] = lambda: "ens3"
    namespace["interface_driver"] = lambda _: "virtio_net"
    namespace["os"] = SimpleNamespace(cpu_count=lambda: 16)
    namespace["post"] = Mock()
    return namespace


def test_queue_tuning_parses_active_channels_separately_from_sysfs_queues():
    namespace = queue_namespace()
    namespace["subprocess"] = SimpleNamespace(
        check_output=Mock(
            return_value=(
                "Pre-set maximums:\nRX: n/a\nCombined: 16\n"
                "Current hardware settings:\nRX: n/a\nCombined: 1\n"
            )
        )
    )
    assert namespace["combined_channels"]("ens3") == (16, 1)


@pytest.mark.parametrize("maximum,cpus,target", [(16, 16, 16), (8, 16, 8), (32, 4, 4)])
def test_queue_tuning_bounds_channels_and_verifies_readback(maximum, cpus, target):
    namespace = queue_namespace()
    namespace["os"] = SimpleNamespace(cpu_count=lambda: cpus)
    namespace["combined_channels"] = Mock(side_effect=[(maximum, 1), (maximum, target)])
    namespace["subprocess"] = SimpleNamespace(run=Mock())
    result = namespace["tune_network_queues"]()
    namespace["subprocess"].run.assert_called_once_with(
        ["ethtool", "-L", "ens3", "combined", str(target)],
        check=True,
    )
    assert result["after"] == result["target"] == target
    assert namespace["post"].call_args.args[1]["event"] == "network_queue_tuning"


def test_queue_tuning_refuses_failed_readback():
    namespace = queue_namespace()
    namespace["combined_channels"] = Mock(return_value=(16, 1))
    namespace["subprocess"] = SimpleNamespace(run=Mock())
    with pytest.raises(RuntimeError, match="readback mismatch"):
        namespace["tune_network_queues"]()
    namespace["post"].assert_not_called()


def test_queue_tuning_defaults_to_no_mutation_and_rejects_non_virtio():
    namespace = queue_namespace(None)
    namespace["subprocess"] = SimpleNamespace(run=Mock())
    assert namespace["tune_network_queues"]() is None
    namespace["subprocess"].run.assert_not_called()
    namespace["GUEST_COMBINED_QUEUES"] = 16
    namespace["interface_driver"] = lambda _: "mlx5_core"
    with pytest.raises(RuntimeError, match="virtio_net"):
        namespace["tune_network_queues"]()


def test_rx_ring_parser_distinguishes_rx_from_other_ring_types():
    namespace = queue_namespace()
    namespace["subprocess"] = SimpleNamespace(
        check_output=Mock(
            return_value=(
                "Pre-set maximums:\nRX: 1024\nRX Jumbo: 0\nTX: 256\n"
                "Current hardware settings:\nRX: 256\nRX Jumbo: 0\nTX: 256\n"
            )
        )
    )
    assert namespace["rx_ring_size"]("ens3") == (1024, 256)


def test_rx_ring_tuning_sets_rx_only_and_checks_readback():
    namespace = queue_namespace()
    namespace["GUEST_RX_RING_SIZE"] = 1024
    namespace["rx_ring_size"] = Mock(side_effect=[(1024, 256), (1024, 1024)])
    namespace["subprocess"] = SimpleNamespace(run=Mock())
    assert namespace["tune_network_rx_ring"]()["after"] == 1024
    namespace["subprocess"].run.assert_called_once_with(
        ["ethtool", "-G", "ens3", "rx", "1024"],
        check=True,
    )


def test_rx_ring_tuning_refuses_unsupported_size_before_mutation():
    namespace = queue_namespace()
    namespace["GUEST_RX_RING_SIZE"] = 1024
    namespace["rx_ring_size"] = Mock(return_value=(256, 256))
    namespace["subprocess"] = SimpleNamespace(run=Mock())
    with pytest.raises(RuntimeError, match="exceeds advertised maximum"):
        namespace["tune_network_rx_ring"]()
    namespace["subprocess"].run.assert_not_called()


def test_rx_ring_tuning_defaults_unchanged_and_rejects_failed_readback():
    namespace = queue_namespace()
    namespace["subprocess"] = SimpleNamespace(run=Mock())
    assert namespace["tune_network_rx_ring"]() is None
    namespace["subprocess"].run.assert_not_called()
    namespace["GUEST_RX_RING_SIZE"] = 1024
    namespace["rx_ring_size"] = Mock(return_value=(1024, 256))
    with pytest.raises(RuntimeError, match="readback mismatch"):
        namespace["tune_network_rx_ring"]()
    namespace["post"].assert_not_called()


def test_controller_cloud_init_uses_threaded_http_server_without_console_spam():
    script = controller_cloud_init()

    assert "ThreadingHTTPServer" in script
    assert "SOAK_CONTROLLER_DEBUG_CONSOLE" in script
    assert ">/var/log/soak-controller.log" in script


def test_network_bootstrap_uses_preinstalled_diagnostics_without_apt(tmp_path):
    _check_network_diagnostics_bootstrap(tmp_path, preinstalled=True)


def test_network_bootstrap_installs_missing_diagnostics(tmp_path):
    _check_network_diagnostics_bootstrap(tmp_path, preinstalled=False)


def _check_network_diagnostics_bootstrap(tmp_path, *, preinstalled):
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    script = worker_cloud_init(
        config=config,
        role="network-sender",
        worker_id="network-sender-000",
        controller_url="http://controller:8080",
        duration_seconds=60,
    )
    setup = script.split("mkdir -p /var/log/soak-tester\n", 1)[1].split(
        "if [ true = true ]; then\n    sysctl", 1
    )[0]
    assert "sysctl" not in setup and "cat >" not in setup
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for tool in ["ethtool", "lspci", "ip", "mpstat"]:
        if not preinstalled and tool == "mpstat":
            continue
        binary = binaries / tool
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
    apt = binaries / "apt-get"
    apt.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_APT_LOG"\n')
    apt.chmod(0o755)
    log = tmp_path / "apt.log"
    subprocess.run(
        ["/bin/bash", "-c", setup],
        env={**os.environ, "PATH": str(binaries), "TEST_APT_LOG": str(log)},
        check=True,
    )
    if preinstalled:
        assert not log.exists()
    else:
        assert log.read_text().splitlines() == [
            "update",
            "install -y ethtool pciutils iproute2 sysstat",
        ]


def test_iperf_failed_attempts_are_recorded_once_per_output_file(tmp_path):
    cfg = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    script = worker_cloud_init(
        config=cfg,
        role="network-sender",
        worker_id="network-sender-000",
        controller_url="http://controller:8080",
        duration_seconds=60,
        flow_assignments=[{"flow_id": "network-flow-00000"}],
    )
    namespace = worker_namespace(script)
    names = [f"iperf3-network-flow-00000-attempt-{attempt:05d}.json" for attempt in range(2)]
    namespace["PARSED_IPERF_FILES"] = set()
    for name in names:
        (tmp_path / name).write_text(json.dumps({"error": "Connection refused"}))
    namespace["os"] = SimpleNamespace(listdir=lambda _: names, path=os.path)
    namespace["open"] = lambda filename: (tmp_path / os.path.basename(filename)).open()
    metrics = []
    namespace["post_many"] = lambda endpoint, rows: (
        metrics.extend(rows) if endpoint == "/metrics" else None
    )
    namespace["post"] = lambda *args: None
    namespace["parse_iperf3_output"]()
    namespace["parse_iperf3_output"]()
    failures = [row for row in metrics if row["metric"] == "iperf.flow_failed"]
    assert len(failures) == 2
    assert {row["tags"]["flow_file"] for row in failures} == set(names)
    assert all(row["value"] == 1 for row in failures)


def test_guest_uses_shared_parser_and_converts_minutes():
    from soak_tester.workloads.results import parse_iperf3_metrics

    cfg = load_config(Path(__file__).parent / "fixtures/config.yaml")
    cfg.workloads.network.flow_duration = "2m"
    ns = worker_namespace(
        worker_cloud_init(
            config=cfg,
            role="network-sender",
            worker_id="s",
            controller_url="http://controller:8080",
            duration_seconds=200,
        )
    )
    assert ns["FLOW_TIME"] == 120
    payload = json.dumps(
        {"end": {"sum_received": {"bits_per_second": 100}, "sum_sent": {"bits_per_second": 110}}}
    )
    assert ns["parse_iperf3_metrics"](payload, worker_id="s", receiver="r") == parse_iperf3_metrics(
        payload, worker_id="s", receiver="r"
    )


def test_guest_does_not_parse_an_active_iperf_attempt(tmp_path):
    ns = storage_namespace()
    name = "iperf3-network-flow-00000-attempt-00000.json"
    path = "/var/log/soak-tester/" + name
    ns["PARSED_IPERF_FILES"] = set()
    ns["IPERF_PROCESSES"] = {path: SimpleNamespace(poll=lambda: None)}
    ns["os"] = SimpleNamespace(listdir=lambda _: [name])
    ns["open"] = Mock(side_effect=AssertionError("active output must not be read"))
    ns["post"] = Mock()
    ns["parse_iperf3_output"]()
    ns["post"].assert_not_called()


def test_compute_guest_refuses_fallback_when_stress_ng_is_missing():
    ns = storage_namespace()
    ns["ROLE"] = "compute"
    ns["has_command"] = lambda _: False
    with pytest.raises(RuntimeError, match="stress-ng is not installed"):
        ns["start_workload"]()


def test_generated_guest_command_uses_each_planned_flow_rate_and_port():
    ns = storage_namespace()
    flow = {
        "receiver_private_ip": "10.0.0.2",
        "receiver_port": 5300,
        "parallel_streams": 32,
        "target_gbps": 50,
    }
    command = ns["iperf_command"](flow, 120)
    assert command[command.index("--parallel") + 1] == "32"
    assert command[command.index("--bitrate") + 1] == "1.5625G"
    assert command[command.index("--port") + 1] == "5300"
    assert command[command.index("--time") + 1] == "120"
    ns["OFFERED_LOAD"] = "unlimited"
    command = ns["iperf_command"](flow, 120)
    assert command[command.index("--bitrate") + 1] == "0G"


def test_fixed_worker_budget_idle_senders_start_without_inventing_traffic():
    from soak_tester.planner import FlavorSpec, create_plan

    cfg = load_config(Path(__file__).parents[1] / "configs/network-virtio.yaml")
    cfg.targets.network.scope = "per_host"
    cfg.workloads.network.topology = "full_mesh"
    cfg.workloads.network.hosts = ["h1", "h2"]
    cfg.targets.network.gbps = 10
    cfg.workloads.network.target_gbps_per_flow = 10
    cfg.workloads.network.sender_worker_target_gbps = 5
    flavors = {
        name: FlavorSpec(name=name, vcpus=4, ram_gib=8)
        for name in cfg.flavors.model_dump().values()
        if name
    }
    plan = create_plan(cfg, target_percent=100, flavor_specs=flavors)
    idle_senders = [
        worker_id
        for worker_id in plan.network.sender_worker_hosts
        if not any(flow.sender_worker_id == worker_id for flow in plan.network.flow_assignments)
    ]
    assert idle_senders
    for worker_id in idle_senders:
        ns = worker_namespace(
            worker_cloud_init(
                config=cfg,
                role="network-sender",
                worker_id=worker_id,
                controller_url="http://controller:8080",
                duration_seconds=60,
                flow_assignments=[],
            )
        )
        ns["subprocess"] = Mock()
        assert ns["start_workload"]() is None
        assert ns["FLOW_ASSIGNMENTS"] == []
        ns["subprocess"].Popen.assert_not_called()


def test_network_worker_cloud_init_emits_python_none_for_optional_iperf_window():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.workloads.network.tcp_window = None

    script = worker_cloud_init(
        config=config,
        role="network-sender",
        worker_id="network-sender-000",
        controller_url="http://controller:8080",
        duration_seconds=60,
    )

    namespace = worker_namespace(script)
    flow = {
        "receiver_private_ip": "10.0.0.2",
        "receiver_port": 5201,
        "parallel_streams": 1,
        "target_gbps": 1,
    }
    assert "--window" not in namespace["iperf_command"](flow, 60)


def test_interrupt_coalescing_parser_reads_adaptive_and_exact_usec_fields():
    namespace = queue_namespace(None)
    namespace["subprocess"] = SimpleNamespace(
        check_output=Mock(
            return_value=(
                "Coalesce parameters for ens3:\nAdaptive RX: on  TX: off\n"
                "rx-usecs: 8\nrx-usecs-irq: n/a\nrx-frames: 128\ntx-usecs: 16\n"
            )
        )
    )
    assert namespace["interrupt_coalescing"]("ens3") == {
        "adaptive_rx": "on",
        "adaptive_tx": "off",
        "rx-usecs": 8,
        "tx-usecs": 16,
    }


@pytest.mark.parametrize(
    "output",
    [
        "Adaptive RX: on TX: on\nrx-usecs: n/a\ntx-usecs: 8\n",
        "rx-usecs: 8\ntx-usecs: 8\n",
    ],
)
def test_interrupt_coalescing_rejects_unsupported_readback_before_mutation(output):
    namespace = queue_namespace(None)
    namespace["GUEST_INTERRUPT_COALESCE_USECS"] = 50
    namespace["subprocess"] = SimpleNamespace(check_output=Mock(return_value=output), run=Mock())
    with pytest.raises(RuntimeError, match="reported"):
        namespace["tune_network_interrupt_coalescing"]()
    namespace["subprocess"].run.assert_not_called()


def test_interrupt_coalescing_sets_fixed_delays_and_verifies_readback():
    namespace = queue_namespace(None)
    namespace["GUEST_INTERRUPT_COALESCE_USECS"] = 50
    before = {"adaptive_rx": "on", "adaptive_tx": "on", "rx-usecs": 8, "tx-usecs": 8}
    target = {"adaptive_rx": "off", "adaptive_tx": "off", "rx-usecs": 50, "tx-usecs": 50}
    namespace["interrupt_coalescing"] = Mock(side_effect=[before, target])
    namespace["subprocess"] = SimpleNamespace(run=Mock())
    assert namespace["tune_network_interrupt_coalescing"]()["after"] == target
    namespace["subprocess"].run.assert_called_once_with(
        [
            "ethtool",
            "-C",
            "ens3",
            "adaptive-rx",
            "off",
            "adaptive-tx",
            "off",
            "rx-usecs",
            "50",
            "tx-usecs",
            "50",
        ],
        check=True,
    )


def test_interrupt_coalescing_defaults_unchanged_and_refuses_failed_readback():
    namespace = queue_namespace(None)
    namespace["subprocess"] = SimpleNamespace(run=Mock())
    assert namespace["tune_network_interrupt_coalescing"]() is None
    namespace["subprocess"].run.assert_not_called()
    namespace["GUEST_INTERRUPT_COALESCE_USECS"] = 50
    namespace["interrupt_coalescing"] = Mock(
        return_value={
            "adaptive_rx": "on",
            "adaptive_tx": "on",
            "rx-usecs": 8,
            "tx-usecs": 8,
        }
    )
    with pytest.raises(RuntimeError, match="readback mismatch"):
        namespace["tune_network_interrupt_coalescing"]()
    namespace["post"].assert_not_called()


def test_interrupt_coalescing_keeps_matching_settings_and_rejects_missing_interface():
    namespace = queue_namespace(None)
    namespace["GUEST_INTERRUPT_COALESCE_USECS"] = 0
    target = {"adaptive_rx": "off", "adaptive_tx": "off", "rx-usecs": 0, "tx-usecs": 0}
    namespace["interrupt_coalescing"] = Mock(return_value=target)
    namespace["subprocess"] = SimpleNamespace(run=Mock())
    assert namespace["tune_network_interrupt_coalescing"]()["after"] == target
    namespace["subprocess"].run.assert_not_called()
    namespace["first_network_interface"] = lambda: None
    namespace["interrupt_coalescing"].reset_mock()
    with pytest.raises(RuntimeError, match="active network interface"):
        namespace["tune_network_interrupt_coalescing"]()
    namespace["interrupt_coalescing"].assert_not_called()


def test_interrupt_coalescing_propagates_cli_failure_without_reporting_success():
    namespace = queue_namespace(None)
    namespace["GUEST_INTERRUPT_COALESCE_USECS"] = 50
    namespace["interrupt_coalescing"] = Mock(
        return_value={
            "adaptive_rx": "on",
            "adaptive_tx": "on",
            "rx-usecs": 8,
            "tx-usecs": 8,
        }
    )
    namespace["subprocess"] = SimpleNamespace(
        run=Mock(side_effect=subprocess.CalledProcessError(1, ["ethtool", "-C"]))
    )
    with pytest.raises(subprocess.CalledProcessError):
        namespace["tune_network_interrupt_coalescing"]()
    namespace["post"].assert_not_called()


@pytest.mark.parametrize("received_bps", [0, 7_000_000_000])
def test_udp_uses_receiver_rate_and_reports_loss(tmp_path, received_bps):
    cfg = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    cfg.workloads.network.protocol = "udp"
    cfg.workloads.network.udp_datagram_size = 8192
    script = worker_cloud_init(
        config=cfg,
        role="network-sender",
        worker_id="sender",
        controller_url="http://controller:8080",
        duration_seconds=60,
    )
    namespace = worker_namespace(script)
    assert namespace["IPERF_PROTOCOL"] == "udp"
    assert namespace["IPERF_UDP_DATAGRAM_SIZE"] == 8192
    name = "iperf3-flow-attempt-00000.json"
    received = dict(
        bits_per_second=received_bps,
        sender=False,
        lost_packets=30,
        packets=100,
        lost_percent=30,
        jitter_ms=0.2,
    )
    payload = {"end": {"sum_sent": {"bits_per_second": 10_000_000_000}, "sum_received": received}}
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    namespace["PARSED_IPERF_FILES"] = set()
    namespace["os"] = SimpleNamespace(listdir=lambda _: [name], path=os.path)
    namespace["open"] = lambda _: path.open()
    rows, events = [], []
    namespace["post_many"] = lambda endpoint, values: (
        rows if endpoint == "/metrics" else events
    ).extend(values)
    namespace["post"] = lambda endpoint, value: events.append(value)
    namespace["parse_iperf3_output"]()
    values = {row["metric"]: row["value"] for row in rows}
    assert values["iperf.goodput_bps"] == received_bps
    assert values["iperf.udp_lost_packets"] == 30
    assert values["iperf.udp_lost_percent"] == 30
    assert all(row["tags"]["protocol"] == "udp" for row in rows)
    # Sender-only or incomplete UDP results must never become goodput.
    for invalid in [
        {},
        {"bits_per_second": 10_000_000_000, "sender": True},
        {"bits_per_second": 0, "sender": False},
    ]:
        payload["end"]["sum_received"] = invalid
        path.write_text(json.dumps(payload))
        namespace["PARSED_IPERF_FILES"] = set()
        rows.clear()
        namespace["parse_iperf3_output"]()
        assert not rows
        assert any(event.get("event") == "iperf_parse_failed" for event in events)


@pytest.mark.parametrize(
    "output, expected",
    [
        ("large-receive-offload: off\n", (False, False)),
        ("large-receive-offload: on\n", (True, False)),
        ("large-receive-offload: off [fixed]\n", (False, True)),
    ],
)
def test_large_receive_offload_parses_native_readback(output, expected):
    ns = queue_namespace(None)
    ns["subprocess"] = SimpleNamespace(check_output=Mock(return_value=output))
    assert ns["large_receive_offload"]("ens3") == expected


def lro_namespace():
    ns = queue_namespace(None)
    ns.update(
        ROLE="network-receiver",
        GUEST_LARGE_RECEIVE_OFFLOAD=True,
        has_command=lambda _: True,
        read_text=Mock(return_value="0"),
    )
    ns["subprocess"] = SimpleNamespace(run=Mock())
    return ns


@pytest.mark.parametrize("requested", [True, False])
def test_large_receive_offload_changes_receiver_only_and_verifies(requested):
    ns = lro_namespace()
    ns["GUEST_LARGE_RECEIVE_OFFLOAD"] = requested
    ns["large_receive_offload"] = Mock(side_effect=[(not requested, False), (requested, False)])
    result = ns["tune_network_large_receive_offload"]()
    assert result["after"] == requested
    assert result["forwarding_verified_disabled"]
    ns["subprocess"].run.assert_called_once_with(
        ["ethtool", "-K", "ens3", "lro", "on" if requested else "off"], check=True, timeout=10
    )


@pytest.mark.parametrize("forwarding", ["1", None])
def test_large_receive_offload_refuses_unverified_forwarding_before_mutation(forwarding):
    ns = lro_namespace()
    ns["read_text"] = Mock(side_effect=["0", "0", "0", forwarding])
    ns["large_receive_offload"] = Mock()
    with pytest.raises(RuntimeError, match="non-forwarding"):
        ns["tune_network_large_receive_offload"]()
    ns["large_receive_offload"].assert_not_called()
    ns["subprocess"].run.assert_not_called()


def test_large_receive_offload_refuses_fixed_or_mismatched_states():
    ns = lro_namespace()
    ns["large_receive_offload"] = Mock(return_value=(False, True))
    with pytest.raises(RuntimeError, match="fixed by the driver"):
        ns["tune_network_large_receive_offload"]()
    ns["subprocess"].run.assert_not_called()
    ns["large_receive_offload"] = Mock(return_value=(False, False))
    with pytest.raises(RuntimeError, match="readback mismatch"):
        ns["tune_network_large_receive_offload"]()
    ns["post"].assert_not_called()


def test_large_receive_offload_keeps_defaults_senders_and_matching_state():
    ns = lro_namespace()
    ns["GUEST_LARGE_RECEIVE_OFFLOAD"] = None
    assert ns["tune_network_large_receive_offload"]() is None
    ns["GUEST_LARGE_RECEIVE_OFFLOAD"] = True
    ns["ROLE"] = "network-sender"
    assert ns["tune_network_large_receive_offload"]() is None
    ns["subprocess"].run.assert_not_called()
    ns["read_text"].assert_not_called()
    ns["ROLE"] = "network-receiver"
    ns["large_receive_offload"] = Mock(return_value=(True, True))
    assert ns["tune_network_large_receive_offload"]()["after"] is True
    ns["subprocess"].run.assert_not_called()


@pytest.mark.parametrize("capture", [False, True])
def test_tcp_receiver_json_is_opt_in(tmp_path, capture):
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.workloads.network.capture_receiver_intervals = capture
    script = worker_cloud_init(
        config=config,
        role="network-receiver",
        worker_id="receiver",
        controller_url="http://controller:8080",
        duration_seconds=60,
        receiver_ports=[5201],
    )
    namespace = worker_namespace(script)
    namespace["has_command"] = lambda _: True
    namespace["open"] = lambda *args: (tmp_path / "server.log").open("a")
    popen = Mock()
    namespace["subprocess"] = SimpleNamespace(Popen=popen, STDOUT=subprocess.STDOUT)
    namespace["SERVER_PROCS"] = []
    namespace["start_workload"]()
    argv = popen.call_args.args[0]
    assert ("--json" in argv) is capture
    assert namespace["IPERF_CAPTURE_RECEIVER_INTERVALS"] is capture
    flow = {
        "receiver_private_ip": "10.0.0.2",
        "receiver_port": 5201,
        "parallel_streams": 1,
        "target_gbps": 1,
    }
    assert ("--get-server-output" in namespace["iperf_command"](flow, 60)) is capture


def mq_layout(kind="fq_codel", handle="0:", queues=4):
    major = handle[:-1] if handle != "0:" else ""
    return [
        {"kind": "mq", "handle": handle, "root": True},
        *[{"kind": kind, "handle": "0:", "parent": f"{major}:{n}"} for n in range(1, queues + 1)],
        {"kind": "clsact", "handle": "ffff:", "parent": "ffff:fff1"},
    ]


def tx_qdisc_namespace(requested="mq_fq"):
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.workloads.network.guest_tx_qdisc = requested
    script = worker_cloud_init(
        config=config,
        role="network-sender",
        worker_id="sender-000",
        controller_url="http://controller:8080",
        duration_seconds=60,
    )
    ns = worker_namespace(script)
    ns["first_network_interface"] = Mock(return_value="ens3")
    ns["has_command"] = Mock(return_value=True)
    ns["count_queues"] = Mock(return_value=4)
    ns["post"] = Mock()
    ns["subprocess"] = SimpleNamespace(check_output=Mock(return_value="[]"), run=Mock())
    return ns


def test_tx_qdisc_default_does_not_inspect_or_mutate_interfaces():
    ns = tx_qdisc_namespace(None)
    assert ns["tune_network_tx_qdisc"]() is None
    ns["first_network_interface"].assert_not_called()
    ns["subprocess"].run.assert_not_called()


def test_tx_qdisc_native_reader_and_bootstrap_placement():
    ns = tx_qdisc_namespace()
    ns["subprocess"].check_output.return_value = json.dumps(mq_layout())
    assert ns["tx_qdiscs"]("ens3") == mq_layout()
    ns["subprocess"].check_output.assert_called_once_with(
        ["tc", "-j", "qdisc", "show", "dev", "ens3"], text=True, timeout=10
    )
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    script = worker_cloud_init(
        config=config,
        role="network-sender",
        worker_id="sender-000",
        controller_url="http://controller:8080",
        duration_seconds=60,
    )
    startup = script[script.index("post('/events', event('info', ROLE, 'software_versions'") :]
    assert startup.index("tune_network_tx_qdisc()") < startup.index("control = wait_for_start()")
    assert startup.index("tune_network_tx_qdisc()") < startup.index("proc = start_workload()")


def test_tx_qdisc_rebuilds_unnamed_mq_before_grafting_and_preserves_clsact():
    ns = tx_qdisc_namespace()
    ns["tx_qdiscs"] = Mock(side_effect=[mq_layout(), mq_layout(handle="1:"), mq_layout("fq", "1:")])
    result = ns["tune_network_tx_qdisc"]()
    assert (result["root_handle_before"], result["root_handle_after"], result["tx_queues"]) == (
        "0:",
        "1:",
        4,
    )
    assert [c.args[0] for c in ns["subprocess"].run.call_args_list] == [
        ["tc", "qdisc", "replace", "dev", "ens3", "root", "handle", "1:", "mq"],
        *[["tc", "qdisc", "replace", "dev", "ens3", "parent", f"1:{n}", "fq"] for n in range(1, 5)],
    ]
    assert ns["post"].call_args.args[1]["event"] == "network_tx_qdisc_tuning"


@pytest.mark.parametrize("handle", ["0:", "2:"])
def test_tx_qdisc_keeps_already_configured_multiqueue_without_mutation(handle):
    ns = tx_qdisc_namespace()
    ns["tx_qdiscs"] = Mock(return_value=mq_layout("fq", handle))
    assert ns["tune_network_tx_qdisc"]()["root_handle_after"] == handle
    ns["subprocess"].run.assert_not_called()


def test_tx_qdisc_grafts_named_mq_without_replacing_root():
    ns = tx_qdisc_namespace()
    ns["tx_qdiscs"] = Mock(side_effect=[mq_layout(handle="2:"), mq_layout("fq", "2:")])
    ns["tune_network_tx_qdisc"]()
    assert [c.args[0] for c in ns["subprocess"].run.call_args_list] == [
        ["tc", "qdisc", "replace", "dev", "ens3", "parent", f"2:{n}", "fq"] for n in range(1, 5)
    ]


@pytest.mark.parametrize(
    "layout",
    [
        [{"kind": "htb", "handle": "1:", "root": True}],
        mq_layout("htb"),
        mq_layout()[:-2],
        [mq_layout()[0], *[mq_layout()[1]] * 4],
        [mq_layout()[0], {"kind": "fq_codel", "handle": "1:", "parent": ":1"}, *mq_layout()[2:]],
        [*mq_layout(), {"kind": "ingress", "handle": "eeee:", "parent": "ffff:fff1"}],
    ],
)
def test_tx_qdisc_refuses_unknown_custom_or_incomplete_layout_before_mutation(layout):
    ns = tx_qdisc_namespace()
    ns["tx_qdiscs"] = Mock(return_value=layout)
    with pytest.raises(RuntimeError):
        ns["tune_network_tx_qdisc"]()
    ns["subprocess"].run.assert_not_called()
    ns["post"].assert_not_called()


@pytest.mark.parametrize("policy_index", [0, 1, 2])
def test_tx_qdisc_refuses_existing_root_or_clsact_filter_policy(policy_index):
    ns = tx_qdisc_namespace()
    ns["tx_qdiscs"] = Mock(return_value=mq_layout())
    ns["subprocess"].check_output.side_effect = ["[]"] * policy_index + ['[{"kind":"bpf"}]']
    with pytest.raises(RuntimeError, match="filter policy"):
        ns["tune_network_tx_qdisc"]()
    ns["subprocess"].run.assert_not_called()


@pytest.mark.parametrize("after", [mq_layout(), mq_layout("fq", "1:")[:-2], mq_layout("fq", "2:")])
def test_tx_qdisc_refuses_failed_readback_without_success_event(after):
    ns = tx_qdisc_namespace()
    ns["tx_qdiscs"] = Mock(side_effect=[mq_layout(), mq_layout(handle="1:"), after])
    with pytest.raises(RuntimeError):
        ns["tune_network_tx_qdisc"]()
    ns["post"].assert_not_called()


def test_tx_qdisc_stops_if_queue_count_changes_during_rebuild():
    ns = tx_qdisc_namespace()
    ns["count_queues"].side_effect = [4, 8]
    ns["tx_qdiscs"] = Mock(side_effect=[mq_layout(), mq_layout(handle="1:", queues=8)])
    with pytest.raises(RuntimeError, match="readback"):
        ns["tune_network_tx_qdisc"]()
    assert ns["subprocess"].run.call_count == 1
    ns["post"].assert_not_called()


def test_tx_qdisc_propagates_cli_failure_without_success_event():
    ns = tx_qdisc_namespace()
    ns["tx_qdiscs"] = Mock(return_value=mq_layout())
    ns["subprocess"].run.side_effect = subprocess.CalledProcessError(2, "tc")
    with pytest.raises(subprocess.CalledProcessError):
        ns["tune_network_tx_qdisc"]()
    ns["post"].assert_not_called()
