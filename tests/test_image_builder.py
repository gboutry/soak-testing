import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("corrupt", [False, True])
def test_builder_preserves_sparse_image_and_rejects_bad_transfer(tmp_path, corrupt):
    image = tmp_path / "image"
    image.mkdir()
    (image / "imagecraft.yaml").write_text("name: test\n")
    binary = tmp_path / "bin"
    binary.mkdir()
    lxc = binary / "lxc"
    lxc.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib, os, sys\n"
        "data = bytes(4 * 1024 * 1024) + b'image-data'\n"
        "args = sys.argv[1:]\n"
        "if args[0] == 'exec':\n"
        "    command = args[args.index('--') + 1:]\n"
        "    if command[0] == 'test':\n"
        "        sys.exit(0 if command[-1].endswith('/pc.img') else 1)\n"
        "    if command[0] == 'sha256sum':\n"
        "        print(hashlib.sha256(data).hexdigest() + '  ' + command[1])\n"
        "    if command[0] == 'cat':\n"
        "        sys.stdout.buffer.write(data[:-1] if os.getenv('CORRUPT') else data)\n"
        "    if command[0] == 'tar':\n"
        "        sys.stdin.buffer.read()\n"
    )
    lxc.chmod(0o755)
    env = {**os.environ, "PATH": f"{binary}:{os.environ['PATH']}"}
    if corrupt:
        env["CORRUPT"] = "1"
    result = subprocess.run(
        ["sh", str(ROOT / "scripts/images/build_imagecraft_vm.sh"), str(image)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert (result.returncode != 0) == corrupt, result.stderr
    if not corrupt:
        disk = image / "disk.img"
        assert disk.read_bytes() == bytes(4 * 1024 * 1024) + b"image-data"
        assert disk.stat().st_blocks * 512 < disk.stat().st_size
