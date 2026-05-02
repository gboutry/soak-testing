#!/usr/bin/env bash
set -euo pipefail

RUN_DIR=${1:?usage: collect_network_diagnostics.sh RUN_DIR}
OUT_DIR="${RUN_DIR}/network-diagnostics"
mkdir -p "${OUT_DIR}"/{openstack,guests,hosts}

python3 - "$RUN_DIR" "$OUT_DIR/openstack/placement.env" > "$OUT_DIR/openstack/placement-summary.txt" <<'PY'
import json
import pathlib
import sys

run = pathlib.Path(sys.argv[1])
env = pathlib.Path(sys.argv[2])
placement = json.loads((run / "placement.json").read_text())
resources = json.loads((run / "resources.json").read_text())
state = json.loads((run / "state.json").read_text())

workers = {}
for item in placement:
    worker_id = item.get("worker_id")
    if worker_id:
        workers[worker_id] = item

senders = sorted(k for k in workers if k.startswith("network-sender-"))
receivers = sorted(k for k in workers if k.startswith("network-receiver-"))
if not senders or not receivers:
    raise SystemExit("no network sender/receiver placement found")

cross_pair = None
for sender_id in senders:
    for receiver_id in receivers:
        sender = workers[sender_id]
        receiver = workers[receiver_id]
        sender_host = sender.get("host") or sender.get("hypervisor_hostname")
        receiver_host = receiver.get("host") or receiver.get("hypervisor_hostname")
        if sender_host and receiver_host and sender_host != receiver_host:
            cross_pair = (sender_id, receiver_id, sender, receiver)
            break
    if cross_pair:
        break
if not cross_pair:
    cross_pair = (senders[0], receivers[0], workers[senders[0]], workers[receivers[0]])

src_id, dst_id, src, dst = cross_pair
src_host = src.get("host") or src.get("hypervisor_hostname")
dst_host = dst.get("host") or dst.get("hypervisor_hostname")
print(f"source_worker={src_id} source_host={src_host} source_ip={src.get('private_ip')}")
print(f"dest_worker={dst_id} dest_host={dst_host} dest_ip={dst.get('private_ip')}")
print(f"controller_url={state.get('controller_url')}")
print(f"same_host={src_host == dst_host}")
for worker_id in senders + receivers:
    item = workers[worker_id]
    print(
        f"{worker_id}: host={item.get('host')} hypervisor={item.get('hypervisor_hostname')} "
        f"ip={item.get('private_ip')} server={item.get('server_id')}"
    )

controller = (state.get("controller_url") or "").replace("http://", "").split(":", 1)[0]
if not controller:
    for resource in resources.get("resources", resources):
        if resource.get("type") == "floating_ip":
            controller = resource.get("name") or resource.get("id") or ""
            break
values = {
    "SRC_WORKER": src_id,
    "DST_WORKER": dst_id,
    "SRC_IP": src.get("private_ip") or "",
    "DST_IP": dst.get("private_ip") or "",
    "SRC_HOST": src_host or "",
    "DST_HOST": dst_host or "",
    "CONTROLLER_IP": controller,
}
env.write_text("\n".join(f"{k}={v!r}" for k, v in values.items()) + "\n")
PY

# shellcheck disable=SC1091
source "$OUT_DIR/openstack/placement.env"

machine_for_host() {
    case "$1" in
        pc8a-rb3-n1.maas|pc8a-rb3-n1) echo 0 ;;
        pc8a-rb3-n2.maas|pc8a-rb3-n2) echo 1 ;;
        pc8a-rb3-n3.maas|pc8a-rb3-n3) echo 2 ;;
        *) return 1 ;;
    esac
}

SRC_MACHINE=$(machine_for_host "$SRC_HOST")
DST_MACHINE=$(machine_for_host "$DST_HOST")
printf 'src_machine=%s dst_machine=%s\n' "$SRC_MACHINE" "$DST_MACHINE" > "$OUT_DIR/host-machines.txt"

KEY="${RUN_DIR}/ssh/id_ed25519"
SSH_BASE=(
    ssh -i "$KEY"
    -o BatchMode=yes
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/tmp/soak-diag-known-hosts
)
GUEST_SSH=(
    "${SSH_BASE[@]}"
    -o "ProxyCommand=ssh -i $KEY -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/tmp/soak-diag-known-hosts -W %h:%p ubuntu@$CONTROLLER_IP"
)

guest_iface() {
    local ip=$1 peer=$2
    "${GUEST_SSH[@]}" "ubuntu@$ip" "ip -o route get '$peer' | sed -n 's/.* dev \\([^ ]*\\).*/\\1/p' | head -1"
}

SRC_IFACE=$(guest_iface "$SRC_IP" "$DST_IP")
DST_IFACE=$(guest_iface "$DST_IP" "$SRC_IP")
printf 'src_iface=%s dst_iface=%s\n' "$SRC_IFACE" "$DST_IFACE" > "$OUT_DIR/guest-ifaces.txt"

collect_guest() {
    local label=$1 ip=$2 peer=$3 iface=$4
    "${GUEST_SSH[@]}" "ubuntu@$ip" "bash -s" <<GUEST
        echo "### guest $label \$(date -Is)"
        hostname || true
        ip -br addr || true
        ip route || true
        ethtool -l "$iface" || true
        ethtool -k "$iface" || true
        ethtool -S "$iface" 2>/dev/null | egrep -i 'queue|rx|tx|drop|err' | head -200 || true
        nproc || true
        lscpu | egrep 'CPU\(s\)|Thread|Core|Socket|NUMA' || true
        ping -M do -s 1472 -c 3 "$peer" || true
        ping -M do -s 8972 -c 3 "$peer" || true
GUEST
}

collect_host() {
    local machine=$1 label=$2
    /snap/bin/juju ssh -m openstack-machines "$machine" "sudo bash -s" > "$OUT_DIR/hosts/${label}.txt" 2>&1 <<'HOST'
set +e
echo "### host identity"
hostname
date -Is
uname -a
echo "### links"
ip -br link
ip -d link show
bridge link show
echo "### ovs topology"
ovs-vsctl show
ovs-vsctl get Open_vSwitch . other_config
ovs-vsctl list-br
ovs-vsctl list-ports br-int
ovs-vsctl list interface | egrep 'name|type|options|ofport|mtu|admin_state|link_state|statistics' -A2
ovn-sbctl show
ovn-nbctl show
echo "### datapath"
ovs-appctl dpctl/show
ovs-appctl dpctl/dump-flows -m | egrep 'tap|genev|vxlan|patch|br-|bond|eno|ens|enp|mlx|ct|recirc' | head -200
ovs-appctl dpctl/dump-flows -m type=offloaded | head -200
ovs-appctl dpctl/dump-flows -m type=non-offloaded | egrep 'tap|genev|vxlan|patch|br-|bond|eno|ens|enp|mlx|ct|recirc' | head -200
ovs-appctl dpctl/dump-flows -m | sort -k1 | head -50
ovs-appctl coverage/show
ovs-appctl dpif/show
echo "### tc offload"
for iface in $(ls /sys/class/net | egrep 'bond|eno|ens|enp|mlx|genev|vxlan|br-int|br-ex|tap'); do
    echo "=== tc $iface ingress ==="
    tc -s filter show dev "$iface" ingress 2>/dev/null || true
    echo "=== tc $iface egress ==="
    tc -s filter show dev "$iface" egress 2>/dev/null || true
done
echo "### cpu during iperf"
mpstat -P ALL 1 5
pidstat -t -p "$(pidof ovs-vswitchd)" 1 5
ps -eLo pid,tid,psr,pcpu,pmem,comm | egrep 'qemu|vhost|ksoftirqd|ovs|irq' | sort -k4 -nr | head -80
grep -E 'NET_RX|NET_TX' /proc/softirqs
cat /proc/interrupts | egrep -i 'mlx|ice|i40e|bnxt|ena|virtio|vhost|enp|ens|eno|bond'
echo "### qemu"
pgrep -af qemu-system
for p in $(pgrep -f qemu-system); do
    echo "=== qemu pid $p ==="
    taskset -cp "$p"
    ps -L -p "$p" -o pid,tid,psr,pcpu,comm | sort -k4 -nr | head -40
done
echo "### nova cpu config"
grep -R "cpu_shared_set\|cpu_dedicated_set\|reserved_host_cpus\|vcpu_pin_set" \
    /etc/nova/nova.conf \
    /var/snap/openstack-hypervisor/common/etc/nova/nova.conf \
    /var/snap/openstack-hypervisor/current/etc/nova/nova.conf \
    2>/dev/null | head -100
echo "### virsh"
virsh list --all
for d in $(virsh list --all --name | grep -v '^$'); do
    echo "=== domain $d ==="
    virsh dumpxml "$d" | sed -n '/<vcpu/,/<\/cputune>/p'
    virsh vcpupin "$d"
    virsh emulatorpin "$d"
    virsh iothreadinfo "$d" || true
done
echo "### nic candidates"
for iface in $(ls /sys/class/net | egrep 'bond|eno|ens|enp|mlx|genev|vxlan|br-int|br-ex|tap'); do
    echo "=== iface $iface ==="
    ip -s link show "$iface"
    ethtool -i "$iface" 2>/dev/null
    ethtool -l "$iface" 2>/dev/null
    ethtool -x "$iface" 2>/dev/null | head -120
    ethtool -k "$iface" 2>/dev/null
    ethtool -k "$iface" 2>/dev/null | egrep -i 'hw-tc-offload|ntuple|rx-checksumming|tx-checksumming|tcp-segmentation|generic-segmentation|generic-receive|large-receive|udp-fragmentation|tx-gre|tx-udp_tnl|rx-udp_tunnel' || true
    ethtool -S "$iface" 2>/dev/null | egrep -i 'rx|tx|drop|err|timeout|miss|discard|pause|xoff|xon|crc|fifo|buffer|queue' | head -300
    cat /sys/class/net/"$iface"/queues/rx-*/rps_cpus 2>/dev/null
    cat /sys/class/net/"$iface"/queues/tx-*/xps_cpus 2>/dev/null
done
echo "### devlink"
devlink dev show 2>/dev/null || true
for dev in $(devlink dev show 2>/dev/null | awk '{print $1}'); do
    echo "=== devlink $dev params ==="
    devlink dev param show "$dev" 2>/dev/null || true
done
echo "### irq affinity"
for f in /proc/irq/*/smp_affinity_list; do
    irq=$(echo "$f" | cut -d/ -f4)
    name=$(grep "^ *$irq:" /proc/interrupts)
    if echo "$name" | egrep -qi 'mlx|ice|i40e|bnxt|ena|enp|ens|eno|bond'; then
        echo "$f: $(cat "$f") :: $name"
    fi
done
echo "### mtu"
ip link show | egrep 'mtu|br-int|br-ex|genev|vxlan|bond|enp|ens|eno|mlx|tap'
echo "### conntrack"
conntrack -S || true
sysctl net.netfilter.nf_conntrack_count net.netfilter.nf_conntrack_max || true
ovs-appctl dpctl/dump-flows -m | egrep 'ct\(|ct_state|ct_zone|recirc' | head -100
HOST
}

echo "Starting host captures"
collect_host "$SRC_MACHINE" "source-host" &
HOST_SRC_PID=$!
collect_host "$DST_MACHINE" "dest-host" &
HOST_DST_PID=$!

echo "Starting explicit iperf captures"
{
    echo "### forward $(date -Is)"
    "${GUEST_SSH[@]}" "ubuntu@$DST_IP" "nohup iperf3 -s -p 5301 -1 >/tmp/soak-diag-iperf-forward-server.log 2>&1 &"
    sleep 2
    "${GUEST_SSH[@]}" "ubuntu@$SRC_IP" "iperf3 -c '$DST_IP' -p 5301 -P 16 -t 60 --get-server-output"
    echo "### reverse $(date -Is)"
    "${GUEST_SSH[@]}" "ubuntu@$DST_IP" "nohup iperf3 -s -p 5302 -1 >/tmp/soak-diag-iperf-reverse-server.log 2>&1 &"
    sleep 2
    "${GUEST_SSH[@]}" "ubuntu@$SRC_IP" "iperf3 -c '$DST_IP' -p 5302 -P 16 -R -t 60 --get-server-output"
} > "$OUT_DIR/guests/iperf-explicit.txt" 2>&1 || true

collect_guest source "$SRC_IP" "$DST_IP" "$SRC_IFACE" > "$OUT_DIR/guests/source.txt" 2>&1 || true
collect_guest dest "$DST_IP" "$SRC_IP" "$DST_IFACE" > "$OUT_DIR/guests/dest.txt" 2>&1 || true

wait "$HOST_SRC_PID" || true
wait "$HOST_DST_PID" || true

echo "network diagnostics written to $OUT_DIR"
