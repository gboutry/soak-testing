# Strict configuration cutover

The maintained examples use the new schema. Unknown fields at every level are
errors. Historical experiment files are maintained outside this repository.
Copy the meaningful settings into one of the seven maintained profiles; do not
feed historical payloads to the current runner.

| Previous setting | Current contract |
| --- | --- |
| `network.flow_source_hosts` | Removed with the integrated planner change; all selected hosts originate full-mesh flows. Remove the option; a former source subset has no equivalent in the maintained runner. |
| `capacity` | Rename to `targets`; conflicting aliases are no longer accepted. |
| `network.per_host_gbps` | Use `targets.network.gbps` with `scope: per_host`. |
| Disabled workload targets, flavors or settings | Remove them; use only `enabled: false`. |
| `flavors.api_worker` | Remove; API execution is local, not a guest worker. |
| `openstack.ssh_user` | Remove; the runner does not SSH to guests. Generated keys remain available for operator recovery. |
| `management.mode`, `controller_floating_ip`, `ssh_private_key_path` | Remove; use `resources.floating_ips.controller`; the runner generates its own key. |
| `resources.create_project`, `create_project_user`, `floating_ips.workers` | Remove; a dedicated project/user is always created; only the controller uses a floating IP. |
| `compute.cpu_resource_class`, `ram_resource_class`, `per_host_ram_gib` | Remove; these only served retired calibration. |
| `workloads.api.rally_times`, `rally_include_create_delete` | Remove; a positive-duration run ignored times and the task renderer did not select create/delete scenarios. |
| `workloads.api.run_from` | Remove; native API and Rally run on the runner host. |
| `workloads.network.protocol` | Defaults to `tcp`; the integrated `udp` path requires explicit receiver throughput/loss evidence and currently supports forward flows only. |
| `workloads.network.tool` | Remove; iperf3 is the supported tool. |
| `workloads.network.host_count` | Supply actual `hosts`, or let Nova discovery populate per-host planning. |
| `extra_client_args` | Use typed `connect_timeout_ms`, `pacing_timer_us`, and `offered_load`. Arbitrary overrides are rejected. |
| `monitoring`, `calibration` | Remove; these controls were unused or belonged to retired entry points. |
| `observability.enabled`, `prometheus.network.enabled`, `prometheus.labels` | Remove; use `observability.prometheus.enabled` and explicit device filters. |
| `run.leave_running`, `cleanup_on_success` | Replace with `run.keep_resources` (default false). |
| `run.warmup`, `cooldown`, `failure_budget`, `stop_on_error` | Remove; these fields did not control execution. Read the recorded measurement window. |

Native API uses `targets.api.ops_per_second` and `operation_mix`. Rally uses
explicit concurrency and duration, without a rate target or native
operation mix. Native settings on Rally and Rally settings on native are errors.
Storage targets are optional reporting thresholds; worker count and FIO shape
control offered load. Network aggregate targets use paired flows; per-host
network targets use full mesh, with at least two real hosts.

`target_gbps_per_flow` now means the total offered rate for a direction of one
flow, divided across its parallel streams. Previously iperf received that rate
for *each stream*. Use `offered_load: unlimited` for an unpaced saturation test
instead of `extra_client_args: [--bitrate, '0']`. A bidirectional flow offers the
configured rate in each direction. Its two receiver measurements remain separate;
sender and receiver counters for the same transfer are never added together.

`calibrate` and `scripts/run_network_tuning_matrix.py` are retired. Use the one
`inspect` / `plan` / `run` / `cleanup` lifecycle. Sequential campaigns remain in
`scripts/run_campaigns.py`; choose explicit run profiles for each experiment.

Flavors and image definitions are external provisioning inputs, not runner
configuration. Campaign flavor catalogs belong outside this repository. Normal
virtio flavors may enable `hw:vif_multiqueue_enabled`; direct SR-IOV flavors
must not carry virtio queue/ring assumptions. The runner does not create flavors
or change shared host networking, hugepages, PCI allocation or MTU policy.
Image build tools under `images/` and `scripts/images/` remain external prerequisites.

Additional explicitness rules:

- `management.allowed_cidrs` is required; there is no implicit world-open ingress.
- `compute_worker` and `workloads.compute.flavor_mix` are alternatives. Remove the
  redundant flavor when selecting a mix.
- `port_security_enabled` overrides require direct ports; normal ports use the
  supported Nova security-group path. Sender/receiver worker-rate budgets require
  per-host planning. `rwmixread` applies only to the mixed FIO profile.
- An external network is required only when creating a router; remove it when
  `create_router: false` and set the controller floating IP to false.
- Enabled Prometheus requires a URL and an explicit device regex. Missing auth
  credentials and contradictory TLS settings are errors.
- Campaign manifests reject unknown fields and unsafe/duplicate case names.

Reports now include independent outcomes and coverage. Consumers must read
`cleanup_status` / `cleanup.json`, not infer cleanup from process exit or a delete
request. A successful cleanup retry does not erase a workload failure. Historical
metrics without timestamps or measurement intervals cannot be promoted into valid
new-window evidence. The removed RX+TX combined host-counter summary must not be
used as application aggregate bandwidth.

Integrated guest settings remain opt-in: `guest_interrupt_coalesce_usecs` (0–1000),
TCP receiver `guest_large_receive_offload`, and `guest_tx_qdisc: mq_fq`. Their
native readbacks are checked before the shared start barrier and recorded in
repeatability evidence. The newer guarded `mq_fq` implementation is distinct from
the earlier rolled-back `fq`/`fq_codel` feature. `capture_receiver_intervals`
requests retained receiver interval events; `udp_datagram_size` applies only to
UDP. These settings change disposable guests, not shared host NIC configuration.
