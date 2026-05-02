from pathlib import Path

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
    assert "'--runtime', str(WORKLOAD_DURATION)" in script


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

    assert "IPERF_OMIT_SECONDS = 10" in script
    assert "IPERF_TCP_WINDOW = '16M'" in script
    assert "IPERF_ZEROCOPY = True" in script
    assert "IPERF_BIDIRECTIONAL = True" in script
    assert "command.extend(['--omit', str(IPERF_OMIT_SECONDS)])" in script
    assert "command.append('--zerocopy')" in script
    assert "command.append('--bidir')" in script


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

    assert "IPERF_TCP_WINDOW = None" in script
    assert "IPERF_TCP_WINDOW = null" not in script


def test_controller_cloud_init_uses_threaded_http_server_without_console_spam():
    script = controller_cloud_init()

    assert "ThreadingHTTPServer" in script
    assert "SOAK_CONTROLLER_DEBUG_CONSOLE" in script
    assert ">/var/log/soak-controller.log" in script


def test_controller_cloud_init_keeps_large_metric_window_for_full_soak_runs():
    script = controller_cloud_init()

    assert "SOAK_CONTROLLER_METRICS_RESPONSE_LIMIT" in script
    assert "'100000'" in script
    assert "[-METRICS_RESPONSE_LIMIT:]" in script
