#!/usr/bin/env bash
# Boot the remote-testing stack in a headless container: a Tailscale node
# (userspace networking), the two-tester environment, and remote_proxy.py
# serving the Flutter web client backed by tester A.
#
# Usage: remote_host.sh [start|stop|status]
#
# Join is non-interactive when TS_AUTHKEY (or TS_AUTH_KEY) is set -- e.g. a
# reusable+ephemeral key injected as an environment variable; otherwise a
# login URL is printed once. Tailscale state persists in the state dir, so
# restarts reconnect without re-authenticating.
#
# Overrides: TRENCHCHAT_REMOTE_STATE (state dir, default ~/.trenchchat-remote),
# TRENCHCHAT_REMOTE_VENV (python venv), TRENCHCHAT_REMOTE_HOSTNAME (node name),
# REMOTE_PROXY_PORT (default 8899).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE_DIR="${TRENCHCHAT_REMOTE_STATE:-$HOME/.trenchchat-remote}"
VENV="${TRENCHCHAT_REMOTE_VENV:-$STATE_DIR/venv}"
TS_DIR="$STATE_DIR/tailscale"
TS_SOCK="$TS_DIR/tailscaled.sock"
TS_VERSION="1.86.2"
TS_HOSTNAME="${TRENCHCHAT_REMOTE_HOSTNAME:-trenchchat-dev}"
PROXY_PORT="${REMOTE_PROXY_PORT:-8899}"
AUTH_KEY="${TS_AUTHKEY:-${TS_AUTH_KEY:-}}"

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

start_backend() {
    if ! port_up 8800; then
        log "starting two-tester environment"
        nohup "$VENV/bin/python" "$REPO_ROOT/devtools/testenv/orchestrator.py" \
            --testers 2 >"$STATE_DIR/orchestrator.log" 2>&1 &
        echo $! >"$STATE_DIR/orchestrator.pid"
    fi
    if ! port_up "$PROXY_PORT"; then
        log "starting single-origin proxy on $PROXY_PORT"
        nohup "$VENV/bin/python" "$REPO_ROOT/devtools/testenv/remote_proxy.py" \
            >"$STATE_DIR/proxy.log" 2>&1 &
        echo $! >"$STATE_DIR/proxy.pid"
    fi
}

cmd_start() {
    mkdir -p "$STATE_DIR"
    ensure_venv
    ensure_web_build
    ensure_tailscale
    start_backend
    start_tailscale || true
    cmd_status
}

cmd_stop() {
    for name in proxy orchestrator tailscaled; do
        if [ -f "$STATE_DIR/$name.pid" ]; then
            kill "$(cat "$STATE_DIR/$name.pid")" 2>/dev/null || true
            rm -f "$STATE_DIR/$name.pid"
        fi
    done
    pkill -f 'testenv/[w]orker\.py' 2>/dev/null || true
    pkill -f 'testenv/[h]ub\.py' 2>/dev/null || true
    log "stopped"
}

cmd_status() {
    port_up 8800 && log "backend: up" || log "backend: down"
    port_up "$PROXY_PORT" && log "proxy: up" || log "proxy: down"
    local ip
    ip="$(ts ip -4 2>/dev/null || true)"
    if [ -n "$ip" ]; then
        log "tailscale: $(ts status --json 2>/dev/null |
            grep -o '"BackendState": "[^"]*"' | head -1)"
        log "open: http://$ip:$PROXY_PORT/  (or http://$TS_HOSTNAME:$PROXY_PORT/ with MagicDNS)"
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
