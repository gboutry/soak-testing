from __future__ import annotations

import json

from soak_tester.config import SoakConfig


def worker_cloud_init(
    *,
    config: SoakConfig,
    role: str,
    worker_id: str,
    controller_url: str,
    duration_seconds: int,
    peers: list[str] | None = None,
) -> str:
    peers = peers or []
    script = f"""#!/bin/bash
set -eux
mkdir -p /var/log/soak-tester
cat >/usr/local/bin/soak-worker <<'PY'
#!/usr/bin/env python3
import json
import os
import subprocess
import time
import urllib.request

ROLE = {json.dumps(role)}
WORKER_ID = {json.dumps(worker_id)}
CONTROLLER = {json.dumps(controller_url)}
DURATION = {duration_seconds}
PEERS = {json.dumps(peers)}
CPU_LOAD = {config.workloads.compute.cpu_load_percent}
MEMORY_LOAD = {config.workloads.compute.memory_load_percent}
FLOW_TIME = {int(config.workloads.network.flow_duration.rstrip("smhd"))}
PARALLEL = {config.workloads.network.parallel_streams_per_flow}
BITRATE = {json.dumps(str(config.workloads.network.target_gbps_per_flow) + "G")}
IODEPTH = {config.workloads.storage.iodepth}
NUMJOBS = {config.workloads.storage.numjobs}
BLOCK_SIZE = {json.dumps(config.workloads.storage.block_size)}
RWMIXREAD = {config.workloads.storage.rwmixread}


def post(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        CONTROLLER + path,
        data=data,
        headers={{'content-type': 'application/json'}},
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as exc:
        try:
            with open('/dev/console', 'a') as stream:
                stream.write('SOAK_POST_FAILED ' + str(exc) + '\\n')
        except Exception:
            pass


def has_command(name):
    return subprocess.call(['which', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def start_workload():
    if ROLE == 'compute':
        if has_command('stress-ng'):
            return subprocess.Popen([
                'stress-ng', '--cpu', '0', '--cpu-load', str(CPU_LOAD),
                '--vm', '1', '--vm-bytes', str(MEMORY_LOAD) + '%',
                '--timeout', str(DURATION),
            ])
        return subprocess.Popen(['sh', '-c', 'yes >/dev/null'])
    if ROLE == 'network-receiver':
        if has_command('iperf3'):
            return subprocess.Popen(['iperf3', '--server'])
        return None
    if ROLE == 'network-sender' and PEERS:
        if has_command('iperf3'):
            return subprocess.Popen([
                'iperf3', '--client', PEERS[0], '--time', str(FLOW_TIME),
                '--parallel', str(PARALLEL), '--bitrate', BITRATE, '--json',
            ], stdout=open('/var/log/soak-tester/iperf3.json', 'a'), stderr=subprocess.STDOUT)
        return None
    if ROLE == 'storage':
        if not has_command('fio'):
            return None
        device = next((p for p in ['/dev/vdb', '/dev/disk/by-id/virtio-soak'] if os.path.exists(p)), '/dev/vdb')
        return subprocess.Popen([
            'fio', '--name=workload', '--ioengine=libaio', '--direct=1',
            '--time_based=1', '--runtime', str(DURATION), '--group_reporting=1',
            '--iodepth', str(IODEPTH), '--numjobs', str(NUMJOBS), '--bs', BLOCK_SIZE,
            '--rw=randrw', '--rwmixread', str(RWMIXREAD), '--filename', device,
            '--output-format=json',
        ], stdout=open('/var/log/soak-tester/fio.json', 'a'), stderr=subprocess.STDOUT)
    return None


proc = start_workload()
end = time.time() + DURATION
while time.time() < end:
    post('/heartbeat', {{
        'worker_id': WORKER_ID,
        'role': ROLE,
        'status': None if proc is None else proc.poll(),
        'ts': time.time(),
    }})
    time.sleep(5)
if proc is not None and proc.poll() is None:
    proc.terminate()
post('/heartbeat', {{
    'worker_id': WORKER_ID,
    'role': ROLE,
    'status': 'finished',
    'ts': time.time(),
}})
PY
chmod 0755 /usr/local/bin/soak-worker
nohup /usr/local/bin/soak-worker >/dev/console 2>&1 &
"""
    return script
