import json
import threading
import time
import pytest

from soak_tester.run.provision import ResourceTracker, _run_boot_tasks


def test_run_boot_tasks_respects_concurrency():
    active = 0
    max_active = 0
    lock = threading.Lock()

    def task(index: int) -> int:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return index

    results = _run_boot_tasks(
        [(f"worker-{index}", lambda index=index: task(index)) for index in range(8)],
        concurrency=3,
    )

    assert results == list(range(8))
    assert max_active == 3


def test_resource_tracker_writes_all_records_from_threads(tmp_path):
    tracker = ResourceTracker(tmp_path)

    def add_record(index: int) -> None:
        tracker.add(
            {
                "type": "server",
                "id": f"server-{index}",
                "name": f"server-{index}",
            }
        )

    threads = [threading.Thread(target=add_record, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    payload = json.loads((tmp_path / "resources.json").read_text())

    assert {record["id"] for record in payload["resources"]} == {
        f"server-{index}" for index in range(20)
    }


def test_resource_tracker_does_not_destroy_existing_recovery_manifest(tmp_path):
    tracker = ResourceTracker(tmp_path)
    tracker.add({"type": "server", "id": "owned-vm"})
    original = (tmp_path / "resources.json").read_bytes()
    with pytest.raises(FileExistsError, match="overwrite"):
        ResourceTracker(tmp_path)
    assert (tmp_path / "resources.json").read_bytes() == original


def test_interruption_persists_created_id_before_stopping(tmp_path):
    tracker = ResourceTracker(tmp_path, stop_requested=lambda: True)
    with pytest.raises(InterruptedError):
        tracker.add({"type": "server", "id": "just-created"})
    assert json.loads((tmp_path / "resources.json").read_text())["resources"] == [
        {"type": "server", "id": "just-created"}
    ]
