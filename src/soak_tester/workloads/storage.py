from __future__ import annotations

from soak_tester.config import SoakConfig


def fio_command(config: SoakConfig, device: str, duration_seconds: int) -> list[str]:
    workload = config.workloads.storage
    return [
        "fio",
        "--name=workload",
        "--ioengine=libaio",
        "--direct=1",
        "--time_based=1",
        "--runtime",
        str(duration_seconds),
        "--group_reporting=1",
        "--iodepth",
        str(workload.iodepth),
        "--numjobs",
        str(workload.numjobs),
        "--bs",
        workload.block_size,
        "--rw=randrw",
        "--rwmixread",
        str(workload.rwmixread),
        "--filename",
        device,
        "--output-format=json",
    ]
