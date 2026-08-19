#!/usr/bin/env bash
# Boot the remote-testing stack in a headless container: a Tailscale node
# (userspace networking), the Flutter web client backed by its own dedicated
# identity (serve_profile.py, joined to the testenv hub), and the dev
# environment alongside it so the mesh has other users to talk to.
#
# Usage: remote_host.sh [start|stop|status]
#
# Join is non-interactive when TS_AUTHKEY (or TS_AUTH_KEY) is set -- e.g. a
# reusable+ephemeral key injected as an environment variable; otherwise a
# login URL is printed once. Tailscale state persists in the state dir, so
# restarts reconnect without re-authenticating. The client's profile lives in
# ~/.trenchchat, so its identity survives restarts too.
#
# Overrides: TRENCHCHAT_REMOTE_STATE (state dir, default ~/.trenchchat-remote),
# TRENCHCHAT_REMOTE_VENV (python venv), TRENCHCHAT_REMOTE_HOSTNAME (node name),
# REMOTE_CLIENT_PORT (default 8899), TESTENV_TESTERS (default 4).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE_DIR="${TRENCHCHAT_REMOTE_STATE:-$HOME/.trenchchat-remote}"
VENV="${TRENCHCHAT_REMOTE_VENV:-$STATE_DIR/venv}"
TS_DIR="$STATE_DIR/tailscale"
TS_SOCK="$TS_DIR/tailscaled.sock"
TS_VERSION="1.86.2"
TS_HOSTNAME="${TRENCHCHAT_REMOTE_HOSTNAME:-trenchchat-dev}"
CLIENT_PORT="${REMOTE_CLIENT_PORT:-8899}"
TESTERS="${TESTENV_TESTERS:-4}"
HUB_PORT=41001
AUTH_KEY="${TS_AUTHKEY:-${TS_AUTH_KEY:-}}"
STOP_WAIT_SECS=15

log() { echo "[remote-host] $*"; }

port_up() { curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:$1/" 2>/dev/null; }

ts() { "$TS_DIR/bin/tailscale" --socket="$TS_SOCK" "$@"; }

ensure_venv() {
    [ -x "$VENV/bin/python" ] && return
    log "creating venv at $VENV"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet \
        rns==1.4.2 lxmf==1.1.1 msgpack==1.1.2 configobj==5.0.9 \
        Pillow==12.1.1 cryptography==46.0.5 \
        -r "$REPO_ROOT/devtools/testenv/requirements.txt"
}

ensure_web_build() {
    [ -f "$REPO_ROOT/flutter_ui/build/web/index.html" ] && return
    command -v flutter >/dev/null || {
        log "no web build and no flutter SDK; run 'flutter build web' first"
        exit 1
    }
    log "building the Flutter web client"
    (cd "$REPO_ROOT/flutter_ui" && flutter build web)
}

ensure_tailscale() {
    [ -x "$TS_DIR/bin/tailscale" ] && return
    log "downloading tailscale $TS_VERSION"
    mkdir -p "$TS_DIR/bin"
    curl -sSL "https://pkgs.tailscale.com/stable/tailscale_${TS_VERSION}_amd64.tgz" |
        tar xz -C "$TS_DIR/bin" --strip-components=1
}

ensure_rns_config() {
    [ -f "$STATE_DIR/rns/config" ] && return
    mkdir -p "$STATE_DIR/rns"
    cat >"$STATE_DIR/rns/config" <<EOF
[reticulum]
enable_transport = False
share_instance = No
instance_name = trenchchat_remote_client

[logging]
loglevel = 3

[interfaces]
  [[TestenvHub]]
    type = TCPClientInterface
    interface_enabled = true
    target_host = 127.0.0.1
    target_port = $HUB_PORT
EOF
}

start_tailscale() {
    if ! ts version --daemon >/dev/null 2>&1; then
        log "starting tailscaled (userspace networking)"
        mkdir -p "$TS_DIR/state"
        nohup "$TS_DIR/bin/tailscaled" --tun=userspace-networking \
            --statedir="$TS_DIR/state" --socket="$TS_SOCK" \
            >"$STATE_DIR/tailscaled.log" 2>&1 &
        echo $! >"$STATE_DIR/tailscaled.pid"
        sleep 3
    fi
    if [ -n "$AUTH_KEY" ]; then
        ts up --hostname="$TS_HOSTNAME" --auth-key="$AUTH_KEY" --timeout=60s
    elif ! ts up --hostname="$TS_HOSTNAME" --timeout=90s; then
        log "not authenticated yet -- visit the URL above, then re-run start"
        return 1
    fi
}

start_testenv() {
    port_up 8800 && return
    log "starting dev environment ($TESTERS testers)"
    # Both servers bind localhost by default; hosting for the tailnet is the
    # whole point of this script, so it opts in explicitly. The API token
    # printed in each log is what protects the exposed identities.
    #
    # The tailnet address has to be passed in: the orchestrator can only
    # discover the address its default route uses, and every call the pages
    # make to a tester is cross-origin, so an address missing from the
    # allow-list shows up as every tester "failed to connect".
    local origins=()
    local ip
    ip="$(ts_ip)"
    if [ -n "$ip" ]; then
        origins+=(--page-origin "http://$ip:8800" --page-origin "http://$ip:$CLIENT_PORT")
    fi
    nohup "$VENV/bin/python" "$REPO_ROOT/devtools/testenv/orchestrator.py" \
        --testers "$TESTERS" --host 0.0.0.0 "${origins[@]}" \
        >"$STATE_DIR/orchestrator.log" 2>&1 &
    echo $! >"$STATE_DIR/orchestrator.pid"
}

ts_ip() {
    ts ip -4 2>/dev/null | head -1
}

start_client() {
    port_up "$CLIENT_PORT" && return
    log "starting web client (own identity) on $CLIENT_PORT"
    ensure_rns_config
    nohup "$VENV/bin/python" "$REPO_ROOT/devtools/testenv/serve_profile.py" \
        --port "$CLIENT_PORT" --host 0.0.0.0 --rns-configdir "$STATE_DIR/rns" \
        >"$STATE_DIR/client.log" 2>&1 &
    echo $! >"$STATE_DIR/client.pid"
}

cmd_start() {
    mkdir -p "$STATE_DIR"
    ensure_venv
    ensure_web_build
    ensure_tailscale
    # Before the servers, so their CORS allow-list can name the tailnet
    # address the user will actually reach them on.
    start_tailscale || true
    start_testenv
    start_client
    cmd_status
}

# Wait for a port to stop answering, so a stop immediately followed by a start
# doesn't see the dying service and skip relaunching it.
await_port_down() {
    local port="$1" i
    for i in $(seq 1 "$STOP_WAIT_SECS"); do
        port_up "$port" || return 0
        sleep 1
    done
    log "warning: port $port still answering after ${STOP_WAIT_SECS}s"
}

cmd_stop() {
    for name in client orchestrator tailscaled; do
        if [ -f "$STATE_DIR/$name.pid" ]; then
            kill "$(cat "$STATE_DIR/$name.pid")" 2>/dev/null || true
            rm -f "$STATE_DIR/$name.pid"
        fi
    done
    pkill -f 'testenv/[w]orker\.py' 2>/dev/null || true
    pkill -f 'testenv/[h]ub\.py' 2>/dev/null || true
    await_port_down 8800
    await_port_down "$CLIENT_PORT"
    log "stopped"
}

cmd_status() {
    port_up 8800 && log "dev environment: up" || log "dev environment: down"
    port_up "$CLIENT_PORT" && log "web client: up" || log "web client: down"
    local ip
    ip="$(ts ip -4 2>/dev/null || true)"
    if [ -n "$ip" ]; then
        log "tailscale: $(ts status --json 2>/dev/null |
            grep -o '"BackendState": "[^"]*"' | head -1)"
        log "web client:      http://$ip:$CLIENT_PORT/  (token in client.log)"
        log "dev environment: http://$ip:8800/"
    else
        log "tailscale: not connected"
    fi
}

case "${1:-start}" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    *) echo "usage: $0 [start|stop|status]"; exit 2 ;;
esac
