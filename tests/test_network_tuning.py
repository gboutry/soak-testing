from pathlib import Path

from soak_tester.config import load_config
from soak_tester.workloads.network import iperf_client_command


def test_iperf_client_command_includes_tuning_options():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.workloads.network.omit_seconds = 10
    config.workloads.network.tcp_window = "16M"
    config.workloads.network.zerocopy = True
    config.workloads.network.bidirectional = True
    config.workloads.network.connect_timeout_ms = 5000

    command = iperf_client_command(config, "10.0.0.2")

    assert command == [
        "iperf3",
        "--client",
        "10.0.0.2",
        "--port",
        "5201",
        "--time",
        "60",
        "--parallel",
        "4",
        "--bitrate",
        "2.5G",
        "--json",
        "--omit",
        "10",
        "--window",
        "16M",
        "--zerocopy",
        "--bidir",
        "--connect-timeout",
        "5000",
    ]


def test_iperf_udp_command_has_explicit_datagram_and_counter_options():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.workloads.network.protocol = "udp"
    config.workloads.network.udp_datagram_size = 8192
    command = iperf_client_command(config, "10.0.0.2")
    assert "--udp" in command
    assert "--udp-counters-64bit" in command
    assert command[command.index("--length") + 1] == "8192"


def test_tcp_receiver_intervals_are_opt_in():
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    assert not config.workloads.network.capture_receiver_intervals
    assert "--get-server-output" not in iperf_client_command(config, "10.0.0.2")
    config.workloads.network.capture_receiver_intervals = True
    command = iperf_client_command(config, "10.0.0.2")
    assert command.count("--get-server-output") == 1
    assert "--udp" not in command
