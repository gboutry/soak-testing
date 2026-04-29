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
soak-tester status --run runs/soak-20260429-143000
soak-tester cleanup --run runs/soak-20260429-143000 --dry-run
soak-tester observability collect --run runs/soak-20260429-143000
```

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
