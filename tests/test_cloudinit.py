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


def test_controller_cloud_init_uses_threaded_http_server_without_console_spam():
    script = controller_cloud_init()

    assert "ThreadingHTTPServer" in script
    assert "SOAK_CONTROLLER_DEBUG_CONSOLE" in script
    assert ">/var/log/soak-controller.log" in script
