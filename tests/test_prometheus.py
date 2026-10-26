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


def test_host_interface_summary_sums_hosts_per_timestamp_without_rx_tx_double_count():
    from soak_tester.observability.collect import summarize_prometheus_results

    result = summarize_prometheus_results(
        {
            "compute_host_network_rx_bps": {
                "data": {
                    "result": [
                        {"metric": {"instance": "n1"}, "values": [[1, "10"], [2, "20"]]},
                        {"metric": {"instance": "n2"}, "values": [[1, "30"], [2, "40"]]},
                    ]
                }
            },
            "compute_host_network_tx_bps": {
                "data": {"result": [{"values": [[1, "40"], [2, "60"]]}]}
            },
        }
    )
    assert result["compute_host_network_rx_bps"]["avg"] == 50
    assert result["compute_host_network_tx_bps"]["avg"] == 50
    assert "compute_host_network_rx_tx_avg_gbps" not in result


def test_query_pack_escapes_regex_for_promql_string_literals():
    queries = render_builtin_pack(
        "node-exporter-network",
        compute_hosts=r"host\.example",
        network_devices='ens1"x',
        excluded_network_devices="lo",
    )
    assert 'instance=~"host\\\\.example"' in queries["compute_host_network_rx_bps"]
    assert 'device=~"ens1\\"x"' in queries["compute_host_network_rx_bps"]
