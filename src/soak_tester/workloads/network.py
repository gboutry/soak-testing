from __future__ import annotations

from soak_tester.config import SoakConfig
from soak_tester.util import duration_seconds


def iperf_client_command(config: SoakConfig, receiver_ip: str) -> list[str]:
    return [
        "iperf3",
        "--client",
        receiver_ip,
        "--time",
        str(duration_seconds(config.workloads.network.flow_duration)),
        "--parallel",
        str(config.workloads.network.parallel_streams_per_flow),
        "--bitrate",
        f"{config.workloads.network.target_gbps_per_flow}G",
        "--json",
    ]
