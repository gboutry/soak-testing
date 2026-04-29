from soak_tester.observability.query_pack import render_builtin_pack
from soak_tester.observability.summarize import summarize_series


def test_render_builtin_node_exporter_network_pack():
    queries = render_builtin_pack(
        "node-exporter-network",
        compute_hosts="host-a|host-b",
        network_devices="eth.*",
        excluded_network_devices="lo",
    )

    assert "compute_host_network_rx_bps" in queries
    assert 'instance=~"host-a|host-b"' in queries["compute_host_network_rx_bps"]
    assert 'device=~"eth.*"' in queries["compute_host_network_tx_bps"]
    assert 'device!~"lo"' in queries["compute_host_network_tx_bps"]


def test_summarize_series_calculates_percentiles_and_gbps():
    summary = summarize_series([1_000_000_000, 2_000_000_000, 3_000_000_000])

    assert summary["sample_count"] == 3
    assert summary["avg"] == 2_000_000_000
    assert summary["min"] == 1_000_000_000
    assert summary["max"] == 3_000_000_000
    assert summary["p50"] == 2_000_000_000
    assert summary["avg_gbps"] == 2
