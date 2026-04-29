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
    flow_assignments: list[dict[str, object]] | None = None,
    receiver_ports: list[int] | None = None,
) -> str:
    peers = peers or []
    flow_assignments = flow_assignments or []
    receiver_ports = receiver_ports or []
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
FLOW_ASSIGNMENTS = {json.dumps(flow_assignments)}
RECEIVER_PORTS = {json.dumps(receiver_ports)}
CPU_LOAD = {config.workloads.compute.cpu_load_percent}
MEMORY_LOAD = {config.workloads.compute.memory_load_percent}
FLOW_TIME = {int(config.workloads.network.flow_duration.rstrip("smhd"))}
PARALLEL = {config.workloads.network.parallel_streams_per_flow}
BITRATE = {json.dumps(str(config.workloads.network.target_gbps_per_flow) + "G")}
IODEPTH = {config.workloads.storage.iodepth}
NUMJOBS = {config.workloads.storage.numjobs}
BLOCK_SIZE = {json.dumps(config.workloads.storage.block_size)}
RWMIXREAD = {config.workloads.storage.rwmixread}
SERVER_PROCS = []
PARSED_IPERF_FILES = set()


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
            ports = RECEIVER_PORTS or [5201]
            for port in ports:
                SERVER_PROCS.append(subprocess.Popen(
                    ['iperf3', '--server', '--port', str(port)],
                    stdout=open('/var/log/soak-tester/iperf3-server-' + str(port) + '.log', 'a'),
                    stderr=subprocess.STDOUT,
                ))
            return None
        return None
    if ROLE == 'network-sender' and (FLOW_ASSIGNMENTS or PEERS):
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
    flow_by_id = {{flow.get('flow_id'): flow for flow in FLOW_ASSIGNMENTS}}
    paths = [
        '/var/log/soak-tester/' + name
        for name in sorted(os.listdir('/var/log/soak-tester'))
        if name.startswith('iperf3-') and name.endswith('.json')
    ]
    if not paths:
        return
    parsed = 0
    for path in paths:
        if path in PARSED_IPERF_FILES:
            continue
        try:
            data = json.load(open(path))
            flow_id = os.path.basename(path).split('-attempt-', 1)[0].replace('iperf3-', '')
            flow = flow_by_id.get(flow_id) or {{}}
            receiver = flow.get('receiver_worker_id') or (PEERS[0] if PEERS else '')
            tags = {{
                'receiver': str(receiver),
                'receiver_ip': str(flow.get('receiver_private_ip') or (PEERS[0] if PEERS else '')),
                'receiver_port': str(flow.get('receiver_port') or ''),
                'flow_id': str(flow.get('flow_id') or flow_id),
                'source_host': str(flow.get('source_host') or ''),
                'dest_host': str(flow.get('dest_host') or ''),
            }}
            if data.get('error'):
                metrics = [metric('network', 'iperf.flow_failed', 1, 'count', tags)]
                post_many('/metrics', metrics)
                post('/events', event('warning', 'network', 'iperf_failed', {{'error': data.get('error'), **tags}}))
                parsed += len(metrics)
                continue
            end = data.get('end', {{}})
            received = end.get('sum_received') or {{}}
            sent = end.get('sum_sent') or {{}}
            goodput = received.get('bits_per_second') or sent.get('bits_per_second')
            retransmits = sent.get('retransmits')
            metrics = []
            tags['flow_file'] = os.path.basename(path)
            if goodput is not None:
                metrics.append(metric('network', 'iperf.goodput_bps', float(goodput), 'bps', tags))
            if retransmits is not None:
                metrics.append(metric('network', 'iperf.retransmits', float(retransmits), 'count', tags))
            post_many('/metrics', metrics)
            parsed += len(metrics)
            PARSED_IPERF_FILES.add(path)
        except Exception as exc:
            post('/events', event('warning', 'network', 'iperf_parse_failed', {{'path': path, 'error': str(exc)}}))
    if parsed:
        post('/events', event('info', 'network', 'iperf_completed', {{'metrics': parsed, 'flows': len(PARSED_IPERF_FILES)}}))


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


end = time.time() + DURATION
proc = start_workload()
flow_index = 0
flow_procs = {{}}
flow_attempts = {{}}
while time.time() < end:
    if ROLE == 'network-sender' and has_command('iperf3'):
        assignments = FLOW_ASSIGNMENTS or [
            {{
                'flow_id': 'legacy-flow-00000',
                'receiver_private_ip': PEERS[0] if PEERS else '',
                'receiver_port': 5201,
                'target_gbps': float(BITRATE.rstrip('G')),
                'parallel_streams': PARALLEL,
            }}
        ]
        for flow in assignments:
            flow_id = str(flow.get('flow_id'))
            active = flow_procs.get(flow_id)
            if active is not None and active.poll() is None:
                continue
            remaining = int(end - time.time())
            if remaining > 1:
                flow_time = max(1, min(FLOW_TIME, remaining))
                attempt = flow_attempts.get(flow_id, 0)
                path = '/var/log/soak-tester/iperf3-' + flow_id + '-attempt-' + str(attempt).zfill(5) + '.json'
                receiver_ip = str(flow.get('receiver_private_ip') or '')
                if not receiver_ip:
                    continue
                flow_procs[flow_id] = subprocess.Popen([
                    'iperf3', '--client', receiver_ip,
                    '--port', str(flow.get('receiver_port') or 5201),
                    '--time', str(flow_time),
                    '--parallel', str(flow.get('parallel_streams') or PARALLEL),
                    '--bitrate', str(flow.get('target_gbps') or BITRATE.rstrip('G')) + 'G',
                    '--json',
                ], stdout=open(path, 'w'), stderr=subprocess.STDOUT)
                flow_attempts[flow_id] = attempt + 1
                flow_index += 1
    running_flows = sum(1 for item in flow_procs.values() if item.poll() is None)
    post('/heartbeat', {{
        'worker_id': WORKER_ID,
        'role': ROLE,
        'status': running_flows if ROLE == 'network-sender' else (None if proc is None else proc.poll()),
        'ts': time.time(),
    }})
    if ROLE == 'network-sender':
        parse_iperf3_output()
    time.sleep(5)
for active in flow_procs.values():
    if active.poll() is None:
        try:
            active.wait(timeout=10)
        except subprocess.TimeoutExpired:
            active.terminate()
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
for active in SERVER_PROCS:
    if active.poll() is None:
        active.terminate()
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
