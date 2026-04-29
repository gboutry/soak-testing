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


def post_many(path, payloads):
    if payloads:
        post(path, payloads)


def event(level, plane, name, details=None):
    return {{
        'level': level,
        'plane': plane,
        'worker_id': WORKER_ID,
        'event': name,
        'details': details or {{}},
        'ts': time.time(),
    }}


def metric(plane, name, value, unit, tags=None):
    return {{
        'plane': plane,
        'worker_id': WORKER_ID,
        'metric': name,
        'value': value,
        'unit': unit,
        'tags': tags or {{}},
        'ts': time.time(),
    }}


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
            flow_time = min(FLOW_TIME, DURATION)
            return subprocess.Popen([
                'iperf3', '--client', PEERS[0], '--time', str(flow_time),
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


def parse_iperf3_output():
    path = '/var/log/soak-tester/iperf3.json'
    if not os.path.exists(path):
        return
    try:
        data = json.load(open(path))
        end = data.get('end', {{}})
        received = end.get('sum_received') or {{}}
        sent = end.get('sum_sent') or {{}}
        goodput = received.get('bits_per_second') or sent.get('bits_per_second')
        retransmits = sent.get('retransmits')
        metrics = []
        tags = {{'receiver': PEERS[0] if PEERS else ''}}
        if goodput is not None:
            metrics.append(metric('network', 'iperf.goodput_bps', float(goodput), 'bps', tags))
        if retransmits is not None:
            metrics.append(metric('network', 'iperf.retransmits', float(retransmits), 'count', tags))
        post_many('/metrics', metrics)
        post('/events', event('info', 'network', 'iperf_completed', {{'metrics': len(metrics)}}))
    except Exception as exc:
        post('/events', event('warning', 'network', 'iperf_parse_failed', {{'error': str(exc)}}))


def _max_percentile(jobs, op, percentile):
    values = []
    for job in jobs:
        percentiles = (((job.get(op) or {{}}).get('clat_ns') or {{}}).get('percentile') or {{}})
        if percentile in percentiles:
            values.append(float(percentiles[percentile]))
    return max(values) if values else None


def parse_fio_output():
    path = '/var/log/soak-tester/fio.json'
    if not os.path.exists(path):
        return
    try:
        data = json.load(open(path))
        jobs = data.get('jobs') or []
        metrics = [
            metric('storage', 'fio.read_iops', sum(float((j.get('read') or {{}}).get('iops') or 0) for j in jobs), 'iops'),
            metric('storage', 'fio.write_iops', sum(float((j.get('write') or {{}}).get('iops') or 0) for j in jobs), 'iops'),
            metric('storage', 'fio.read_bps', sum(float((j.get('read') or {{}}).get('bw_bytes') or 0) * 8 for j in jobs), 'bps'),
            metric('storage', 'fio.write_bps', sum(float((j.get('write') or {{}}).get('bw_bytes') or 0) * 8 for j in jobs), 'bps'),
        ]
        for op in ('read', 'write'):
            p95 = _max_percentile(jobs, op, '95.000000')
            p99 = _max_percentile(jobs, op, '99.000000')
            if p95 is not None:
                metrics.append(metric('storage', 'fio.' + op + '_p95_latency_ns', p95, 'ns'))
            if p99 is not None:
                metrics.append(metric('storage', 'fio.' + op + '_p99_latency_ns', p99, 'ns'))
        post_many('/metrics', metrics)
        post('/events', event('info', 'storage', 'fio_completed', {{'metrics': len(metrics)}}))
    except Exception as exc:
        post('/events', event('warning', 'storage', 'fio_parse_failed', {{'error': str(exc)}}))


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
if proc is not None and proc.poll() is None and ROLE not in ('network-receiver',):
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.terminate()
if ROLE == 'network-sender':
    parse_iperf3_output()
if ROLE == 'storage':
    parse_fio_output()
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
