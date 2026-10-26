from __future__ import annotations

import inspect
import json

from soak_tester.config import SoakConfig
from soak_tester.workloads import results
from soak_tester.workloads.storage import fio_command
from soak_tester.workloads.network import iperf_client_command
from soak_tester.util import duration_seconds as parse_duration_seconds


def _python_literal(value: object) -> str:
    return repr(value)


def worker_cloud_init(
    *,
    config: SoakConfig,
    role: str,
    worker_id: str,
    controller_url: str,
    duration_seconds: int,
    control_token: str = "",
    workload_duration_seconds: int | None = None,
    flow_assignments: list[dict[str, object]] | None = None,
    receiver_ports: list[int] | None = None,
) -> str:
    flow_assignments = flow_assignments or []
    receiver_ports = receiver_ports or []
    workload_duration_seconds = workload_duration_seconds or duration_seconds
    client_template = iperf_client_command(
        config,
        "__SOAK_RECEIVER__",
        receiver_port="__SOAK_PORT__",
        seconds="__SOAK_SECONDS__",
        streams="__SOAK_STREAMS__",
        bitrate="__SOAK_BITRATE__",
    )
    script = f"""#!/bin/bash
set -eux
mkdir -p /var/log/soak-tester
if [ {json.dumps(role.startswith("network-"))} = true ]; then
    missing_diagnostics=false
    for diagnostic in ethtool lspci ip mpstat; do
        if ! command -v "$diagnostic" >/dev/null 2>&1; then
            missing_diagnostics=true
            break
        fi
    done
    if [ "$missing_diagnostics" = true ] && command -v apt-get >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update || true
        apt-get install -y ethtool pciutils iproute2 sysstat || true
    fi
fi
if [ {json.dumps(role.startswith("network-"))} = true ]; then
    sysctl -w net.core.rmem_max=134217728 || true
    sysctl -w net.core.wmem_max=134217728 || true
    sysctl -w 'net.ipv4.tcp_rmem=4096 87380 134217728' || true
    sysctl -w 'net.ipv4.tcp_wmem=4096 65536 134217728' || true
fi
cat >/usr/local/bin/soak-worker <<'PY'
#!/usr/bin/env python3
{inspect.getsource(results)}
import json
import os
import re
import stat
import subprocess
import time
import urllib.request

ROLE = {json.dumps(role)}
WORKER_ID = {json.dumps(worker_id)}
CONTROLLER = {_python_literal(controller_url)}
CONTROL_TOKEN = {_python_literal(control_token)}
AGENT_DURATION = {duration_seconds}
WORKLOAD_DURATION = {workload_duration_seconds}
FIO_COMMAND = {_python_literal(fio_command(config, "__SOAK_VOLUME__", workload_duration_seconds))}
FLOW_ASSIGNMENTS = {_python_literal(flow_assignments)}
RECEIVER_PORTS = {_python_literal(receiver_ports)}
CPU_LOAD = {config.workloads.compute.cpu_load_percent}
MEMORY_LOAD = {config.workloads.compute.memory_load_percent}
FLOW_TIME = {parse_duration_seconds(config.workloads.network.flow_duration)}
IPERF_BIDIRECTIONAL = {repr(config.workloads.network.bidirectional)}
IPERF_REVERSE = {repr(config.workloads.network.reverse)}
IPERF_OMIT_SECONDS = {config.workloads.network.omit_seconds}
IPERF_COMMAND_TEMPLATE = {_python_literal(client_template)}
OFFERED_LOAD = {_python_literal(config.workloads.network.offered_load)}
GUEST_COMBINED_QUEUES = {_python_literal(config.workloads.network.guest_combined_queues)}
GUEST_RX_RING_SIZE = {_python_literal(config.workloads.network.guest_rx_ring_size)}
IPERF_PROTOCOL = {config.workloads.network.protocol!r}
IPERF_CAPTURE_RECEIVER_INTERVALS = {config.workloads.network.capture_receiver_intervals!r}
IPERF_UDP_DATAGRAM_SIZE = {config.workloads.network.udp_datagram_size!r}
GUEST_INTERRUPT_COALESCE_USECS = {config.workloads.network.guest_interrupt_coalesce_usecs!r}
GUEST_LARGE_RECEIVE_OFFLOAD = {config.workloads.network.guest_large_receive_offload!r}
GUEST_TX_QDISC = {config.workloads.network.guest_tx_qdisc!r}
IODEPTH = {config.workloads.storage.iodepth}
NUMJOBS = {config.workloads.storage.numjobs}
BLOCK_SIZE = {_python_literal(config.workloads.storage.block_size)}
RWMIXREAD = {config.workloads.storage.rwmixread}
SERVER_PROCS = []
PARSED_IPERF_FILES = set()
IPERF_START_TIMES = {{}}
IPERF_PROCESSES = {{}}
POST_FAILURES = 0
LAST_CPU_SAMPLE = None
FIO_STARTED_AT = None


def post(path, payload):
    global POST_FAILURES
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        CONTROLLER + path,
        data=data,
        headers={{'content-type': 'application/json'}},
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as exc:
        if path in ('/metrics', '/events'):
            POST_FAILURES += 1
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


def read_text(path):
    try:
        return open(path).read().strip()
    except Exception:
        return None


def first_network_interface():
    try:
        route = subprocess.check_output(['ip', 'route', 'show', 'default'], text=True)
        parts = route.split()
        if 'dev' in parts:
            return parts[parts.index('dev') + 1]
    except Exception:
        pass
    try:
        for name in sorted(os.listdir('/sys/class/net')):
            if name == 'lo':
                continue
            if read_text('/sys/class/net/' + name + '/operstate') == 'up':
                return name
    except Exception:
        pass
    return None


def count_queues(interface, prefix):
    base = '/sys/class/net/' + interface + '/queues'
    try:
        return len([name for name in os.listdir(base) if name.startswith(prefix)])
    except Exception:
        return None


def interface_driver(interface):
    path = '/sys/class/net/' + interface + '/device/driver'
    try:
        return os.path.basename(os.path.realpath(path))
    except Exception:
        return None


def interface_pci_id(interface):
    base = '/sys/class/net/' + interface + '/device'
    vendor = read_text(base + '/vendor')
    device = read_text(base + '/device')
    if vendor and device:
        return vendor.replace('0x', '') + ':' + device.replace('0x', '')
    return None


def combined_channels(interface):
    output = subprocess.check_output(['ethtool', '-l', interface], text=True)
    maximum, current = output.split('Current hardware settings:', 1)
    def combined(section):
        match = re.search(r'^Combined:\\s+(\\d+)\\s*$', section, re.MULTILINE)
        if not match:
            raise RuntimeError('No combined channels reported for ' + interface)
        return int(match.group(1))
    return combined(maximum), combined(current)


def tune_network_queues():
    if GUEST_COMBINED_QUEUES is None:
        return None
    interface = first_network_interface()
    if not interface or interface_driver(interface) != 'virtio_net':
        raise RuntimeError('Queue tuning requires an active virtio_net interface')
    maximum, before = combined_channels(interface)
    target = min(GUEST_COMBINED_QUEUES, os.cpu_count() or 1, maximum)
    if target < 1:
        raise RuntimeError('No usable combined queues on ' + interface)
    if before != target:
        subprocess.run(['ethtool', '-L', interface, 'combined', str(target)], check=True)
    _, after = combined_channels(interface)
    details = {{'interface': interface, 'requested': GUEST_COMBINED_QUEUES,
               'maximum': maximum, 'before': before, 'target': target, 'after': after}}
    if after != target:
        raise RuntimeError('Guest queue readback mismatch: ' + json.dumps(details))
    post('/events', event('info', 'network', 'network_queue_tuning', details))
    return details


def rx_ring_size(interface):
    output = subprocess.check_output(['ethtool', '-g', interface], text=True)
    maximum, current = output.split('Current hardware settings:', 1)
    def rx(section):
        match = re.search(r'^RX:\\s+(\\d+)\\s*$', section, re.MULTILINE)
        if not match:
            raise RuntimeError('No RX ring size reported for ' + interface)
        return int(match.group(1))
    return rx(maximum), rx(current)


def tune_network_rx_ring():
    if GUEST_RX_RING_SIZE is None:
        return None
    interface = first_network_interface()
    if not interface or interface_driver(interface) != 'virtio_net':
        raise RuntimeError('RX ring tuning requires an active virtio_net interface')
    maximum, before = rx_ring_size(interface)
    if GUEST_RX_RING_SIZE > maximum:
        raise RuntimeError('Requested RX ring exceeds advertised maximum: ' + str(maximum))
    if before != GUEST_RX_RING_SIZE:
        subprocess.run(['ethtool', '-G', interface, 'rx', str(GUEST_RX_RING_SIZE)], check=True)
    _, after = rx_ring_size(interface)
    details = {{'interface': interface, 'requested': GUEST_RX_RING_SIZE,
               'maximum': maximum, 'before': before, 'after': after}}
    if after != GUEST_RX_RING_SIZE:
        raise RuntimeError('Guest RX ring readback mismatch: ' + json.dumps(details))
    post('/events', event('info', 'network', 'network_rx_ring_tuning', details))
    return details


def interrupt_coalescing(interface):
    output = subprocess.check_output(['ethtool', '-c', interface], text=True)
    adaptive = re.search(r'^Adaptive RX:\\s+(on|off)\\s+TX:\\s+(on|off)\\s*$',
                         output, re.MULTILINE)
    if not adaptive:
        raise RuntimeError('No adaptive coalescing settings reported for ' + interface)
    values = {{'adaptive_rx': adaptive.group(1), 'adaptive_tx': adaptive.group(2)}}
    for key in ['rx-usecs', 'tx-usecs']:
        match = re.search(r'^' + key + r':\\s+(\\d+)\\s*$', output, re.MULTILINE)
        if not match:
            raise RuntimeError('No ' + key + ' coalescing setting reported for ' + interface)
        values[key] = int(match.group(1))
    return values


def tune_network_interrupt_coalescing():
    if GUEST_INTERRUPT_COALESCE_USECS is None:
        return None
    interface = first_network_interface()
    if not interface:
        raise RuntimeError('Interrupt coalescing requires an active network interface')
    before = interrupt_coalescing(interface)
    target = {{'adaptive_rx': 'off', 'adaptive_tx': 'off',
              'rx-usecs': GUEST_INTERRUPT_COALESCE_USECS,
              'tx-usecs': GUEST_INTERRUPT_COALESCE_USECS}}
    if before != target:
        subprocess.run(['ethtool', '-C', interface, 'adaptive-rx', 'off',
                        'adaptive-tx', 'off', 'rx-usecs', str(GUEST_INTERRUPT_COALESCE_USECS),
                        'tx-usecs', str(GUEST_INTERRUPT_COALESCE_USECS)], check=True)
    after = interrupt_coalescing(interface)
    if after != target:
        raise RuntimeError('Guest interrupt coalescing readback mismatch: ' + json.dumps(after))
    details = {{'interface': interface, 'before': before, 'target': target, 'after': after}}
    post('/events', event('info', 'network', 'network_interrupt_coalescing_tuning', details))
    return details


def large_receive_offload(interface):
    output = subprocess.check_output(['ethtool', '-k', interface], text=True, timeout=10)
    match = re.search(r'^large-receive-offload:\\s+(on|off)(?:\\s+\\[fixed\\])?\\s*$',
                      output, re.MULTILINE)
    if not match:
        raise RuntimeError('No large receive offload state reported for ' + interface)
    return match.group(1) == 'on', '[fixed]' in match.group(0)


def tune_network_large_receive_offload():
    if GUEST_LARGE_RECEIVE_OFFLOAD is None or ROLE != 'network-receiver':
        return None
    interface = first_network_interface()
    if not interface or not has_command('ethtool'):
        raise RuntimeError('LRO tuning requires an active interface and ethtool')
    paths = ['/proc/sys/net/ipv4/ip_forward',
             '/proc/sys/net/ipv4/conf/' + interface + '/forwarding',
             '/proc/sys/net/ipv6/conf/all/forwarding',
             '/proc/sys/net/ipv6/conf/' + interface + '/forwarding']
    if any(read_text(path) != '0' for path in paths):
        raise RuntimeError('LRO tuning requires verified non-forwarding end hosts')
    before, fixed = large_receive_offload(interface)
    if before != GUEST_LARGE_RECEIVE_OFFLOAD:
        if fixed:
            raise RuntimeError('Requested LRO state is fixed by the driver on ' + interface)
        subprocess.run(['ethtool', '-K', interface, 'lro',
                        'on' if GUEST_LARGE_RECEIVE_OFFLOAD else 'off'],
                       check=True, timeout=10)
    after, _ = large_receive_offload(interface)
    if after != GUEST_LARGE_RECEIVE_OFFLOAD:
        raise RuntimeError('Guest LRO readback mismatch on ' + interface)
    details = {{'interface': interface, 'requested': GUEST_LARGE_RECEIVE_OFFLOAD,
               'before': before, 'after': after, 'forwarding_verified_disabled': True}}
    post('/events', event('info', 'network', 'network_large_receive_offload_tuning', details))
    return details


def tx_qdiscs(interface):
    return json.loads(subprocess.check_output(
        ['tc', '-j', 'qdisc', 'show', 'dev', interface], text=True, timeout=10))


def tx_mq_layout(interface, qdiscs):
    roots = [q for q in qdiscs if q.get('root')]
    if len(roots) != 1 or roots[0]['kind'] != 'mq':
        raise RuntimeError('Expected one multiqueue TX root on ' + interface)
    root = roots[0]
    handle = root.get('handle', '')
    if not re.fullmatch(r'[0-9a-fA-F]+:', handle):
        raise RuntimeError('Unsupported TX root handle on ' + interface)
    major = int(handle[:-1], 16)
    leaves = []
    for qdisc in qdiscs:
        if qdisc is root or qdisc['kind'] == 'clsact':
            continue
        parent = qdisc.get('parent', '')
        if (not re.fullmatch(r'[0-9a-fA-F]*:[0-9a-fA-F]+', parent) or
                int(parent.split(':')[0] or '0', 16) != major or
                qdisc['kind'] not in ('fq', 'fq_codel', 'pfifo_fast', 'pfifo')):
            raise RuntimeError('Unsupported TX leaf policy on ' + interface)
        if major == 0 and qdisc.get('handle') != '0:':
            raise RuntimeError('Refusing to rebuild a custom TX layout on ' + interface)
        leaves.append(qdisc)
    if len([q for q in qdiscs if q['kind'] == 'clsact']) > 1:
        raise RuntimeError('Unsupported duplicate clsact policy on ' + interface)
    queues = count_queues(interface, 'tx-')
    minors = [int(q['parent'].split(':')[1], 16) for q in leaves]
    if queues < 2 or len(minors) != queues or set(minors) != set(range(1, queues + 1)):
        raise RuntimeError('Incomplete or duplicate TX queue layout on ' + interface)
    return root, leaves


def tune_network_tx_qdisc():
    if GUEST_TX_QDISC is None:
        return None
    interface = first_network_interface()
    if not interface or not has_command('tc'):
        raise RuntimeError('TX qdisc tuning requires an active interface and tc')
    before = tx_qdiscs(interface)
    root, leaves = tx_mq_layout(interface, before)
    expected_queues = len(leaves)
    clsact = [q for q in before if q['kind'] == 'clsact']
    for parent in ['root'] + (['ingress', 'egress'] if clsact else []):
        filters = json.loads(subprocess.check_output(
            ['tc', '-j', 'filter', 'show', 'dev', interface, parent], text=True, timeout=10))
        if filters != []:
            raise RuntimeError('Refusing to replace existing TX filter policy on ' + interface)
    expected_handle = root['handle']
    if int(expected_handle[:-1], 16) == 0 and any(q['kind'] != 'fq' for q in leaves):
        # Linux does not expose the automatic mq 0: leaves to a tc graft.
        # Assign a handle before replacing leaves, without collapsing mq.
        if any(q.get('handle') == '1:' for q in before):
            raise RuntimeError('Requested TX root handle is already in use')
        expected_handle = '1:'
        subprocess.run(['tc', 'qdisc', 'replace', 'dev', interface,
                        'root', 'handle', expected_handle, 'mq'], check=True, timeout=10)
        named = tx_qdiscs(interface)
        named_root, leaves = tx_mq_layout(interface, named)
        if (named_root['handle'] != expected_handle or len(leaves) != expected_queues or
                [q for q in named if q['kind'] == 'clsact'] != clsact):
            raise RuntimeError('Named TX root readback mismatch on ' + interface)
    for leaf in leaves:
        if leaf['kind'] != 'fq':
            subprocess.run(['tc', 'qdisc', 'replace', 'dev', interface,
                            'parent', leaf['parent'], 'fq'], check=True, timeout=10)
    after = tx_qdiscs(interface)
    after_root, after_leaves = tx_mq_layout(interface, after)
    if (after_root['handle'] != expected_handle or
            len(after_leaves) != expected_queues or
            any(q['kind'] != 'fq' for q in after_leaves) or
            [q for q in after if q['kind'] == 'clsact'] != clsact):
        raise RuntimeError('Guest TX qdisc readback mismatch on ' + interface)
    details = {{'interface': interface, 'requested': GUEST_TX_QDISC,
               'root_handle_before': root['handle'], 'root_handle_after': expected_handle,
               'tx_queues': len(after_leaves), 'leaf_kind': 'fq', 'clsact_preserved': True}}
    post('/events', event('info', 'network', 'network_tx_qdisc_tuning', details))
    return details


def ethtool_offloads(interface):
    if not has_command('ethtool'):
        return {{}}
    try:
        output = subprocess.check_output(['ethtool', '-k', interface], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return {{}}
    wanted = ('tcp-segmentation-offload', 'generic-segmentation-offload', 'generic-receive-offload')
    values = {{}}
    for line in output.splitlines():
        stripped = line.strip()
        for key in wanted:
            if stripped.startswith(key + ':'):
                values[key] = stripped.split(':', 1)[1].strip()
    return values


def diagnostic_output(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        return {{'command': command, 'returncode': result.returncode, 'stdout': result.stdout[:16384], 'stderr': result.stderr[:4096]}}
    except Exception as exc:
        return {{'command': command, 'error': str(exc)}}


def collect_network_interface_info():
    interface = first_network_interface()
    if not interface:
        return {{'error': 'no active interface found'}}
    return {{
        'interface': interface,
        'driver': interface_driver(interface),
        'pci_id': interface_pci_id(interface),
        'speed_mbps': read_text('/sys/class/net/' + interface + '/speed'),
        'duplex': read_text('/sys/class/net/' + interface + '/duplex'),
        'mtu': read_text('/sys/class/net/' + interface + '/mtu'),
        'rx_queues': count_queues(interface, 'rx-'),
        'tx_queues': count_queues(interface, 'tx-'),
        'numa_node': read_text('/sys/class/net/' + interface + '/device/numa_node'),
        'offloads': ethtool_offloads(interface),
        'channels': diagnostic_output(['ethtool', '-l', interface]),
        'rings': diagnostic_output(['ethtool', '-g', interface]),
        'tcp_buffers': {{name: read_text('/proc/sys/' + name.replace('.', '/')) for name in ['net.core.rmem_max', 'net.core.wmem_max', 'net.ipv4.tcp_rmem', 'net.ipv4.tcp_wmem']}},
    }}


def _read_cpu_times():
    parts = open('/proc/stat').readline().split()[1:]
    values = [int(value) for value in parts]
    idle = values[3] + values[4]
    return sum(values), idle


def sample_system_metrics():
    global LAST_CPU_SAMPLE
    metrics = []
    try:
        current = _read_cpu_times()
        if LAST_CPU_SAMPLE is not None:
            total_delta = current[0] - LAST_CPU_SAMPLE[0]
            idle_delta = current[1] - LAST_CPU_SAMPLE[1]
            if total_delta > 0:
                metrics.append(metric('compute', 'guest.cpu_percent', 100 * (1 - idle_delta / total_delta), 'percent'))
        LAST_CPU_SAMPLE = current
    except Exception:
        pass
    try:
        meminfo = {{}}
        for line in open('/proc/meminfo'):
            key, value = line.split(':', 1)
            meminfo[key] = float(value.strip().split()[0])
        total = meminfo.get('MemTotal', 0)
        available = meminfo.get('MemAvailable', 0)
        if total > 0:
            metrics.append(metric('compute', 'guest.memory_used_percent', 100 * (total - available) / total, 'percent'))
    except Exception:
        pass
    post_many('/metrics', metrics)


def wait_for_storage_device(timeout_seconds=180):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for path in ['/dev/vdb', '/dev/disk/by-id/virtio-soak']:
            try:
                if not stat.S_ISBLK(os.stat(path).st_mode):
                    continue
            except FileNotFoundError:
                continue
            device = os.path.realpath(path)
            sources = [os.path.realpath(line.split()[0])
                       for line in read_text('/proc/mounts').splitlines()]
            if any(source.startswith(device) for source in sources):
                raise RuntimeError('Refusing fio on mounted device ' + device)
            return device
        time.sleep(1)
    raise RuntimeError('Attached storage device did not appear within deadline')


def start_workload():
    if ROLE == 'compute':
        if has_command('stress-ng'):
            return subprocess.Popen([
                'stress-ng', '--cpu', '0', '--cpu-load', str(CPU_LOAD),
                '--vm', '1', '--vm-bytes', str(MEMORY_LOAD) + '%',
                '--hdd', '1', '--hdd-bytes', '10G',
                '--hdd-opts', 'direct,sync',
                '--iostat', '1',
                '--timeout', str(WORKLOAD_DURATION),
            ])
        raise RuntimeError('stress-ng is not installed; refusing a different workload')
    if ROLE == 'network-receiver':
        if has_command('iperf3'):
            ports = RECEIVER_PORTS
            for port in ports:
                command = ['iperf3', '--server', '--port', str(port)]
                if IPERF_PROTOCOL == 'udp' or IPERF_CAPTURE_RECEIVER_INTERVALS:
                    command.append('--json')
                SERVER_PROCS.append(subprocess.Popen(
                    command,
                    stdout=open('/var/log/soak-tester/iperf3-server-' + str(port) + '.log', 'a'),
                    stderr=subprocess.STDOUT,
                ))
            return None
        return None
    if ROLE == 'network-sender':
        # Fixed worker budgets can intentionally leave this sender idle.
        # Only explicit FLOW_ASSIGNMENTS launch traffic; never invent fallback flows.
        return None
    if ROLE == 'storage':
        if not has_command('fio'):
            raise RuntimeError('fio is not installed')
        device = wait_for_storage_device()
        command = [device if arg == '__SOAK_VOLUME__' else arg for arg in FIO_COMMAND]
        post('/events', event('info', 'storage', 'fio_started', {{'device': device}}))
        global FIO_STARTED_AT
        FIO_STARTED_AT = time.time()
        return subprocess.Popen(command,
            stdout=open('/var/log/soak-tester/fio.json', 'w'), stderr=subprocess.STDOUT)
    return None


def iperf_command(flow, flow_time):
    streams = int(flow['parallel_streams'])
    per_stream_gbps = 0 if OFFERED_LOAD == 'unlimited' else float(flow['target_gbps']) / streams
    replacements = {{
        '__SOAK_RECEIVER__': str(flow['receiver_private_ip']),
        '__SOAK_PORT__': str(flow['receiver_port']),
        '__SOAK_SECONDS__': str(flow_time),
        '__SOAK_STREAMS__': str(streams),
        '__SOAK_BITRATE__': str(per_stream_gbps) + 'G',
    }}
    return [replacements.get(part, part) for part in IPERF_COMMAND_TEMPLATE]


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
        active = IPERF_PROCESSES.get(path)
        if active is not None and active.poll() is None:
            continue
        try:
            data = json.load(open(path))
            if active is not None and active.returncode and not data.get('error'):
                data['error'] = 'iperf exited with status ' + str(active.returncode)
            flow_id = os.path.basename(path).split('-attempt-', 1)[0].replace('iperf3-', '')
            flow = flow_by_id.get(flow_id) or {{}}
            receiver = flow.get('receiver_worker_id') or ''
            tags = {{
                'receiver': str(receiver),
                'receiver_ip': str(flow.get('receiver_private_ip') or ''),
                'receiver_port': str(flow.get('receiver_port') or ''),
                'flow_id': str(flow.get('flow_id') or flow_id),
                'source_host': str(flow.get('source_host') or ''),
                'dest_host': str(flow.get('dest_host') or ''),
            }}
            tags['flow_file'] = os.path.basename(path)
            metrics, events = parse_iperf3_metrics(
                json.dumps(data), worker_id=WORKER_ID, receiver=str(receiver), tags=tags,
                bidirectional=IPERF_BIDIRECTIONAL, reverse=IPERF_REVERSE,
                started_at=IPERF_START_TIMES.get(path), omit_seconds=IPERF_OMIT_SECONDS, protocol=IPERF_PROTOCOL,
                capture_receiver_intervals=IPERF_CAPTURE_RECEIVER_INTERVALS,
            )
            for item in metrics + events:
                item.setdefault('ts', time.time())
            post_many('/events', events)
            post_many('/metrics', metrics)
            parsed += len(metrics)
            PARSED_IPERF_FILES.add(path)
        except Exception as exc:
            post('/events', event('warning', 'network', 'iperf_parse_failed', {{'path': path, 'error': str(exc)}}))
            PARSED_IPERF_FILES.add(path)
    if parsed:
        post('/events', event('info', 'network', 'iperf_results_collected', {{'metrics': parsed, 'flows': len(PARSED_IPERF_FILES)}}))


def parse_fio_output():
    path = '/var/log/soak-tester/fio.json'
    if not os.path.exists(path):
        return
    try:
        metrics, events = parse_fio_metrics(open(path).read(), worker_id=WORKER_ID, started_at=FIO_STARTED_AT)
        for item in metrics + events:
            item.setdefault('ts', time.time())
        post_many('/metrics', metrics)
        post_many('/events', events)
        if metrics and not events:
            post('/events', event('info', 'storage', 'fio_completed', {{'metrics': len(metrics)}}))
    except Exception as exc:
        post('/events', event('warning', 'storage', 'fio_parse_failed', {{'error': str(exc)}}))


def software_versions():
    versions = {{'kernel': os.uname().release}}
    for name in ['iperf3', 'fio', 'stress-ng']:
        if has_command(name):
            result = subprocess.run([name, '--version'], capture_output=True, text=True, timeout=10)
            versions[name] = (result.stdout or result.stderr).splitlines()[:2]
    return versions


def wait_for_start():
    deadline = time.monotonic() + AGENT_DURATION
    while time.monotonic() < deadline:
        if ROLE == 'network-receiver' and any(proc.poll() is not None for proc in SERVER_PROCS):
            raise RuntimeError('iperf receiver exited before the start barrier')
        post('/heartbeat', {{'worker_id': WORKER_ID, 'role': ROLE, 'status': 'ready', 'ts': time.time()}})
        try:
            request = urllib.request.Request(CONTROLLER + '/control', headers={{'authorization': 'Bearer ' + CONTROL_TOKEN}})
            with urllib.request.urlopen(request, timeout=5) as response:
                control = json.load(response)
            if control.get('started_at') is not None:
                time.sleep(max(0, control['started_at'] - time.time()))
                return control
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('Controller start deadline expired')


post('/events', event('info', ROLE, 'software_versions', software_versions()))
agent_end = time.time() + AGENT_DURATION
workload_end = time.time() + WORKLOAD_DURATION
try:
    if ROLE.startswith('network-'):
        if not has_command('iperf3'):
            raise RuntimeError('iperf3 is not installed')
        tune_network_rx_ring()
        tune_network_queues()
        tune_network_interrupt_coalescing()
        tune_network_large_receive_offload()
        tune_network_tx_qdisc()
    if ROLE == 'network-receiver':
        proc = start_workload()
        control = wait_for_start()
    else:
        if ROLE == 'storage':
            wait_for_storage_device()
        control = wait_for_start()
        proc = start_workload()
    workload_end = control['ends_at']
    agent_end = workload_end + 10
except Exception as exc:
    post('/events', event('error', ROLE, 'workload_start_failed', {{'error': str(exc)}}))
    raise
if ROLE.startswith('network-'):
    post('/events', event('info', 'network', 'network_interface_validation', collect_network_interface_info()))
storage_parsed = False
process_failure_reported = False
flow_index = 0
flow_procs = {{}}
flow_attempts = {{}}
while time.time() < agent_end:
    if ROLE == 'network-sender' and has_command('iperf3') and time.time() < workload_end:
        for flow in FLOW_ASSIGNMENTS:
            flow_id = str(flow.get('flow_id'))
            active = flow_procs.get(flow_id)
            if active is not None and active.poll() is None:
                continue
            remaining = int(workload_end - time.time())
            if remaining > IPERF_OMIT_SECONDS + 1:
                flow_time = min(FLOW_TIME, remaining - IPERF_OMIT_SECONDS)
                attempt = flow_attempts.get(flow_id, 0)
                path = '/var/log/soak-tester/iperf3-' + flow_id + '-attempt-' + str(attempt).zfill(5) + '.json'
                receiver_ip = str(flow.get('receiver_private_ip') or '')
                if not receiver_ip:
                    continue
                command = iperf_command(flow, flow_time)
                IPERF_START_TIMES[path] = time.time()
                post('/events', event('info', 'network', 'iperf_started', {{
                    'flow_id': flow_id, 'attempt': attempt, 'command': command,
                    'started_at': IPERF_START_TIMES[path], 'offered_load': OFFERED_LOAD,
                }}))
                flow_procs[flow_id] = subprocess.Popen(command, stdout=open(path, 'w'), stderr=subprocess.STDOUT)
                IPERF_PROCESSES[path] = flow_procs[flow_id]
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
    if proc is not None and proc.poll() not in (None, 0) and not process_failure_reported:
        post('/events', event('error', ROLE, 'workload_failed', {{'returncode': proc.returncode}}))
        process_failure_reported = True
    if ROLE == 'compute' and proc is not None and proc.poll() is None:
        sample_system_metrics()
    if ROLE == 'storage' and proc is not None and proc.poll() is not None and not storage_parsed:
        parse_fio_output()
        storage_parsed = True
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
        post('/events', event('error', ROLE, 'workload_failed', {{'error': 'process exceeded finish deadline'}}))
        proc.terminate()
if ROLE == 'network-sender':
    parse_iperf3_output()
if ROLE == 'storage' and not storage_parsed:
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
    'delivery_failures': POST_FAILURES,
    'ts': time.time(),
}})
PY
chmod 0755 /usr/local/bin/soak-worker
nohup /usr/local/bin/soak-worker >/dev/console 2>&1 &
"""
    return script
