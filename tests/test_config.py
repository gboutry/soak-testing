from pathlib import Path

import pytest

from soak_tester.config import load_config
from soak_tester.util import parse_duration


FIXTURE = Path(__file__).parent / "fixtures" / "config.yaml"


def test_load_config_accepts_example_schema():
    config = load_config(FIXTURE)

    assert config.openstack.external_network == "ext-net"
    assert config.resources.network_cidr == "10.250.0.0/24"
    assert config.workloads.storage.volume_type == "ceph-ssd"
    assert config.observability.prometheus.query_packs == ["node-exporter-network"]


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("5m", 300), ("8h", 28800), ("1d", 86400)],
)
def test_parse_duration_supported_units(text, seconds):
    assert parse_duration(text).total_seconds() == seconds


def test_parse_duration_rejects_invalid_value():
    with pytest.raises(ValueError):
        parse_duration("forever")
