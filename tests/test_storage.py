from pathlib import Path

import pytest

from soak_tester.config import load_config
from soak_tester.workloads.storage import fio_command


@pytest.mark.parametrize(
    "profile,rw",
    [
        ("mixed-randrw", "randrw"),
        ("random-read", "randread"),
        ("random-write", "randwrite"),
        ("sequential-read", "read"),
        ("sequential-write", "write"),
    ],
)
def test_fio_command_honors_selected_profile(profile, rw):
    config = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    config.workloads.storage.profile = profile
    command = fio_command(config, "/dev/vdb", 60)
    assert f"--rw={rw}" in command
    assert command[command.index("--filename") + 1] == "/dev/vdb"
