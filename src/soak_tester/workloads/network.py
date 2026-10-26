from __future__ import annotations

from soak_tester.config import SoakConfig
from soak_tester.util import duration_seconds


def iperf_client_command(
    config: SoakConfig,
    receiver_ip: str,
    *,
    receiver_port: int | str = 5201,
    seconds: int | str | None = None,
    streams: int | str | None = None,
    bitrate: str | None = None,
) -> list[str]:
    network = config.workloads.network
    command = [
        "iperf3",
        "--client",
        receiver_ip,
        "--port",
        str(receiver_port),
        "--time",
        str(seconds if seconds is not None else duration_seconds(network.flow_duration)),
        "--parallel",
        str(streams if streams is not None else network.parallel_streams_per_flow),
        "--bitrate",
        bitrate
        if bitrate is not None
        else f"{network.target_gbps_per_flow / network.parallel_streams_per_flow if network.offered_load == 'target' else 0}G",
        "--json",
    ]
    if network.protocol == "udp":
        command.extend(["--udp", "--udp-counters-64bit", "--get-server-output"])
        if network.udp_datagram_size is not None:
            command.extend(["--length", str(network.udp_datagram_size)])
    elif network.capture_receiver_intervals:
        command.append("--get-server-output")
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
    if network.connect_timeout_ms is not None:
        command.extend(["--connect-timeout", str(network.connect_timeout_ms)])
    if network.pacing_timer_us is not None:
        command.extend(["--pacing-timer", str(network.pacing_timer_us)])
    return command
