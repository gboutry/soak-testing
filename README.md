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
soak-tester calibrate network --os-cloud admin --config config.yaml --duration 10m
soak-tester status --run runs/soak-20260429-143000
soak-tester cleanup --run runs/soak-20260429-143000 --dry-run
soak-tester observability collect --run runs/soak-20260429-143000
```

## Capacity Model

Compute, network, storage, and API load are planned as separate domains and can
run at the same time. Compute workers are sized from requested vCPU/RAM. Storage
workers are sized from configured fio worker count and reported against
read/write/IOPS targets. Network can use either an aggregate target or a
host-aware full mesh:

```yaml
boot_concurrency: 12

capacity:
  network:
    gbps: 200
    scope: per_host
    per_host_gbps: 200

workloads:
  network:
    topology: full_mesh
    target_gbps_per_flow: 5
    sender_worker_target_gbps: 20
    receiver_worker_target_gbps: 20
```

With `scope: per_host`, `--target 80` means `160 Gbit/s` per selected compute
host. If host names are not listed in config, `soak-tester` tries to discover
enabled hypervisors with admin Nova credentials and records them in the plan.

`soak-tester calibrate cloud` writes `runs/<run-id>/calibration.json` and
`runs/<run-id>/calibrated-capacity.yaml`. Compute capacity is discovered from
Placement inventories when available. On dedicated-CPU clouds, `PCPU` inventory
is selected when `VCPU` inventory is zero, while `capacity.compute.vcpus` remains
the guest-core count used by the planner:

```yaml
capacity:
  compute:
    vcpus: 1248
    ram_gib: 2304
    cpu_resource_class: PCPU
```

If benchmark flavors use huge pages and Placement exposes only generic
`MEMORY_MB`, set the intended per-host memory envelope explicitly:

```yaml
capacity:
  compute:
    per_host_ram_gib: 2048
```

Calibration will then write total compute RAM as
`per_host_ram_gib * discovered_compute_hosts`.

Network bandwidth, storage performance, and API throughput are not reliably
advertised by vanilla OpenStack APIs. The calibration report records configured
assumptions for those planes until workload probes replace them.

`soak-tester calibrate profile` turns inventory and optional validation-run
evidence into a tuned workload profile. It writes `calibration-profile.json`,
`calibration-profile.md`, and `tuned-config.yaml`. The profile distinguishes
OpenStack inventory from the exercisable workload envelope, applies a default
`0.95` pass ratio, models network capacity as per-host egress, and can derate
planes once when a validation run misses the requested target.

## Artifact Layout

Runs are stored under `runs/<run-id>/` and include:

- `config.yaml`
- `plan.yaml`
- `state.json`
- `events.jsonl`
- `metrics.jsonl`
- `resources.json`
- `placement.json`
- `final-report.json`
- `final-report.md`
- `auth.json` with mode `0600`
- `ssh/id_ed25519` with mode `0600`
- `observability/prometheus-*` files when Prometheus collection is used

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
