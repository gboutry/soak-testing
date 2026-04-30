from pathlib import Path

from soak_tester.config import load_config
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
