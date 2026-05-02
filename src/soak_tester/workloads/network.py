from __future__ import annotations

from soak_tester.config import SoakConfig
from soak_tester.util import duration_seconds


def iperf_client_command(config: SoakConfig, receiver_ip: str) -> list[str]:
    network = config.workloads.network
    command = [
        "iperf3",
        "--client",
        receiver_ip,
        "--time",
        str(duration_seconds(network.flow_duration)),
        "--parallel",
        str(network.parallel_streams_per_flow),
        "--bitrate",
        f"{network.target_gbps_per_flow}G",
        "--json",
    ]
    if network.omit_seconds:
        command.extend(["--omit", str(network.omit_seconds)])
    if network.tcp_window:
        command.extend(["--window", network.tcp_window])
    if network.zerocopy:
        command.append("--zerocopy")
    if network.reverse:
        command.append("--reverse")
    if network.bidirectional:
        command.append("--bidir")
    command.extend(network.extra_client_args)
    return command
