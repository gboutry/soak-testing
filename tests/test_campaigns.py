import json
import runpy
from pathlib import Path

import yaml

from soak_tester.config import load_config
from soak_tester.planner import FlavorSpec, create_plan


ROOT = Path(__file__).parents[1]


def test_vm_spread_preserves_cpu_ram_and_stream_budgets():
    definitions = yaml.safe_load((ROOT / "tests/fixtures/budgets/flavors.yaml").read_text())[
        "flavors"
    ]
    specs = {
        name: FlavorSpec(name=name, vcpus=v["vcpus"], ram_gib=v["ram_mb"] / 1024)
        for name, v in definitions.items()
    }
    budgets = []
    for name, workers, queues in [("vmspread-baseline", 16, 16), ("vmspread-32", 32, 8)]:
        cfg = load_config(ROOT / f"tests/fixtures/budgets/{name}.yaml")
        plan = create_plan(cfg, target_percent=100, flavor_specs=specs)
        count = plan.network.sender_count + plan.network.receiver_count
        assert count == workers
        assert cfg.workloads.network.guest_combined_queues == queues
        assert cfg.workloads.network.guest_rx_ring_size == 256
        spec = specs[cfg.flavors.network_worker]
        budgets.append(
            (
                count * spec.vcpus,
                count * spec.ram_gib,
                sum(f.parallel_streams for f in plan.network.flow_assignments),
            )
        )
    assert budgets == [(256, 512, 128), (256, 512, 128)]


def test_full_profiles_include_support_workers_within_80_percent_budget():
    definitions = yaml.safe_load((ROOT / "tests/fixtures/budgets/flavors.yaml").read_text())[
        "flavors"
    ]
    specs = {
        name: FlavorSpec(
            name=name,
            vcpus=value["vcpus"],
            ram_gib=value["ram_mb"] / 1024,
        )
        for name, value in definitions.items()
    }
    capacity = json.loads((ROOT / "tests/fixtures/budgets/capacity.json").read_text())
    for name in ["small-all-80", "large-all-80", "large-sequential-write-80"]:
        cfg = load_config(ROOT / f"tests/fixtures/budgets/{name}.yaml")
        plan = create_plan(cfg, target_percent=80, flavor_specs=specs)
        support = (
            plan.network.sender_count + plan.network.receiver_count + plan.storage.worker_count
        )
        cpu = plan.compute.planned_vcpus + support * 4 + 2
        ram = plan.compute.planned_ram_gib + support * 8 + 4
        assert capacity["target_vcpus"] * 0.99 <= cpu <= capacity["target_vcpus"]
        assert capacity["target_ram_gib"] * 0.99 <= ram <= capacity["target_ram_gib"]
        disks = (
            sum(
                definitions[group["flavor"]]["disk_gb"] * group["count"]
                for group in plan.compute.worker_groups
            )
            + (support + 1) * 16
        )
        assert disks <= capacity["available_root_disk_gb"]


def test_campaign_validation_refuses_fio_failure(tmp_path):
    cfg = load_config(ROOT / "tests/fixtures/budgets/fio-baseline.yaml")
    # Only the fields consumed by evidence validation are needed here.
    (tmp_path / "plan.yaml").write_text(yaml.safe_dump({"storage": {"worker_count": 1}}))
    (tmp_path / "metrics.jsonl").write_text(
        json.dumps({"worker_id": "storage-000", "metric": "fio.failed", "value": 1}) + "\n"
    )
    (tmp_path / "events.jsonl").write_text("")
    assert cfg.workloads.storage.enabled
    validate = runpy.run_path(str(ROOT / "scripts/run_campaigns.py"))["validate_evidence"]
    errors = validate(tmp_path, ROOT / "tests/fixtures/budgets/fio-baseline.yaml")
    assert "fio.failed" in errors
    assert any("FIO worker evidence incomplete" in error for error in errors)


def test_campaign_stop_retires_rally_groups_before_fallback_cleanup(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import textwrap
    import time

    child_script = tmp_path / "child.py"
    child_script.write_text(
        textwrap.dedent("""
        import os, signal, subprocess, sys, time
        from pathlib import Path
        path = Path(sys.argv[1])
        if sys.argv[2] == 'leader':
            signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
            subprocess.Popen([
                sys.executable, __file__, str(path.with_suffix('.grandchild')), 'ignore'
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        path.write_text(str(os.getpid()))
        while True:
            time.sleep(.1)
    """)
    )
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        textwrap.dedent("""
        import sys
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path
        from soak_tester.cli import _cancellable_rally_runner, _stop_signals
        directory = Path(sys.argv[1])
        with _stop_signals() as stopped:
            runner = _cancellable_rally_runner(stopped.is_set)
            def execute(mode):
                try:
                    runner([sys.executable, str(directory / 'child.py'),
                            str(directory / mode), mode])
                except InterruptedError:
                    return
                raise AssertionError('command did not interrupt')
            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks = [pool.submit(execute, mode) for mode in ['leader', 'report']]
                for task in tasks:
                    task.result()
            try:
                runner([sys.executable, str(directory / 'child.py'),
                        str(directory / 'late'), 'destroy'])
            except InterruptedError:
                pass
            else:
                raise AssertionError('teardown reset the cancellation budget')
        (directory / 'finished').touch()
    """)
    )
    process = subprocess.Popen([sys.executable, str(wrapper), str(tmp_path)])
    paths = [tmp_path / name for name in ["leader", "leader.grandchild", "report"]]

    def live(pid):
        status = Path(f"/proc/{pid}/stat")
        return status.exists() and status.read_text().rsplit(")", 1)[1].split()[0] != "Z"

    try:
        deadline = time.monotonic() + 10
        while not all(path.exists() for path in paths):
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        stop = runpy.run_path(str(ROOT / "scripts/run_campaigns.py"))["_stop_campaign_process"]
        stop(process)
        assert process.returncode == 0
        assert (tmp_path / "finished").exists()
        assert not (tmp_path / "late").exists()
        deadline = time.monotonic() + 2
        while any(live(int(path.read_text())) for path in paths) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not any(live(int(path.read_text())) for path in paths)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for path in paths:
            if path.exists():
                pid = int(path.read_text())
                if live(pid):
                    os.kill(pid, signal.SIGKILL)
