from __future__ import annotations

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
    return f"""#cloud-config
packages:
  - python3
  - curl
  - stress-ng
  - fio
  - iperf3
write_files:
  - path: /usr/local/bin/soak-worker
    permissions: '0755'
    content: |
      #!/usr/bin/env python3
      import json, os, subprocess, time, urllib.request
      ROLE = {role!r}
      WORKER_ID = {worker_id!r}
      CONTROLLER = {controller_url!r}
      DURATION = {duration_seconds}
      PEERS = {peers!r}
      def post(path, payload):
          data = json.dumps(payload).encode()
          req = urllib.request.Request(CONTROLLER + path, data=data, headers={{'content-type': 'application/json'}})
          try:
              urllib.request.urlopen(req, timeout=5).read()
          except Exception:
              pass
      def start_workload():
          if ROLE == 'compute':
              return subprocess.Popen(['stress-ng','--cpu','0','--cpu-load',{str(config.workloads.compute.cpu_load_percent)!r},'--vm','1','--vm-bytes',{str(config.workloads.compute.memory_load_percent) + '%'!r},'--timeout',str(DURATION)])
          if ROLE == 'network-receiver':
              return subprocess.Popen(['iperf3','--server'])
          if ROLE == 'network-sender' and PEERS:
              return subprocess.Popen(['iperf3','--client',PEERS[0],'--time',{str(int(config.workloads.network.flow_duration.rstrip('smhd')))!r},'--parallel',{str(config.workloads.network.parallel_streams_per_flow)!r},'--bitrate',{str(config.workloads.network.target_gbps_per_flow) + 'G'!r},'--json'])
          if ROLE == 'storage':
              device = next((p for p in ['/dev/vdb','/dev/disk/by-id/virtio-soak'] if os.path.exists(p)), '/dev/vdb')
              return subprocess.Popen(['fio','--name=workload','--ioengine=libaio','--direct=1','--time_based=1','--runtime',str(DURATION),'--group_reporting=1','--iodepth',{str(config.workloads.storage.iodepth)!r},'--numjobs',{str(config.workloads.storage.numjobs)!r},'--bs',{config.workloads.storage.block_size!r},'--rw=randrw','--rwmixread',{str(config.workloads.storage.rwmixread)!r},'--filename',device,'--output-format=json'])
          return None
      proc = start_workload()
      end = time.time() + DURATION
      while time.time() < end:
          post('/heartbeat', {{'worker_id': WORKER_ID, 'role': ROLE, 'status': None if proc is None else proc.poll(), 'ts': time.time()}})
          time.sleep(5)
runcmd:
  - mkdir -p /var/log/soak-tester
  - nohup /usr/local/bin/soak-worker >/var/log/soak-tester/worker.log 2>&1 &
"""
