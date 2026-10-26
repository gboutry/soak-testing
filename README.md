# soak-tester

`soak-tester` is an OpenStack-specific soak-load tool. It uses admin
credentials and explicit operator-provided config to create isolated run
artifacts, plan OpenStack resources, inspect cloud prerequisites, track owned
resources, collect workload evidence, and optionally query an existing
Prometheus HTTP API.

It is OpenStack-native and intentionally not Canonical-distribution-specific.
It does not assume Sunbeam, Juju, Charms, MicroStack, MAAS, Ceph, OVN, OVS,
Grafana, or a specific deployment layout.

## What It Does Not Create

`soak-tester` never creates Glance images, Nova flavors, external networks,
Cinder volume types, availability zones, host aggregates, Nova traits, Neutron
provider networks, Ceph pools, Prometheus, or Grafana.

## Prerequisites

- Python 3.11+
- OpenStack admin credentials in `clouds.yaml`
- Existing external network, image, flavors, quota policy, and optional volume type
- Worker image with Python 3, cloud-init and the enabled workload tools already installed: `stress-ng`, `fio`, `iperf3`
- Synchronized guest clocks; management ingress CIDRs explicitly configured
- Guest diagnostics (`ethtool`, `ip`, `lspci`, `mpstat`); missing network diagnostics trigger a guest package-install attempt
- Optional existing Prometheus server with the HTTP API enabled

## Example Commands

```bash
soak-tester inspect --os-cloud admin --config config.yaml
soak-tester plan --os-cloud admin --config config.yaml --target 80
soak-tester run --os-cloud admin --config config.yaml --target 80 --duration 8h
soak-tester status --run runs/soak-20260429-143000
soak-tester cleanup --run runs/soak-20260429-143000 --dry-run
soak-tester observability collect --run runs/soak-20260429-143000
```

## Maintained profiles

`configs/` contains seven independent examples: compute, network-virtio,
network-sriov, storage, api-native, api-rally and mixed. Replace the image,
flavor, external-network names and management ingress CIDR with your deployment
values. These resources must already exist. See [migration](docs/migration.md)
for the strict schema cutover. Campaign manifests, deployment-specific profiles,
and run evidence belong outside this repository; only reusable examples and test
fixtures are maintained here.

A complete network-only example is:

```yaml
openstack:
  external_network: external-network
  image: ubuntu-soak
management:
  allowed_cidrs: [198.51.100.10/32]
flavors:
  controller: soak-controller
  network_worker: soak-network
targets:
  network: {gbps: 20}
workloads:
  compute: {enabled: false}
  storage: {enabled: false}
  api: {enabled: false}
  network:
    enabled: true
    target_gbps_per_flow: 10
    parallel_streams_per_flow: 4
```

## Benchmark Targets

Compute, network, storage, and API load are planned as separate domains and can
run at the same time. Benchmark profiles use `targets` for requested reporting
values and `workloads` for concrete VM/workload shape. Compute can use an
explicit worker count; storage workers are configured directly and reported
against read/write/IOPS targets. Network can use either an aggregate target or
a host-aware full mesh:

```yaml
boot_concurrency: 12

targets:
  network:
    gbps: 200
    scope: per_host

workloads:
  network:
    topology: full_mesh
    flow_duration: 60s
    omit_seconds: 10
    target_gbps_per_flow: 5
    parallel_streams_per_flow: 16
    sender_worker_target_gbps: 20
    receiver_worker_target_gbps: 20
    zerocopy: true
    tcp_window: 16M
    vnic_type: normal
```

With `scope: per_host`, `--target 80` means `160 Gbit/s` per selected compute
host. If host names are not listed in config, `soak-tester` tries to discover
enabled hypervisors with admin Nova credentials and records them in the plan.

Set `workloads.network.vnic_type: direct` only for network-plane experiments
that should boot sender and receiver VMs from explicit Neutron ports with
`binding:vnic_type=direct`. For OVN/Geneve switchdev offload experiments, set
`hardware_offload_type: switchdev` and usually `port_security_enabled: false`
for the first validation pass. Other worker planes keep normal tenant-network
ports.

For high-throughput offloaded Geneve tests, set `resources.network_mtu` to the
tenant MTU you expect the cloud to support, for example `8942` when the
underlay and Geneve configuration support jumbo frames. If unset, Neutron's
deployment default is used, which may be a small value such as `1442` on a
1500-byte underlay. The matching network worker flavor must already exist and
should carry Nova extra specs for dedicated CPUs and PCI locality, for example:

```bash
openstack flavor set m1.benchmark-net-direct-16c \
  --property hw:cpu_policy=dedicated \
  --property hw:emulator_threads_policy=share \
  --property hw:pci_numa_affinity_policy=required \
  --property hw:numa_nodes=1
```

`hw:numa_nodes=1` means one guest NUMA cell; it does not select host NUMA node
1. Nova selects the host NUMA node local to the VF through
`hw:pci_numa_affinity_policy`.

For normal virtio ports, enable `hw:vif_multiqueue_enabled=true` on the network
worker flavor. Set `workloads.network.guest_combined_queues` to the desired
queue count to activate guest channels before traffic starts. The worker caps
this count at its online CPU count and the NIC's advertised maximum, reads back
the active channels with `ethtool -l`, and reports `network_queue_tuning`.
A failed setting or readback stops that worker. The default leaves queues
unchanged; this option requires normal ports and a `virtio_net` guest driver.
`workloads.network.guest_rx_ring_size` similarly requests an RX ring depth.
The guest checks the advertised maximum, adjusts RX only if necessary, and
verifies the result before starting traffic. The hypervisor must expose the
requested capacity; the worker refuses an unsupported size. This defaults to
leaving rings unchanged and does not configure TX ring depth.

For a fixed interrupt-coalescing comparison, set
`workloads.network.guest_interrupt_coalesce_usecs` (0–1000 microseconds).
This disables adaptive RX/TX coalescing and sets both RX/TX usec values on
the guest's workload interface before traffic starts. Frame thresholds stay
unchanged. The worker reads back all four settings and refuses unsupported
or mismatched results. The default makes no change. Larger delays can reduce
interrupt frequency at the cost of latency; benchmark both before adopting
a setting. This changes only disposable worker guests, not host PF settings.

For TCP receiver-only LRO comparisons, set
`workloads.network.guest_large_receive_offload` to `true` or `false`.
The default leaves offloads unchanged. Before setting LRO, the worker requires
verified disabled IPv4/IPv6 forwarding globally and on its workload interface.
It refuses a fixed or missing driver feature and verifies the native
`ethtool -k` readback before traffic. Sender guests and host PF settings are
unchanged. An advertised adjustable feature is not proof that enabling it
improves throughput; verify the real guest and benchmark separately.

For an opt-in multiqueue transmit pacing comparison, set
`workloads.network.guest_tx_qdisc: mq_fq`. Before starting network workloads,
the guest retains its multiqueue topology and configures FQ on every TX leaf.
For an automatic `mq 0:` root, the helper first assigns an explicit handle
before replacing leaves. It verifies the queue count and clsact state.
Unknown shapers, custom default layouts, incomplete queues and existing
filter policy are refused. Leaving the option unset makes no qdisc changes.

Set the benchmark network MTU only after verifying the underlying tunnel path;
an MTU of 9000 on a Geneve underlay does not mean 9000 inside the guest.

Per-host full-mesh planning originates traffic on every selected host.
`flow_source_hosts` is retired and rejected by the strict schema; directional
experiment profiles must be maintained outside this repository.

Calibration commands and the network matrix runner have been retired. Use
`inspect` and `plan` for prerequisites and resource sizing, and `run` with an
explicit profile for each measurement. Native API and Rally remain available
through `run`. Sequential campaign execution remains supported by
`scripts/run_campaigns.py`; it uses the native run lifecycle.

`offered_load: target` (default) divides each flow's rate across its parallel
TCP or UDP streams. `offered_load: unlimited` requests iperf's unlimited rate (`0`).
Targets size the plan; they do not prove achieved throughput. Application goodput uses
receiver results, counting each direction once. Host interface counters are a
separate observation, never added to application goodput.

For UDP, adapt a maintained network profile with `protocol: udp` and optionally
`udp_datagram_size` (for example 8192 bytes on a verified jumbo guest path).
UDP currently supports forward flows; use full-mesh host pairs for opposite
traffic directions. Its goodput requires explicit receiver results, including
packet counts, loss and jitter. Zero received throughput remains zero.

`capture_receiver_intervals: true` requests JSON receiver intervals. They are
retained as `iperf_receiver_intervals` events in `events.jsonl`; missing requested
intervals fail evidence validation. Rates continue to use complete receiver
summary observations, without summing sender and receiver counters.

The `mq_fq` option configures the guest discipline; it does not set socket pacing.
Arbitrary `extra_client_args`, including `--fq-rate`, remain unsupported by the
maintained runner.

## API Workload

The API plane supports two engines:

```yaml
workloads:
  api:
    engine: rally
    rally_mode: saturation
    rally_concurrency: 32
```

`engine: rally` runs Rally on the host where `soak-tester` is executed and
normalizes Rally results into `metrics.jsonl` and `final-report.*`. Rally is an
optional runtime dependency; install `rally` and `rally-openstack` on the
runner host before using this engine. `rally_mode: saturation` runs each
selected Rally scenario for the full soak duration at `rally_concurrency`
instead of targeting a configured ops/sec value. Saturation runs five scenarios
in parallel, each at the configured concurrency. `single_task` runs the same
scenarios sequentially inside one task, each for the requested duration; its API
phase can therefore outlast the guest workload window. Both modes use private
Rally databases under the run directory. The default Rally saturation
set uses token validation and list operations for Nova, Neutron, and Cinder. It
avoids create/delete scenarios in the full-soak API mix because Rally-managed
tenants need quota handling before those operations are reproducible.

## Execution and result boundaries

`run` checks access, images, flavors and their extra specs, member role, selected
volume type and the local Rally command before provisioning. It refuses a direct
network flavor carrying virtio multiqueue specs. Quota policy, hardware capacity,
underlay MTU, host tuning, clock synchronization and suitable image tools remain
operator prerequisites; `inspect` is not a capacity or data-path benchmark.
Recorded prerequisites distinguish these assumptions from checks.

The controller receives ready heartbeats before issuing one authenticated start
barrier. Guest FIO/stress durations use the requested duration, not the larger
agent provisioning budget. The runner allows 20 seconds for final guest evidence
after execution, then checks final worker completion and collection. Stop requests
and SIGINT/SIGTERM enter the same finalization path. In-flight SDK operations must
return before cleanup; SIGKILL cannot run Python finalizers.

`final-report.json` separates `workload_success`, `measurement_validity`,
`target_attainment` and `cleanup_status`. Valid observations require every
expected worker/flow/direction, actual placement, final worker completion and a
successful final collection. Guest delivery errors invalidate evidence. Failed
attempts remain visible even if later attempts succeed.

Only complete observations contained in the recorded measurement window enter
statistics. Network goodput is a sum of duration-weighted per-flow receiver
means; per-flow intervals and observed seconds expose coverage gaps. This is a
completed-attempt statistic, **not continuous sustained bandwidth**. Sender and
receiver values for one transfer are not added together. Bidirectional flows
count each receiver direction once; plans show total directional streams and
offered rate, including rounding and bidirectional expansion. Unlimited offered
load has no finite planned rate.

FIO percentiles remain per-worker observations. Global Rally latency quantiles
are unavailable when its report contains only scenario aggregates; the tool
retains per-scenario values rather than calculating quantiles of quantiles.
Rally rates use runner wall time including local setup and teardown. Missing
measurements are not proof of zero latency or successful operation.

## Artifact Layout

Runs are stored under `runs/<run-id>/` and include:

- `config.yaml`
- `plan.yaml`
- `state.json`
- `events.jsonl`
- `metrics.jsonl`
- `resources.json`
- `placement.json`, `flavors.json`, `prerequisites.json`, `runtime.json`
- `controller-status.json`, `cleanup.json`
- `ownership-intent.json` and private `controller-token` for recovery/start coordination
- `summary.json`
- `summary.md`
- `final-report.json`
- `final-report.md`
- `auth.json` with mode `0600`
- `ssh/id_ed25519` with mode `0600`
- `observability/prometheus-*` files when Prometheus collection is used

`summary.md` is the human-readable per-domain summary for compute, network,
storage, and API results. `summary.json` contains the same domain breakdown for
automation.

## Cleanup and recovery

Cleanup uses `resources.json` first, then run ownership metadata for discovery. It must only delete resources
owned by the run and must never delete pre-existing images, flavors, external
networks, or volume types.

Cleanup is repeatable and records both requests and live absence checks in
`cleanup.json`. An inaccessible inventory is a failed verification. Identity is
retained until tenant resources are absent. Only exact recorded IDs, the dedicated
run project/user, or explicit ownership metadata authorize discovery; matching
names alone do not. Lost identity-create responses can be recovered through a
persisted unique ownership description. Existing run directories/manifests are
never overwritten. `cleanup --dry-run` leaves state unchanged and manual cleanup
refuses an active run lock.

`run.keep_resources: true` explicitly retains resources even after a failure;
otherwise finalization attempts cleanup. Manual cleanup preserves the original
workload failure. Keep the private run directory for recovery. It contains auth,
SSH and Rally database credentials; share sanitized reports rather than the whole
directory.

Sequential campaigns require strict manifests with `timezone`, `os_cloud`,
`target`, a timezone-aware `latest_finish`, and `campaigns` entries containing
unique safe `name`, existing `config` path and positive `duration`. Paths are
relative to the invocation directory. They require `keep_resources: false`,
reserve ten minutes before the deadline for cleanup, invoke the native `run` and
`cleanup` commands, and stop before the next case if evidence or restoration
fails. Use a fresh campaign output directory; prior evidence is not overwritten.

See [verification and remaining gaps](docs/verification.md).

## Prometheus Observability

Prometheus is optional and evidence-only. `soak-tester` does not deploy or
manage Prometheus and does not expose Prometheus metrics for the MVP. Query
results are saved and correlated with workload artifacts. Reports distinguish
workload-observed goodput, guest counters, and Prometheus-observed host NIC
counters; they do not claim exact physical fabric throughput.

Enable collection only with an explicit `network.device_regex`. Placement hosts
must match the exporter instance labels. RX and TX remain separate sums across
selected host series at each timestamp, followed by sample summaries. Selecting
physical ports and avoiding stacked interfaces is the operator's responsibility;
these counters are not application goodput and do not prove fabric throughput.
No-data responses remain labelled no data. Prometheus queries and cloud traffic
were not exercised during this local simplification.
