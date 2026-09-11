#!/usr/bin/env bash
#
# Phase 0 spike: a UDP hole punch between two Linux network namespaces, each behind its
# own masquerading NAT, plus the symmetric variant that must fail.
#
# Topology, four namespaces and three veth pairs:
#
#   tcu-a                tcu-nat-a            tcu-nat-b                tcu-b
#   10.1.0.2/24 -------- 10.1.0.1/24          10.2.0.1/24 ------------ 10.2.0.2/24
#                        198.51.100.1/24 ---- 198.51.100.2/24
#
# The "cone" variant masquerades normally, which on Linux keeps the source port and
# filters on address and port, so both peers punch. The "symmetric" variant masquerades
# `fully-random`, so the external port cannot be predicted from the candidate and every
# probe lands on a NAT with no matching mapping.
#
# Needs root with CAP_NET_ADMIN, iproute2 and nftables. Linux only.
#
#   sudo ./netns_nat.sh                 # both variants
#   sudo ./netns_nat.sh cone            # just the punchable one
#   sudo PYTHON=/path/to/python ./netns_nat.sh symmetric

set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
WORK="${WORK:-/tmp/trenchchat-netns-punch}"
PUNCH_SECS="${PUNCH_SECS:-8}"
PORT=5000

NS_A=tcu-a
NS_NAT_A=tcu-nat-a
NS_NAT_B=tcu-nat-b
NS_B=tcu-b
LAN_A=10.1.0
LAN_B=10.2.0
WAN=198.51.100

fail() { echo "netns_nat.sh: $*" >&2; exit 1; }

preflight() {
    [ "$(id -u)" = "0" ] || fail "must run as root (id -u is $(id -u))"
    command -v ip  >/dev/null || fail "iproute2 is missing: install it for the ip command"
    command -v nft >/dev/null || fail "nftables is missing: install it for the nft command"
    ip netns add tcu-preflight 2>/dev/null \
        || fail "ip netns add refused: the container lacks CAP_NET_ADMIN or /var/run/netns"
    ip netns del tcu-preflight
    ip link add tcu-probe0 type veth peer name tcu-probe1 2>/dev/null \
        || fail "ip link add veth refused: the container lacks CAP_NET_ADMIN"
    ip link del tcu-probe0
}

teardown() {
    for ns in "$NS_A" "$NS_NAT_A" "$NS_NAT_B" "$NS_B"; do
        ip netns del "$ns" 2>/dev/null || true
    done
}

setup() {
    local nat_mode="$1"
    teardown
    for ns in "$NS_A" "$NS_NAT_A" "$NS_NAT_B" "$NS_B"; do
        ip netns add "$ns"
        ip netns exec "$ns" ip link set lo up
    done

    ip link add tcu-a0 type veth peer name tcu-a1
    ip link add tcu-w0 type veth peer name tcu-w1
    ip link add tcu-b0 type veth peer name tcu-b1
    ip link set tcu-a0 netns "$NS_A"
    ip link set tcu-a1 netns "$NS_NAT_A"
    ip link set tcu-w0 netns "$NS_NAT_A"
    ip link set tcu-w1 netns "$NS_NAT_B"
    ip link set tcu-b1 netns "$NS_NAT_B"
    ip link set tcu-b0 netns "$NS_B"

    ip netns exec "$NS_A" ip addr add "$LAN_A.2/24" dev tcu-a0
    ip netns exec "$NS_A" ip link set tcu-a0 up
    ip netns exec "$NS_A" ip route add default via "$LAN_A.1"

    ip netns exec "$NS_NAT_A" ip addr add "$LAN_A.1/24" dev tcu-a1
    ip netns exec "$NS_NAT_A" ip addr add "$WAN.1/24" dev tcu-w0
    ip netns exec "$NS_NAT_A" ip link set tcu-a1 up
    ip netns exec "$NS_NAT_A" ip link set tcu-w0 up
    ip netns exec "$NS_NAT_A" sysctl -qw net.ipv4.ip_forward=1

    ip netns exec "$NS_NAT_B" ip addr add "$LAN_B.1/24" dev tcu-b1
    ip netns exec "$NS_NAT_B" ip addr add "$WAN.2/24" dev tcu-w1
    ip netns exec "$NS_NAT_B" ip link set tcu-b1 up
    ip netns exec "$NS_NAT_B" ip link set tcu-w1 up
    ip netns exec "$NS_NAT_B" sysctl -qw net.ipv4.ip_forward=1

    ip netns exec "$NS_B" ip addr add "$LAN_B.2/24" dev tcu-b0
    ip netns exec "$NS_B" ip link set tcu-b0 up
    ip netns exec "$NS_B" ip route add default via "$LAN_B.1"

    masquerade "$NS_NAT_A" tcu-w0 "$nat_mode"
    masquerade "$NS_NAT_B" tcu-w1 "$nat_mode"
}

# The stateful drop is not decoration. Without it an unsolicited probe reaching a NAT
# before that NAT has made its own outbound mapping leaves an unreplied conntrack entry
# holding the exact tuple the mapping wants, Linux then remaps to a random external port,
# and the punch can never complete. Dropping the packet before conntrack confirms the
# entry is what a consumer NAT does, and what makes this a port-restricted cone NAT.
masquerade() {
    local ns="$1" wan="$2" mode="$3" flags=""
    [ "$mode" = "symmetric" ] && flags=" fully-random"
    ip netns exec "$ns" nft add table ip tcu
    ip netns exec "$ns" nft "add chain ip tcu post { type nat hook postrouting priority 100 ; }"
    ip netns exec "$ns" nft "add rule ip tcu post oifname \"$wan\" masquerade$flags"
    ip netns exec "$ns" nft \
        "add chain ip tcu input { type filter hook input priority 0 ; policy accept ; }"
    ip netns exec "$ns" nft \
        "add chain ip tcu forward { type filter hook forward priority 0 ; policy accept ; }"
    ip netns exec "$ns" nft "add rule ip tcu input iifname \"$wan\" ct state new,invalid drop"
    ip netns exec "$ns" nft "add rule ip tcu forward iifname \"$wan\" ct state new,invalid drop"
}

run_variant() {
    local mode="$1"
    local expect="$2"
    echo "=== $mode NAT, expecting the punch to $expect ==="
    setup "$mode"
    mkdir -p "$WORK"
    rm -f "$WORK/$mode.a.json" "$WORK/$mode.b.json"

    ip netns exec "$NS_A" "$PYTHON" "$HERE/punch.py" \
        --bind "$LAN_A.2:$PORT" --nonce a1a1 --peer-nonce b2b2 \
        --candidate "$WAN.2:$PORT" --candidate "$LAN_B.2:$PORT" \
        --seconds "$PUNCH_SECS" --out "$WORK/$mode.a.json" >/dev/null 2>&1 &
    local pid_a=$!
    ip netns exec "$NS_B" "$PYTHON" "$HERE/punch.py" \
        --bind "$LAN_B.2:$PORT" --nonce b2b2 --peer-nonce a1a1 \
        --candidate "$WAN.1:$PORT" --candidate "$LAN_A.2:$PORT" \
        --seconds "$PUNCH_SECS" --out "$WORK/$mode.b.json" >/dev/null 2>&1 &
    local pid_b=$!
    wait $pid_a; local rc_a=$?
    wait $pid_b; local rc_b=$?

    echo "  peer a: $(cat "$WORK/$mode.a.json" 2>/dev/null || echo 'no result')"
    echo "  peer b: $(cat "$WORK/$mode.b.json" 2>/dev/null || echo 'no result')"
    teardown

    if [ "$expect" = "succeed" ]; then
        [ "$rc_a" = "0" ] && [ "$rc_b" = "0" ] && { echo "  PASS"; return 0; }
        echo "  FAIL: the punch did not complete both ways"; return 1
    fi
    [ "$rc_a" != "0" ] && [ "$rc_b" != "0" ] && { echo "  PASS"; return 0; }
    echo "  FAIL: the punch succeeded through a symmetric NAT, which it must not"; return 1
}

main() {
    local which="${1:-both}"
    preflight
    trap teardown EXIT
    local failures=0
    case "$which" in
        cone)      run_variant cone succeed || failures=1 ;;
        symmetric) run_variant symmetric fail || failures=1 ;;
        both)
            run_variant cone succeed || failures=1
            run_variant symmetric fail || failures=1
            ;;
        *) fail "unknown variant '$which', expected cone, symmetric or both" ;;
    esac
    [ "$failures" = "0" ] && echo "all variants behaved as expected"
    return "$failures"
}

main "$@"
