import json
from pathlib import Path
import re

import pytest
import yaml


DASHBOARD = Path(__file__).parents[1] / "openstack-soak-showcase.json"
PHYSICAL_PANELS = {36, 37, 40, 41}


@pytest.fixture
def physical_queries():
    dashboard = json.loads(DASHBOARD.read_text())
    panels = [p for p in dashboard["panels"] if p["id"] in PHYSICAL_PANELS]
    assert {p["id"] for p in panels} == PHYSICAL_PANELS
    return [target["expr"] for panel in panels for target in panel["targets"]]


def matches_device(query, device):
    selectors = re.findall(r'device(=~|!~)"([^"]*)"', query)
    assert selectors, "Physical NIC queries must select interfaces explicitly"
    return all(
        bool(re.fullmatch(pattern, device)) == (operator == "=~") for operator, pattern in selectors
    )


@pytest.mark.parametrize(
    "device,expected",
    [
        ("enp65s0f0np0", True),
        ("enp65s0f1np1", True),
        ("eno8403", True),
        ("ens3", True),
        ("eth0", True),
        ("enp65s0f0r0", False),
        ("enp65s0f1r15", False),
        ("enp65s0f0v0", False),
        ("enp65s0f1v15", False),
        ("enp65s0f0np0.3606", False),
        ("bond0", False),
        ("bond0.3606", False),
        ("br-int", False),
        ("genev_sys_6081", False),
        ("tap123", False),
        ("veth123", False),
        ("lo", False),
    ],
)
def test_physical_panels_select_ports_without_virtual_interfaces(
    physical_queries, device, expected
):
    assert all(matches_device(query, device) == expected for query in physical_queries)


def test_physical_panels_do_not_add_representor_traffic_to_port_traffic(physical_queries):
    # One host receives 100 Gb/s on two PFs, also visible on its VF representors.
    rates = {
        "enp65s0f0np0": 50,
        "enp65s0f1np1": 50,
        "enp65s0f0r0": 60,
        "enp65s0f1r15": 40,
        "bond0": 100,
        "bond0.3606": 100,
    }
    for query in physical_queries:
        assert sum(rate for device, rate in rates.items() if matches_device(query, device)) == 100


def test_promql_regression_cases_exercise_current_dashboard_queries():
    panels = {p["id"]: p for p in json.loads(DASHBOARD.read_text())["panels"]}
    cases = yaml.safe_load((DASHBOARD.parent / "tests/fixtures/showcase-promql.yaml").read_text())
    for case in cases["tests"]:
        panel, ref, _ = case["name"].split(".")
        target = next(
            t for t in panels[int(panel.removeprefix("panel"))]["targets"] if t["refId"] == ref
        )
        rendered = target["expr"].replace("$__rate_interval", "5m")
        for variable in ["domain", "ceph_pool", "instance"]:
            rendered = rendered.replace("$" + variable, ".*")
        assert case["promql_expr_test"][0]["expr"] == rendered


def test_hypervisor_allocations_use_separate_count_and_byte_panels():
    panels = {p["id"]: p for p in json.loads(DASHBOARD.read_text())["panels"]}
    for panel_id, unit, metric in [
        (81, "short", "openstack_nova_vcpus_used"),
        (92, "bytes", "openstack_nova_memory_used_bytes"),
        (93, "bytes", "openstack_nova_local_storage_used_bytes"),
    ]:
        panel = panels[panel_id]
        assert panel["fieldConfig"]["defaults"]["unit"] == unit
        assert len(panel["targets"]) == 1
        assert metric in panel["targets"][0]["expr"]


def test_dashboard_panels_have_unique_ids_and_do_not_overlap():
    panels = json.loads(DASHBOARD.read_text())["panels"]
    assert len({p["id"] for p in panels}) == len(panels)
    for index, panel in enumerate(panels):
        a = panel["gridPos"]
        for other in panels[index + 1 :]:
            b = other["gridPos"]
            assert not (
                a["x"] < b["x"] + b["w"]
                and b["x"] < a["x"] + a["w"]
                and a["y"] < b["y"] + b["h"]
                and b["y"] < a["y"] + a["h"]
            ), (panel["title"], other["title"])


def test_unavailable_ovs_interface_metrics_have_an_explicit_coverage_notice():
    panels = {p["id"]: p for p in json.loads(DASHBOARD.read_text())["panels"]}
    notice = panels[46]
    assert notice["type"] == "text"
    assert "not exported" in notice["options"]["content"]
    assert "targets" not in notice
    for panel_id in [21, 24, 38, 90]:
        assert (
            "missing VM network data does not mean zero traffic" in panels[panel_id]["description"]
        )
