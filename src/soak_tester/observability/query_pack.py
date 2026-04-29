from __future__ import annotations

BUILTIN_QUERY_PACKS: dict[str, dict[str, str]] = {
    "node-exporter-network": {
        "compute_host_network_rx_bps": """
sum by (instance) (
  rate(node_network_receive_bytes_total{
    instance=~"$compute_hosts",
    device=~"$network_devices",
    device!~"$excluded_network_devices"
  }[5m])
) * 8
""".strip(),
        "compute_host_network_tx_bps": """
sum by (instance) (
  rate(node_network_transmit_bytes_total{
    instance=~"$compute_hosts",
    device=~"$network_devices",
    device!~"$excluded_network_devices"
  }[5m])
) * 8
""".strip(),
    }
}


def render_builtin_pack(
    name: str,
    *,
    compute_hosts: str,
    network_devices: str,
    excluded_network_devices: str,
) -> dict[str, str]:
    if name not in BUILTIN_QUERY_PACKS:
        raise ValueError(f"unknown built-in query pack: {name}")
    replacements = {
        "$compute_hosts": compute_hosts,
        "$network_devices": network_devices,
        "$excluded_network_devices": excluded_network_devices,
    }
    rendered: dict[str, str] = {}
    for query_name, template in BUILTIN_QUERY_PACKS[name].items():
        query = template
        for key, value in replacements.items():
            query = query.replace(key, value)
        rendered[query_name] = query
    return rendered
