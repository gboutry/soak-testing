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
- Worker image capable of installing `stress-ng`, `fio`, and `iperf3` with cloud-init
- Optional existing Prometheus server with the HTTP API enabled

## Example Commands

```bash
soak-tester inspect --os-cloud admin --config config.yaml
soak-tester plan --os-cloud admin --config config.yaml --target 80
soak-tester run --os-cloud admin --config config.yaml --target 80 --duration 8h
soak-tester calibrate cloud --os-cloud admin --config config.yaml
soak-tester calibrate profile --os-cloud admin --config config.yaml --target 80
soak-tester calibrate api --os-cloud admin --config config.yaml --duration 10m
soak-tester calibrate network --os-cloud admin --config config.yaml --duration 10m
soak-tester status --run runs/soak-20260429-143000
soak-tester cleanup --run runs/soak-20260429-143000 --dry-run
soak-tester observability collect --run runs/soak-20260429-143000
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
    per_host_gbps: 200

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
```

With `scope: per_host`, `--target 80` means `160 Gbit/s` per selected compute
host. If host names are not listed in config, `soak-tester` tries to discover
enabled hypervisors with admin Nova credentials and records them in the plan.

For cross-host east-west tuning, use the matrix runner to create reproducible
config variants and repeated network-only calibration runs:

```bash
PYTHONPATH=src scripts/run_network_tuning_matrix.py \
  --os-cloud admin \
  --base-config configs/network-diagnose-2host-8vm.yaml \
  --matrix configs/network-tuning-matrix.yaml \
  --duration 3m \
  --repetitions 3 \
  --command soak-tester
```

The runner writes generated configs plus `network-tuning-results.json` and
`network-tuning-results.md`. It reports workload-observed iperf goodput and
cross-host flow validation; it does not prove physical fabric throughput.

`soak-tester calibrate cloud` writes `runs/<run-id>/calibration.json` and
`runs/<run-id>/calibrated-capacity.yaml`. Compute capacity is discovered from
Placement inventories when available. On dedicated-CPU clouds, `PCPU` inventory
is selected when `VCPU` inventory is zero, while `targets.compute.vcpus` remains
the guest-core count used by the planner:

```yaml
targets:
  compute:
    vcpus: 1248
    ram_gib: 2304
    cpu_resource_class: PCPU
```

If benchmark flavors use huge pages and Placement exposes only generic
`MEMORY_MB`, set the intended per-host memory envelope explicitly:

```yaml
targets:
  compute:
    per_host_ram_gib: 2048
```

Calibration will then write total compute RAM as
`per_host_ram_gib * discovered_compute_hosts`.

Network bandwidth, storage performance, and API throughput are not reliably
advertised by vanilla OpenStack APIs. Calibration commands write workload
evidence and an operator-ready `recommended-profile.yaml`. That YAML file is a
normal benchmark profile: review it, rename or commit it, then pass it directly
to `soak-tester plan` and `soak-tester run`.

`soak-tester calibrate profile` turns inventory and optional validation-run
evidence into a recommended benchmark profile. It writes
`calibration-profile.json`, `calibration-profile.md`,
`recommended-profile.md`, and `recommended-profile.yaml`. The profile
distinguishes OpenStack inventory from the exercisable workload envelope,
applies a default `0.95` pass ratio, models network capacity as per-host
egress, and can derate planes once when a validation run misses the requested
target.

## API Workload

The API plane supports two engines:

```yaml
workloads:
  api:
    engine: rally
    rally_mode: saturation
    rally_concurrency: 32
    rally_times: null
    rally_include_create_delete: true
```

`engine: rally` runs Rally on the host where `soak-tester` is executed and
normalizes Rally results into `metrics.jsonl` and `final-report.*`. Rally is an
optional runtime dependency; install `rally` and `rally-openstack` on the
runner host before using this engine. `rally_mode: saturation` runs each
selected Rally scenario for the full soak duration at `rally_concurrency`
instead of targeting a configured ops/sec value. The default Rally saturation
set uses token validation and list operations for Nova, Neutron, and Cinder. It
avoids create/delete scenarios in the full-soak API mix because Rally-managed
tenants need quota handling before those operations are reproducible.

## Artifact Layout

Runs are stored under `runs/<run-id>/` and include:

- `config.yaml`
- `plan.yaml`
- `state.json`
- `events.jsonl`
- `metrics.jsonl`
- `resources.json`
- `placement.json`
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

## Cleanup Warning

Cleanup uses `resources.json` first, then run ownership metadata where a live
OpenStack cleanup implementation is wired in. It must only delete resources
owned by the run and must never delete pre-existing images, flavors, external
networks, or volume types.

## Prometheus Observability

Prometheus is optional and evidence-only. `soak-tester` does not deploy or
manage Prometheus and does not expose Prometheus metrics for the MVP. Query
results are saved and correlated with workload artifacts. Reports distinguish
workload-observed goodput, guest counters, and Prometheus-observed host NIC
counters; they do not claim exact physical fabric throughput.
