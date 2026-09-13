#!/usr/bin/env bash
#
# Two real TrenchChat backends, each behind its own masquerading NAT, upgrading
# to a direct IP session across them.
#
# The scenario suite runs every tester on one host, where a candidate is a
# local address and the punch is barely a punch. This is the other case: two
# peers that can only reach each other through address translation, brokered by
# a Reticulum hub both can reach outbound and neither can be reached at.
#
# Topology, four namespaces, a bridge in the root namespace, and the hub on it:
#
#   tcn-a              tcn-nat-a          tcn-nat-b              tcn-b
#   10.1.0.2/24 ------ 10.1.0.1/24        10.2.0.1/24 ---------- 10.2.0.2/24
#                      198.51.100.1 --- [ tcn-br0 ] --- 198.51.100.2
#                                            |
#                                    198.51.100.254 (root namespace)
#                                    hub.py, and this script's driver
#
# The root namespace routes into both LANs so the driver can reach each
# tester's API, and each NAT accepts inbound TCP to that API port only. Nothing
# routes between the two LANs, so a peer's lan candidate is genuinely
# unreachable from the other side and the punch has to do the work.
#
# Six variants, and `cone` alone is recorded rather than judged:
#
#   one_nat    A is behind a masquerading NAT and B is on the segment with the
#              hub, which is the shape of every pair where one side is
#              reachable: a public host, a forwarded port, a tailnet. The punch
#              must succeed, and the session must come up across real address
#              translation.
#   cone       Both sides behind their own masquerading NAT, both port
#              restricted. Recorded rather than judged: see the finding below.
#   cone_helper  The cone pair again, with a third member on the hub's segment
#              that both can reach. A and B each come up direct with C first,
#              and C's hello tells each of them the address it arrived from, so
#              a peer that could name nothing now has something to name. The
#              pair must then come up direct: one reachable member is all the
#              design asks for.
#   cone_stun  The cone pair again, with no helper and a STUN responder in the
#              root namespace that both can reach outbound. It must stay on
#              Reticulum while the echo is off, with both sides recording
#              no_public_address, and come up direct once both users switch it
#              on: the pair no member can help, and the one thing left that
#              helps it.
#   symmetric  Both sides behind `fully-random` masquerading, so no candidate
#              can predict the external port. The pair must stay on Reticulum,
#              which is the deliberate non-fix the plan records.
#   ipv6_fw    No NAT at all: both peers hold a global IPv6 address and sit
#              behind their own stateful IPv6 firewall, which accepts
#              established and related and drops new inbound. Inbound IPv4 UDP
#              is dropped in both namespaces, so the only path between them is
#              the IPv6 one, and the hub they both signal through is reached
#              over IPv6 as well. The punch must succeed with nothing observed
#              and nobody helping: each side already knows the address it will
#              be reached at, and all the firewall asks is that it send first.
#
# The finding the cone variant records: a node knows its own addresses and
# nothing about the address translation in front of them, so two peers that are
# both behind a NAT with no router mapping have nothing to name each other by.
# Every probe goes to an unroutable lan candidate, nobody's probe arrives, and
# nobody can observe an address to report back. The design's answers to that
# are a router mapping (UPnP-IGD or NAT-PMP, which no namespace here speaks) or
# an address a peer observed in an earlier exchange, and with neither the pair
# stays on Reticulum and says so, as no_public_address rather than punch_failed
# because the two are a different problem for a user. Phase 0's spike punched
# this case only because the harness told each side the other's public address.
#
# Needs root with CAP_NET_ADMIN, iproute2 and nftables. Linux only.
#
#   sudo ./nat_harness.sh                     # every variant
#   sudo ./nat_harness.sh one_nat
#   sudo ./nat_harness.sh cone_stun
#   sudo ./nat_harness.sh ipv6_fw
#   sudo PYTHON=/path/to/.venv/bin/python ./nat_harness.sh symmetric

set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
WORK="${WORK:-/tmp/trenchchat-nat-harness}"
UPGRADE_TIMEOUT="${UPGRADE_TIMEOUT:-180}"
TOKEN="nat-harness-token"

NS_A=tcn-a
NS_NAT_A=tcn-nat-a
NS_NAT_B=tcn-nat-b
NS_B=tcn-b
NS_C=tcn-c
BRIDGE=tcn-br0
LAN_A=10.1.0
LAN_B=10.2.0
WAN=198.51.100
WAN6=2001:db8:ff
HUB_IP="$WAN.254"
HUB6="$WAN6::254"
# Where the hub listens and where the testers dial it, which the IPv6 variant
# moves onto IPv6 so that nothing in the run depends on IPv4 but the driver.
HUB_HOST="$HUB_IP"
HUB_PORT=41101
API_A=8901
API_B=8902
API_C=8903
# Where the address echo answers, in the root namespace on the bridge: both
# NATed peers reach it outbound and neither can be reached at.
STUN_PORT=3478
# Where B is, which the one_nat variant moves onto the segment with the hub.
HOST_B="$LAN_B.2"

hub_pid=""
stun_pid=""
worker_a_pid=""
worker_b_pid=""
worker_c_pid=""

fail() { echo "nat_harness.sh: $*" >&2; exit 1; }

preflight() {
    [ "$(id -u)" = "0" ] || fail "must run as root (id -u is $(id -u))"
    command -v ip  >/dev/null || fail "iproute2 is missing: install it for the ip command"
    command -v nft >/dev/null || fail "nftables is missing: install it for the nft command"
    [ -x "$PYTHON" ] || fail "no python at $PYTHON; set PYTHON to the venv's interpreter"
    ip netns add tcn-preflight 2>/dev/null \
        || fail "ip netns add refused: the container lacks CAP_NET_ADMIN or /var/run/netns"
    ip netns del tcn-preflight
    ip link add tcn-probe0 type veth peer name tcn-probe1 2>/dev/null \
        || fail "ip link add veth refused: the container lacks CAP_NET_ADMIN"
    ip link del tcn-probe0
}

# The IPv6 variant needs a kernel with IPv6 in it. A kernel booted with
# ipv6.disable=1 has no /proc/sys/net/ipv6 and refuses every socket of the
# family, and no amount of namespace setup works around that. Asking for the
# variant by name on such a kernel is an error; a run of everything says so and
# carries on, because the other four still mean something.
have_ipv6() {
    [ -d /proc/sys/net/ipv6 ]
}

require_ipv6() {
    have_ipv6 || fail \
        "this kernel has no IPv6 (booted with ipv6.disable=1?), so ipv6_fw cannot run"
}

stop_processes() {
    for pid in "$worker_a_pid" "$worker_b_pid" "$worker_c_pid" "$hub_pid" \
               "$stun_pid"; do
        [ -n "$pid" ] && kill "$pid" 2>/dev/null
    done
    wait 2>/dev/null || true
    worker_a_pid=""; worker_b_pid=""; worker_c_pid=""; hub_pid=""; stun_pid=""
}

teardown() {
    stop_processes
    for ns in "$NS_A" "$NS_NAT_A" "$NS_NAT_B" "$NS_B" "$NS_C"; do
        ip netns del "$ns" 2>/dev/null || true
    done
    for leg in tcn-wa-br tcn-wb-br tcn-a-br tcn-b-br tcn-c-br; do
        ip link del "$leg" 2>/dev/null || true
    done
    ip link del "$BRIDGE" 2>/dev/null || true
    ip route del "$LAN_A.0/24" 2>/dev/null || true
    ip route del "$LAN_B.0/24" 2>/dev/null || true
}

# The stateful drop is not decoration. A probe that reaches a NAT before that
# NAT has made its own outbound mapping leaves an unreplied conntrack entry
# holding the exact tuple the mapping wants; Linux then remaps to a random
# external port and no ordering of probes recovers. A consumer NAT drops that
# packet instead, which is what makes this a port-restricted cone NAT.
masquerade() {
    local ns="$1" wan="$2" mode="$3" api="$4" flags=""
    [ "$mode" = "symmetric" ] && flags=" fully-random"
    ip netns exec "$ns" nft add table ip tcn
    ip netns exec "$ns" nft "add chain ip tcn post { type nat hook postrouting priority 100 ; }"
    ip netns exec "$ns" nft "add rule ip tcn post oifname \"$wan\" masquerade$flags"
    ip netns exec "$ns" nft \
        "add chain ip tcn input { type filter hook input priority 0 ; policy accept ; }"
    ip netns exec "$ns" nft \
        "add chain ip tcn forward { type filter hook forward priority 0 ; policy accept ; }"
    # The driver's own reach, and nothing else: one TCP port, never UDP.
    ip netns exec "$ns" nft \
        "add rule ip tcn forward iifname \"$wan\" tcp dport $api accept"
    ip netns exec "$ns" nft "add rule ip tcn input iifname \"$wan\" ct state new,invalid drop"
    ip netns exec "$ns" nft "add rule ip tcn forward iifname \"$wan\" ct state new,invalid drop"
}

# A stateful IPv6 firewall, the one an ordinary home router runs: everything
# established or related comes back in, anything new from outside does not, and
# ICMPv6 is let through because neighbour discovery is ICMPv6 and a segment
# without it has no addresses to talk to. IPv4 UDP is dropped outright, so the
# punch this variant measures is the IPv6 one and cannot quietly be an IPv4 one.
ipv6_firewall() {
    local ns="$1"
    ip netns exec "$ns" nft add table ip6 tcn6
    ip netns exec "$ns" nft \
        "add chain ip6 tcn6 input { type filter hook input priority 0 ; policy accept ; }"
    ip netns exec "$ns" nft "add rule ip6 tcn6 input iifname \"lo\" accept"
    ip netns exec "$ns" nft "add rule ip6 tcn6 input meta l4proto ipv6-icmp accept"
    ip netns exec "$ns" nft "add rule ip6 tcn6 input ct state established,related accept"
    ip netns exec "$ns" nft "add rule ip6 tcn6 input ct state new,invalid drop"
    ip netns exec "$ns" nft add table ip tcn
    ip netns exec "$ns" nft \
        "add chain ip tcn input { type filter hook input priority 0 ; policy accept ; }"
    ip netns exec "$ns" nft "add rule ip tcn input meta l4proto udp drop"
}

# One namespace on the bridge with both families and no translation in front.
bridge_peer() {
    local ns="$1" leg="$2" four="$3" six="$4"
    ip link add "$leg" type veth peer name "$leg-br"
    ip link set "$leg" netns "$ns"
    ip link set "$leg-br" master "$BRIDGE"
    ip link set "$leg-br" up
    ip netns exec "$ns" ip addr add "$four/24" dev "$leg"
    # nodad, because an address still proving itself is tentative, and a
    # tentative address is one this node deliberately does not offer a peer.
    ip netns exec "$ns" ip -6 addr add "$six/64" dev "$leg" nodad
    ip netns exec "$ns" ip link set "$leg" up
}

# Whether this variant is the IPv6 one, which has no NAT anywhere in it.
is_ipv6() {
    [ "$1" = "ipv6_fw" ]
}

# Whether this variant puts a NAT in front of B as well as in front of A.
nat_in_front_of_b() {
    [ "$1" != "one_nat" ]
}

# Whether this variant runs a third member on the hub's segment, reachable by
# both NATed peers and therefore able to tell each of them its own address.
has_helper() {
    [ "$1" = "cone_helper" ]
}

# Whether this variant runs an address echo, which is a server outside both
# networks and the only thing left for a pair no member can name.
has_stun() {
    [ "$1" = "cone_stun" ]
}

setup() {
    local mode="$1"
    teardown
    for ns in "$NS_A" "$NS_NAT_A" "$NS_NAT_B" "$NS_B" "$NS_C"; do
        ip netns add "$ns"
        ip netns exec "$ns" ip link set lo up
    done

    ip link add "$BRIDGE" type bridge
    ip addr add "$HUB_IP/24" dev "$BRIDGE"
    ip link set "$BRIDGE" up

    if is_ipv6 "$mode"; then
        HUB_HOST="$HUB6"
        HOST_A="$WAN.5"
        HOST_B="$WAN.6"
        ip -6 addr add "$HUB6/64" dev "$BRIDGE" nodad
        bridge_peer "$NS_A" tcn-a0 "$HOST_A" "$WAN6::5"
        bridge_peer "$NS_B" tcn-b0 "$HOST_B" "$WAN6::6"
        ipv6_firewall "$NS_A"
        ipv6_firewall "$NS_B"
        return
    fi
    HUB_HOST="$HUB_IP"
    HOST_A="$LAN_A.2"

    # A is always behind a NAT: its LAN, and its NAT's leg on the bridge.
    ip link add tcn-a0 type veth peer name tcn-a1
    ip link add tcn-wa type veth peer name tcn-wa-br
    ip link set tcn-a0 netns "$NS_A"
    ip link set tcn-a1 netns "$NS_NAT_A"
    ip link set tcn-wa netns "$NS_NAT_A"
    ip link set tcn-wa-br master "$BRIDGE"
    ip link set tcn-wa-br up

    ip netns exec "$NS_A" ip addr add "$LAN_A.2/24" dev tcn-a0
    ip netns exec "$NS_A" ip link set tcn-a0 up
    ip netns exec "$NS_A" ip route add default via "$LAN_A.1"

    ip netns exec "$NS_NAT_A" ip addr add "$LAN_A.1/24" dev tcn-a1
    ip netns exec "$NS_NAT_A" ip addr add "$WAN.1/24" dev tcn-wa
    ip netns exec "$NS_NAT_A" ip link set tcn-a1 up
    ip netns exec "$NS_NAT_A" ip link set tcn-wa up
    ip netns exec "$NS_NAT_A" sysctl -qw net.ipv4.ip_forward=1

    # The root namespace reaches each tester's API, and nothing reaches across:
    # a NAT here knows no route to the other side's LAN.
    ip route add "$LAN_A.0/24" via "$WAN.1" dev "$BRIDGE"
    masquerade "$NS_NAT_A" tcn-wa "$mode" "$API_A"

    if nat_in_front_of_b "$mode"; then
        HOST_B="$LAN_B.2"
        ip link add tcn-b0 type veth peer name tcn-b1
        ip link add tcn-wb type veth peer name tcn-wb-br
        ip link set tcn-b0 netns "$NS_B"
        ip link set tcn-b1 netns "$NS_NAT_B"
        ip link set tcn-wb netns "$NS_NAT_B"
        ip link set tcn-wb-br master "$BRIDGE"
        ip link set tcn-wb-br up

        ip netns exec "$NS_B" ip addr add "$LAN_B.2/24" dev tcn-b0
        ip netns exec "$NS_B" ip link set tcn-b0 up
        ip netns exec "$NS_B" ip route add default via "$LAN_B.1"

        ip netns exec "$NS_NAT_B" ip addr add "$LAN_B.1/24" dev tcn-b1
        ip netns exec "$NS_NAT_B" ip addr add "$WAN.2/24" dev tcn-wb
        ip netns exec "$NS_NAT_B" ip link set tcn-b1 up
        ip netns exec "$NS_NAT_B" ip link set tcn-wb up
        ip netns exec "$NS_NAT_B" sysctl -qw net.ipv4.ip_forward=1
        ip route add "$LAN_B.0/24" via "$WAN.2" dev "$BRIDGE"
        masquerade "$NS_NAT_B" tcn-wb "$mode" "$API_B"
    else
        # B sits on the segment the hub is on, reachable by anything there:
        # a public host, a forwarded port, or a tailnet address.
        HOST_B="$WAN.3"
        ip link add tcn-b0 type veth peer name tcn-b-br
        ip link set tcn-b0 netns "$NS_B"
        ip link set tcn-b-br master "$BRIDGE"
        ip link set tcn-b-br up
        ip netns exec "$NS_B" ip addr add "$WAN.3/24" dev tcn-b0
        ip netns exec "$NS_B" ip link set tcn-b0 up
    fi

    if has_helper "$mode"; then
        # C is the member both NATed peers can reach, which is all the design
        # asks of an observer: no service, no address of ours, just a member.
        ip link add tcn-c0 type veth peer name tcn-c-br
        ip link set tcn-c0 netns "$NS_C"
        ip link set tcn-c-br master "$BRIDGE"
        ip link set tcn-c-br up
        ip netns exec "$NS_C" ip addr add "$WAN.4/24" dev tcn-c0
        ip netns exec "$NS_C" ip link set tcn-c0 up
    fi
}

start_hub() {
    mkdir -p "$WORK/hub"
    "$PYTHON" "$HERE/hub.py" "$WORK/hub" "$HUB_PORT" trenchchat_nat_hub "$HUB_HOST" \
        > "$WORK/hub.log" 2>&1 &
    hub_pid=$!
    for _ in $(seq 1 30); do
        ss -ltn 2>/dev/null | grep -q ":$HUB_PORT" && return 0
        sleep 0.5
    done
    fail "the hub never opened its listener; see $WORK/hub.log"
}

start_stun() {
    "$PYTHON" "$HERE/stun_responder.py" "$HUB_IP" "$STUN_PORT" \
        > "$WORK/stun.log" 2>&1 &
    stun_pid=$!
    for _ in $(seq 1 30); do
        grep -q "listening on" "$WORK/stun.log" 2>/dev/null && return 0
        sleep 0.5
    done
    fail "the address echo never came up; see $WORK/stun.log"
}

start_worker() {
    local ns="$1" tag="$2" data="$3" api="$4" lan="$5" instance="$6"
    rm -rf "$data"
    mkdir -p "$data"
    # The API answers 421 to a Host header it does not recognise, and a
    # namespace address is not the loopback, so it is declared here the same
    # way the launcher declares a Tailscale name.
    ip netns exec "$ns" env PYTHONPATH="$REPO_ROOT:$HERE" \
        "$PYTHON" "$HERE/worker.py" "$tag" "$data" "$tag" client 0 \
        "$HUB_HOST" "$HUB_PORT" "$api" "$instance" false 0 "$TOKEN" "$lan" \
        "http://$lan:$api" \
        > "$WORK/$tag.log" 2>&1 &
}

drive() {
    local mode="$1"
    "$PYTHON" - "$mode" "$WORK" "$REPO_ROOT" "$HOST_A" "$HOST_B" <<'PYTHON'
import sys
import time
from pathlib import Path

MODE, WORK, REPO_ROOT, HOST_A, HOST_B = sys.argv[1:6]
sys.path.insert(0, f"{REPO_ROOT}/devtools/testenv/scenarios")
sys.path.insert(0, f"{REPO_ROOT}/devtools/testenv")
sys.path.insert(0, REPO_ROOT)

from asserts import ScenarioFailure, set_timeout_scale, wait_until  # noqa: E402
from flows import invite_only_channel  # noqa: E402
from peer import Peer  # noqa: E402
from scen_upgrade import _upgraded  # noqa: E402
from trenchchat.network.base import PATH_DIRECT  # noqa: E402

set_timeout_scale(1.5)
a = Peer("A", 8901, "nat-harness-token", host=HOST_A)
b = Peer("B", 8902, "nat-harness-token", host=HOST_B)
for peer in (a, b):
    deadline = time.time() + 120
    while time.time() < deadline and not peer.alive():
        time.sleep(1.0)
    if not peer.alive():
        raise SystemExit(f"{peer.tag}'s API never came up")

channel = invite_only_channel(a, [b], "nat-room")
started = time.time()
result = {"mode": MODE, "channel": channel[:12]}
try:
    wait_until(lambda: _upgraded(a, b), "A to open a session with B", 120.0)
    wait_until(lambda: _upgraded(b, a), "B to hold the far side", 60.0)
    wait_until(lambda: a.member_path(channel, b.hash) == PATH_DIRECT,
               "A's roster to show B as direct", 60.0)
    result["upgraded"] = True
    result["seconds"] = round(time.time() - started, 1)
except (ScenarioFailure, TimeoutError) as e:
    result["upgraded"] = False
    result["seconds"] = round(time.time() - started, 1)
    result["detail"] = str(e)[:200]
    result["failure"] = a.upgrade_failure(b.hash)

a.send(channel, "nat-harness-message")
carried = False
for _ in range(60):
    if "nat-harness-message" in b.contents(channel):
        carried = True
        break
    time.sleep(1.0)
result["message_carried"] = carried
Path(WORK, f"{MODE}.result.json").write_text(repr(result))
print(f"  {result}")
raise SystemExit(0 if result["upgraded"] else 1)
PYTHON
}

# The three-member case: A and B behind their own NATs, C where both can reach
# it. What it measures is how far one reachable member gets a pair that could
# name nothing before: each of them learns its own translated address from C's
# hello, offers it to the other, and the punch either completes or says where
# it stopped.
drive_helper() {
    "$PYTHON" - "$WORK" "$REPO_ROOT" "$LAN_A.2" "$LAN_B.2" "$WAN.4" <<'PYTHON'
import sys
import time
from pathlib import Path

WORK, REPO_ROOT, HOST_A, HOST_B, HOST_C = sys.argv[1:6]
sys.path.insert(0, f"{REPO_ROOT}/devtools/testenv/scenarios")
sys.path.insert(0, f"{REPO_ROOT}/devtools/testenv")
sys.path.insert(0, REPO_ROOT)

from asserts import ScenarioFailure, set_timeout_scale, wait_until  # noqa: E402
from flows import invite_only_channel  # noqa: E402
from peer import Peer  # noqa: E402
from scen_upgrade import _upgraded  # noqa: E402
from trenchchat.core.storage import Storage  # noqa: E402

set_timeout_scale(1.5)
a = Peer("A", 8901, "nat-harness-token", host=HOST_A)
b = Peer("B", 8902, "nat-harness-token", host=HOST_B)
c = Peer("C", 8903, "nat-harness-token", host=HOST_C)
for peer in (a, b, c):
    deadline = time.time() + 120
    while time.time() < deadline and not peer.alive():
        time.sleep(1.0)
    if not peer.alive():
        raise SystemExit(f"{peer.tag}'s API never came up")


def observed_self(data_dir: str) -> list:
    """What a tester has been told about its own address, read from its store."""
    store = Storage(db_path=Path(data_dir) / "storage.db")
    try:
        return store.get_upgrade_addresses("self")
    finally:
        store.close()


channel = invite_only_channel(c, [a, b], "nat-helper-room")
result = {"mode": "cone_helper", "channel": channel[:12]}
started = time.time()
try:
    wait_until(lambda: _upgraded(a, c), "A to come up direct with C", 150.0)
    wait_until(lambda: _upgraded(b, c), "B to come up direct with C", 150.0)
    result["helper_secs"] = round(time.time() - started, 1)
except (ScenarioFailure, TimeoutError) as e:
    result["helper_secs"] = round(time.time() - started, 1)
    result["helper_detail"] = str(e)[:200]

# What each of them was told about itself, which is the whole point of C.
time.sleep(5.0)
result["a_observed_self"] = observed_self(f"{WORK}/a")
result["b_observed_self"] = observed_self(f"{WORK}/b")

started = time.time()
try:
    wait_until(lambda: _upgraded(a, b), "A to come up direct with B", 150.0)
    wait_until(lambda: _upgraded(b, a), "B to hold the far side", 60.0)
    result["upgraded"] = True
    result["seconds"] = round(time.time() - started, 1)
except (ScenarioFailure, TimeoutError) as e:
    result["upgraded"] = False
    result["seconds"] = round(time.time() - started, 1)
    result["detail"] = str(e)[:200]
    result["a_failure"] = a.upgrade_failure(b.hash)
    result["b_failure"] = b.upgrade_failure(a.hash)

a.send(channel, "nat-helper-message")
carried = False
for _ in range(60):
    if "nat-helper-message" in b.contents(channel):
        carried = True
        break
    time.sleep(1.0)
result["message_carried"] = carried
Path(WORK, "cone_helper.result.json").write_text(repr(result))
print(f"  {result}")
raise SystemExit(0 if result["upgraded"] else 1)
PYTHON
}

# The pair nobody can name: two NATs, no helper, and an address echo outside
# both networks. It has two halves and needs both, because a variant that only
# watched the pair come up could not tell a working echo from a punch that
# would have worked anyway: first with the echo off, where the pair must stay
# on Reticulum and say why, and then with it on.
drive_stun() {
    "$PYTHON" - "$WORK" "$REPO_ROOT" "$LAN_A.2" "$LAN_B.2" \
        "$HUB_IP:$STUN_PORT" <<'PYTHON'
import sys
import time
from pathlib import Path

WORK, REPO_ROOT, HOST_A, HOST_B, ECHO = sys.argv[1:6]
sys.path.insert(0, f"{REPO_ROOT}/devtools/testenv/scenarios")
sys.path.insert(0, f"{REPO_ROOT}/devtools/testenv")
sys.path.insert(0, REPO_ROOT)

from asserts import ScenarioFailure, set_timeout_scale, wait_until  # noqa: E402
from flows import invite_only_channel  # noqa: E402
from peer import Peer  # noqa: E402
from scen_upgrade import _upgraded  # noqa: E402
from trenchchat.core.upgrade import REASON_NO_PUBLIC_ADDRESS  # noqa: E402
from trenchchat.network.base import PATH_DIRECT  # noqa: E402

set_timeout_scale(1.5)
a = Peer("A", 8901, "nat-harness-token", host=HOST_A)
b = Peer("B", 8902, "nat-harness-token", host=HOST_B)
for peer in (a, b):
    deadline = time.time() + 120
    while time.time() < deadline and not peer.alive():
        time.sleep(1.0)
    if not peer.alive():
        raise SystemExit(f"{peer.tag}'s API never came up")


def stuck_for_an_address(peer, other) -> bool:
    """Whether this tester has given up on a pair for want of its own address."""
    failure = peer.upgrade_failure(other.hash) or {}
    return failure.get("reason") == REASON_NO_PUBLIC_ADDRESS


channel = invite_only_channel(a, [b], "nat-stun-room")
result = {"mode": "cone_stun", "channel": channel[:12]}

# Half one: the echo is off, which is the default, and must stay unasked.
started = time.time()
try:
    wait_until(lambda: stuck_for_an_address(a, b) and stuck_for_an_address(b, a),
               "both testers to give up for want of their own address", 120.0)
    result["stuck_secs"] = round(time.time() - started, 1)
    result["stuck_without_echo"] = True
except (ScenarioFailure, TimeoutError) as e:
    result["stuck_without_echo"] = False
    result["stuck_detail"] = str(e)[:200]
    result["a_failure"] = a.upgrade_failure(b.hash)
    result["b_failure"] = b.upgrade_failure(a.hash)
result["upgraded_without_echo"] = _upgraded(a, b)
result["asked_before_enabling"] = [peer.stun()["enabled"] for peer in (a, b)]
result["needs_public_address"] = [peer.needs_public_address() for peer in (a, b)]

# Half two: both users switch it on, and the pair has something to name.
for peer in (a, b):
    peer.set_stun(enabled=True, servers=[ECHO])
started = time.time()
try:
    wait_until(lambda: _upgraded(a, b), "A to open a session with B", 150.0)
    wait_until(lambda: _upgraded(b, a), "B to hold the far side", 60.0)
    wait_until(lambda: a.member_path(channel, b.hash) == PATH_DIRECT,
               "A's roster to show B as direct", 60.0)
    result["upgraded"] = True
    result["seconds"] = round(time.time() - started, 1)
except (ScenarioFailure, TimeoutError) as e:
    result["upgraded"] = False
    result["seconds"] = round(time.time() - started, 1)
    result["detail"] = str(e)[:200]
    result["a_failure"] = a.upgrade_failure(b.hash)
    result["b_failure"] = b.upgrade_failure(a.hash)

a.send(channel, "nat-stun-message")
carried = False
for _ in range(60):
    if "nat-stun-message" in b.contents(channel):
        carried = True
        break
    time.sleep(1.0)
result["message_carried"] = carried
Path(WORK, "cone_stun.result.json").write_text(repr(result))
print(f"  {result}")
raise SystemExit(0 if (result["upgraded"] and result["stuck_without_echo"]
                       and not result["upgraded_without_echo"]) else 1)
PYTHON
}

# The IPv6 case: no translation anywhere, both peers knowing the address they
# will be reached at, and a firewall that only asks each of them to send first.
# What it has to show beyond "a session came up" is which family carried it,
# since a pair that quietly punched over IPv4 would look exactly the same.
drive_ipv6() {
    "$PYTHON" - "$WORK" "$REPO_ROOT" "$HOST_A" "$HOST_B" <<'PYTHON'
import sys
import time
from pathlib import Path

WORK, REPO_ROOT, HOST_A, HOST_B = sys.argv[1:5]
sys.path.insert(0, f"{REPO_ROOT}/devtools/testenv/scenarios")
sys.path.insert(0, f"{REPO_ROOT}/devtools/testenv")
sys.path.insert(0, REPO_ROOT)

from asserts import ScenarioFailure, set_timeout_scale, wait_until  # noqa: E402
from flows import invite_only_channel  # noqa: E402
from peer import Peer  # noqa: E402
from scen_upgrade import _upgraded  # noqa: E402
from trenchchat.core.storage import Storage  # noqa: E402
from trenchchat.network.base import PATH_DIRECT  # noqa: E402

set_timeout_scale(1.5)
a = Peer("A", 8901, "nat-harness-token", host=HOST_A)
b = Peer("B", 8902, "nat-harness-token", host=HOST_B)
for peer in (a, b):
    deadline = time.time() + 120
    while time.time() < deadline and not peer.alive():
        time.sleep(1.0)
    if not peer.alive():
        raise SystemExit(f"{peer.tag}'s API never came up")


def read_store(data_dir: str, read):
    """Open a tester's store, ask it one question, and close it again."""
    store = Storage(db_path=Path(data_dir) / "storage.db")
    try:
        return read(store)
    finally:
        store.close()


def seen_peer_at(data_dir: str, peer_hash: str):
    """Where a tester saw the other's probes arrive from."""
    return read_store(data_dir, lambda store: store.get_upgrade_address(
        peer_hash, "peer"))


channel = invite_only_channel(a, [b], "ipv6-room")
started = time.time()
result = {"mode": "ipv6_fw", "channel": channel[:12]}
try:
    wait_until(lambda: _upgraded(a, b), "A to open a session with B", 120.0)
    wait_until(lambda: _upgraded(b, a), "B to hold the far side", 60.0)
    wait_until(lambda: a.member_path(channel, b.hash) == PATH_DIRECT,
               "A's roster to show B as direct", 60.0)
    result["upgraded"] = True
    result["seconds"] = round(time.time() - started, 1)
except (ScenarioFailure, TimeoutError) as e:
    result["upgraded"] = False
    result["seconds"] = round(time.time() - started, 1)
    result["detail"] = str(e)[:200]
    result["failure"] = a.upgrade_failure(b.hash)

result["a_saw_b_at"] = seen_peer_at(f"{WORK}/a", b.hash)
result["b_saw_a_at"] = seen_peer_at(f"{WORK}/b", a.hash)
# The addresses are stored as this node read them off the wire, so a colon in
# one is the punch saying which family it crossed.
result["over_ipv6"] = all(":" in (seen or ("", 0))[0]
                          for seen in (result["a_saw_b_at"], result["b_saw_a_at"]))
result["observed_self"] = {
    tag: read_store(f"{WORK}/{tag}",
                    lambda store: store.get_upgrade_addresses("self"))
    for tag in ("a", "b")
}

a.send(channel, "ipv6-harness-message")
carried = False
for _ in range(60):
    if "ipv6-harness-message" in b.contents(channel):
        carried = True
        break
    time.sleep(1.0)
result["message_carried"] = carried
Path(WORK, "ipv6_fw.result.json").write_text(repr(result))
print(f"  {result}")
raise SystemExit(0 if result["upgraded"] and result["over_ipv6"] else 1)
PYTHON
}

run_variant() {
    local mode="$1" expect="$2"
    echo "=== $mode, upgrade expected to $expect ==="
    mkdir -p "$WORK"
    setup "$mode"
    start_hub
    start_worker "$NS_A" A "$WORK/a" "$API_A" "$HOST_A" trenchchat_nat_a
    worker_a_pid=$!
    start_worker "$NS_B" B "$WORK/b" "$API_B" "$HOST_B" trenchchat_nat_b
    worker_b_pid=$!
    if has_helper "$mode"; then
        start_worker "$NS_C" C "$WORK/c" "$API_C" "$WAN.4" trenchchat_nat_c
        worker_c_pid=$!
    fi
    if has_stun "$mode"; then
        start_stun
    fi

    if has_helper "$mode"; then
        drive_helper
    elif has_stun "$mode"; then
        drive_stun
    elif is_ipv6 "$mode"; then
        drive_ipv6
    else
        drive "$mode"
    fi
    local rc=$?
    stop_processes
    teardown

    if [ "$expect" = "record" ]; then
        [ "$rc" = "0" ] && echo "  NOTE: the pair upgraded" \
                        || echo "  NOTE: the pair stayed on Reticulum"
        return 0
    fi
    if [ "$expect" = "succeed" ]; then
        [ "$rc" = "0" ] && { echo "  PASS"; return 0; }
        echo "  FAIL: the variant did not behave as it must; see $WORK/A.log"
        return 1
    fi
    [ "$rc" != "0" ] && { echo "  PASS: the pair stayed on Reticulum, as it must"; return 0; }
    echo "  FAIL: a session came up through a symmetric NAT, which it must not"
    return 1
}

main() {
    local which="${1:-both}"
    preflight
    trap teardown EXIT
    local failures=0
    case "$which" in
        one_nat)     run_variant one_nat succeed || failures=1 ;;
        cone)        run_variant cone record ;;
        cone_helper) run_variant cone_helper succeed || failures=1 ;;
        cone_stun)   run_variant cone_stun succeed || failures=1 ;;
        symmetric)   run_variant symmetric fail || failures=1 ;;
        ipv6_fw)     require_ipv6; run_variant ipv6_fw succeed || failures=1 ;;
        all|both)
            run_variant one_nat succeed || failures=1
            run_variant cone record
            run_variant cone_helper succeed || failures=1
            run_variant cone_stun succeed || failures=1
            run_variant symmetric fail || failures=1
            if have_ipv6; then
                run_variant ipv6_fw succeed || failures=1
            else
                echo "=== ipv6_fw skipped: this kernel has no IPv6"
            fi
            ;;
        *) fail "unknown variant '$which', expected one_nat, cone, cone_helper, "\
                "cone_stun, symmetric, ipv6_fw or all" ;;
    esac
    [ "$failures" = "0" ] && echo "every variant behaved as expected"
    return "$failures"
}

main "$@"
