#!/bin/bash
# Provisions a Claude Code on the web container for TrenchChat.
#
# Three things this repo's commands need and a fresh container has none of:
# the .venv both test suites run from, the system libraries the voice and Qt
# tests link against (without libopus/libegl the suite reports 16 failures and
# 9 collection errors that have nothing to do with the code), and a Flutter
# SDK for flutter_ui.
#
# Local machines are setup.sh's job -- it asks before downloading a gigabyte
# or running sudo, which a hook cannot do -- so this runs on the web only.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
    exit 0
fi

REPO_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SDK_DIR="$REPO_ROOT/.flutter-sdk"
FLUTTER_BIN="$SDK_DIR/flutter/bin"
FLUTTER_RELEASES_URL="https://storage.googleapis.com/flutter_infra_release/releases"

log() { echo "[session-start] $*"; }

# --- system libraries ---------------------------------------------------
# libopus/libportaudio: tests/test_voice_audio.py imports opuslib, which
# dlopen()s them.
# ldconfig's output is captured once rather than piped into grep: `grep -q`
# exits on its first match and SIGPIPEs ldconfig, which `set -o pipefail`
# then reads as a failed check.
LDCONFIG_CACHE="$(ldconfig -p 2>/dev/null || true)"
have_lib() { case "$LDCONFIG_CACHE" in *"$1"*) return 0 ;; *) return 1 ;; esac; }

if have_lib libopus && have_lib libportaudio; then
    log "system libraries: present"
else
    log "installing system libraries (libopus, PortAudio)..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq libopus0 libportaudio2
fi

# --- Python venv --------------------------------------------------------
if [ ! -d "$REPO_ROOT/.venv" ]; then
    log "creating .venv..."
    python3 -m venv "$REPO_ROOT/.venv"
fi
log "installing Python dependencies..."
"$REPO_ROOT/.venv/bin/pip" install --upgrade pip --quiet
"$REPO_ROOT/.venv/bin/pip" install -r "$REPO_ROOT/requirements.txt" --quiet
"$REPO_ROOT/.venv/bin/pip" install -r "$REPO_ROOT/devtools/testenv/requirements.txt" --quiet

# --- Flutter SDK --------------------------------------------------------
# Not fatal: a session that only touches trenchchat/ still wants its venv, so
# a failure here degrades to "no Flutter" rather than to no session.
install_flutter() {
    if [ -x "$FLUTTER_BIN/flutter" ]; then
        log "Flutter SDK: present"
    else
        # Same archive setup.sh picks, so both provision the same SDK.
        local archive
        archive=$(curl -sSL "$FLUTTER_RELEASES_URL/releases_linux.json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
stable = d['current_release']['stable']
print(next(r['archive'] for r in d['releases'] if r['hash'] == stable))
")
        log "downloading $archive (~1 GB)..."
        mkdir -p "$SDK_DIR"
        curl -sSL "$FLUTTER_RELEASES_URL/$archive" | tar xJ -C "$SDK_DIR"
    fi
    # The SDK ships as a git checkout; git refuses to run in it when the
    # ownership check trips, which it does in a container. Scoped to the SDK,
    # and only once -- --add does not deduplicate, and this hook re-runs on
    # every resume and compact.
    local trusted
    trusted="$(git config --global --get-all safe.directory 2>/dev/null || true)"
    case "$trusted" in
        *"$SDK_DIR/flutter"*) ;;
        *) git config --global --add safe.directory "$SDK_DIR/flutter" 2>/dev/null || true ;;
    esac
    "$FLUTTER_BIN/flutter" --disable-analytics >/dev/null 2>&1 || true
    log "warming the pub cache..."
    (cd "$REPO_ROOT/flutter_ui" && "$FLUTTER_BIN/flutter" pub get >/dev/null)
}

if install_flutter; then
    # Put flutter and dart on PATH for the session, once.
    if [ -n "${CLAUDE_ENV_FILE:-}" ] && ! grep -qF ".flutter-sdk/flutter/bin" "$CLAUDE_ENV_FILE" 2>/dev/null; then
        echo "export PATH=\"$FLUTTER_BIN:\$PATH\"" >> "$CLAUDE_ENV_FILE"
    fi
    log "flutter: $("$FLUTTER_BIN/flutter" --version 2>/dev/null | head -1)"
else
    log "WARNING: the Flutter SDK did not install; flutter_ui commands will not run."
fi

log "ready."
