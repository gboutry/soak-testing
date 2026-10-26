import json
from pathlib import Path

import pytest

from soak_tester.config import load_config, dump_config
from soak_tester.evidence import assess_run
from soak_tester.planner import create_plan, write_plan
from soak_tester.report import generate_final_report
from soak_tester.state import initial_state, save_state


def artifacts(tmp_path):
    cfg = load_config(Path(__file__).parents[1] / "configs/network-virtio.yaml")
    (tmp_path / "config.yaml").write_text(dump_config(cfg))
    plan = create_plan(cfg, target_percent=100, flavor_specs={})
    write_plan(tmp_path / "plan.yaml", plan)
    state = initial_state("r", target_percent=100, os_cloud="test", duration="100s")
    state.timeline.steady_state_started_at = "1970-01-01T00:01:40Z"
    state.timeline.steady_state_ended_at = "1970-01-01T00:03:20Z"
    state.summary["workload_status"] = "completed"
    state.summary["controller_collection"] = {"metrics": 4, "events": 0, "errors": []}
    state.status = "completed"
    save_state(tmp_path, state)
    (tmp_path / "placement.json").write_text(
        json.dumps(
            [
                {"worker_id": "network-sender-000", "hypervisor_hostname": "n1"},
                {"worker_id": "network-receiver-000", "hypervisor_hostname": "n2"},
            ]
        )
    )
    (tmp_path / "events.jsonl").write_text("")
    (tmp_path / "controller-status.json").write_text(
        json.dumps(
            {
                "heartbeats": [
                    {"worker_id": worker, "status": "finished", "delivery_failures": 0}
                    for worker in ["network-sender-000", "network-receiver-000"]
                ]
            }
        )
    )
    rows = []
    for flow, value, start, end in [
        (0, 1000, 10, 20),
        (0, 10, 110, 120),
        (0, 20, 120, 140),
        (1, 30, 110, 140),
    ]:
        rows.append(
            {
                "metric": "iperf.goodput_bps",
                "value": value * 1e9,
                "worker_id": "network-sender-000",
                "tags": {"flow_id": f"network-flow-{flow:05d}", "flow_file": str(start)},
                "measurement_start": start,
                "measurement_end": end,
                "ts": end,
            }
        )
    (tmp_path / "metrics.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    return rows


def test_report_uses_only_contained_observations_and_weights_duration(tmp_path):
    artifacts(tmp_path)
    (tmp_path / "cleanup.json").write_text(
        json.dumps({"status": "passed", "remaining": {"server": []}})
    )
    report = generate_final_report(tmp_path)
    assert report["measurement_validity"] == "valid_observations"
    assert report["workload_success"] is True
    assert report["cleanup_status"] == "passed"
    assert report["active_resources"] == 0
    assert report["achieved"]["iperf.goodput_gbps_avg"] == pytest.approx(10 / 3 + 40 / 3 + 30)
    assert report["evidence"]["excluded_metric_count"] == 1
    assert report["evidence"]["coverage"]["network"]["observed_seconds"]["network-flow-00000"] == 30
    assert report["evidence"]["continuous_rate_claimed"] is False


def test_failed_attempt_and_cleanup_are_separate_from_valid_later_observations(tmp_path):
    artifacts(tmp_path)
    (tmp_path / "events.jsonl").write_text(
        json.dumps(
            {"event": "iperf_failed", "worker_id": "network-sender-000", "details": {"attempt": 0}}
        )
        + "\n"
    )
    (tmp_path / "cleanup.json").write_text(
        json.dumps({"status": "failed", "errors": ["API unavailable"]})
    )
    report = generate_final_report(tmp_path)
    assert report["workload_success"] is False
    assert report["measurement_validity"] == "valid_observations"
    assert len(report["evidence"]["failed_attempts"]) == 1
    assert report["cleanup_status"] == "failed"
    assert report["active_resources"] is None


def test_missing_flow_and_timestamp_only_result_are_invalid(tmp_path):
    rows = artifacts(tmp_path)
    rows = [m for m in rows if m["tags"]["flow_id"] != "network-flow-00001"]
    for row in rows:
        row.pop("measurement_start")
        row.pop("measurement_end")
    (tmp_path / "metrics.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    evidence = assess_run(tmp_path)
    assert evidence["measurement_validity"] == "invalid"
    assert evidence["coverage"]["network"]["observed"] == 0
    assert any("Network flow evidence incomplete" in e for e in evidence["errors"])
    assert generate_final_report(tmp_path)["target_attainment"]["status"] == "unassessed"


def test_bidirectional_coverage_requires_both_receiver_directions(tmp_path):
    artifacts(tmp_path)
    cfg = load_config(tmp_path / "config.yaml")
    cfg.workloads.network.bidirectional = True
    (tmp_path / "config.yaml").write_text(dump_config(cfg))
    result = assess_run(tmp_path)
    assert result["measurement_validity"] == "invalid"
    assert any("direction evidence incomplete" in e for e in result["errors"])


def test_actual_placement_is_required(tmp_path):
    artifacts(tmp_path)
    (tmp_path / "placement.json").unlink()
    assert any("placement missing" in e for e in assess_run(tmp_path)["errors"])


def test_failed_final_collection_cannot_certify_absence_of_guest_failures(tmp_path):
    artifacts(tmp_path)
    from soak_tester.state import load_state

    state = load_state(tmp_path)
    state.summary["controller_collection"]["errors"] = ["events API unreachable"]
    save_state(tmp_path, state)
    assert assess_run(tmp_path)["measurement_validity"] == "invalid"


def test_one_good_attempt_does_not_prove_a_worker_finished(tmp_path):
    artifacts(tmp_path)
    (tmp_path / "controller-status.json").unlink()
    result = assess_run(tmp_path)
    assert not result["workload_success"]
    assert result["measurement_validity"] == "invalid"


def test_lost_guest_events_prevent_success_claim(tmp_path):
    artifacts(tmp_path)
    path = tmp_path / "controller-status.json"
    data = json.loads(path.read_text())
    data["heartbeats"][0]["delivery_failures"] = 1
    path.write_text(json.dumps(data))
    result = assess_run(tmp_path)
    assert not result["workload_success"]
    assert any("delivery failed" in e for e in result["errors"])


def test_udp_run_requires_protocol_and_receiver_loss_evidence(tmp_path):
    rows = artifacts(tmp_path)
    cfg = load_config(tmp_path / "config.yaml")
    cfg.workloads.network.protocol = "udp"
    (tmp_path / "config.yaml").write_text(dump_config(cfg))
    assert "UDP receiver/loss evidence incomplete" in assess_run(tmp_path)["errors"]
    for row in rows:
        row["tags"]["protocol"] = "udp"
    for flow in ["network-flow-00000", "network-flow-00001"]:
        for key, value in [
            ("lost_packets", 1),
            ("packets", 10),
            ("lost_percent", 10),
            ("jitter_ms", 0.2),
        ]:
            rows.append(
                {
                    "metric": "iperf.udp_" + key,
                    "value": value,
                    "ts": 140,
                    "tags": {"protocol": "udp", "flow_id": flow},
                }
            )
    (tmp_path / "metrics.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert assess_run(tmp_path)["measurement_validity"] == "valid_observations"


def test_requested_receiver_intervals_must_survive_collection(tmp_path):
    artifacts(tmp_path)
    cfg = load_config(tmp_path / "config.yaml")
    cfg.workloads.network.capture_receiver_intervals = True
    (tmp_path / "config.yaml").write_text(dump_config(cfg))
    assert "Requested receiver interval evidence incomplete" in assess_run(tmp_path)["errors"]
    (tmp_path / "events.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "event": "iperf_receiver_intervals",
                    "details": {"flow_id": flow, "intervals": [{"sum": {"bits_per_second": 10}}]},
                }
            )
            + "\n"
            for flow in ["network-flow-00000", "network-flow-00001"]
        )
    )
    assert assess_run(tmp_path)["measurement_validity"] == "valid_observations"


def test_integrated_guest_tuning_readbacks_survive_report_generation(tmp_path):
    artifacts(tmp_path)
    events = [
        {"event": name, "worker_id": "network-receiver-000", "details": {"after": "verified"}}
        for name in [
            "network_interrupt_coalescing_tuning",
            "network_large_receive_offload_tuning",
            "network_tx_qdisc_tuning",
        ]
    ]
    (tmp_path / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
    assert generate_final_report(tmp_path)["repeatability"]["guest_network_settings"] == events
