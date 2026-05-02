from __future__ import annotations

from soak_tester.config import SoakConfig


def stress_ng_command(config: SoakConfig, duration_seconds: int) -> list[str]:
    return [
        "stress-ng",
        "--cpu",
        "0",
        "--cpu-load",
        str(config.workloads.compute.cpu_load_percent),
        "--vm",
        "1",
        "--vm-bytes",
        f"{config.workloads.compute.memory_load_percent}%",
        "--hdd",
        "1",
        "--hdd-bytes",
        "10G",
        "--hdd-opts",
        "direct,sync",
        "--iostat",
        "1",
        "--timeout",
        str(duration_seconds),
    ]
