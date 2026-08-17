#!/usr/bin/env bash
# One-command bootstrap for the Flutter client: Python venv + dependencies,
# voice system libraries, a Flutter SDK if none is installed, the client
# build, and launch. Idempotent -- re-run any time; each step skips what's
# already done.
#
# Testing hooks: TRENCHCHAT_OS_RELEASE overrides /etc/os-release,
# TRENCHCHAT_SETUP_NO_LAUNCH=1 prints the launch command instead of running it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_DIR="$REPO_ROOT/.flutter-sdk"
OS_RELEASE="${TRENCHCHAT_OS_RELEASE:-/etc/os-release}"
FLUTTER_RELEASES_URL="${TRENCHCHAT_FLUTTER_RELEASES_URL:-https://storage.googleapis.com/flutter_infra_release/releases}"

log() { echo "[setup] $*"; }

ask() {
    # ask "prompt" default(y|n) -> returns 0 for yes. An empty answer takes
    # the default; EOF (no interactive stdin left) always declines, so a
    # non-interactive run can never be defaulted into a download or a sudo.
    local prompt="$1" default="$2" reply
    if [ "$default" = "y" ]; then prompt="$prompt (Y/n) "; else prompt="$prompt (y/N) "; fi
    printf "%s" "$prompt"
    if ! read -r reply; then
        echo ""
        return 1
    fi
    reply="${reply:-$default}"
    [[ "$reply" =~ ^[Yy] ]]
}

# --- Quad4 Reticulum node ---
RETICULUM_CONFIG="$HOME/.reticulum/config"
QUAD4_BLOCK="
  [[Quad4]]
    type = TCPClientInterface
    interface_enabled = true
    target_host = 62.151.179.77
    target_port = 45657
    mode = full"

if [ -f "$RETICULUM_CONFIG" ]; then
    if grep -q "\[\[Quad4\]\]" "$RETICULUM_CONFIG"; then
        log "Quad4 interface already present in Reticulum config, skipping."
    elif ask "Add Quad4 TCP node to Reticulum config?" n; then
        printf "%s\n" "$QUAD4_BLOCK" >> "$RETICULUM_CONFIG"
        log "Quad4 interface added."
    else
        log "Skipping Quad4 interface."
    fi
else
    log "Reticulum config not found at $RETICULUM_CONFIG -- skipping Quad4 setup."
    log "(Run the app once to generate the config, then re-run this script.)"
fi

echo ""

# --- Python 3.10+ ---
PYTHON=$(command -v python3 || command -v python || true)
if [ -z "$PYTHON" ]; then
    log "ERROR: Python 3 not found. Install Python 3.10 or newer and try again."
    exit 1
fi
PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)")
PY_MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    log "ERROR: Python 3.10+ required (found $PY_VERSION)."
    exit 1
fi
log "Using Python $PY_VERSION"

# --- venv + Python dependencies (app + the Flutter client's backend) ---
if [ ! -d "$REPO_ROOT/.venv" ]; then
    log "creating virtual environment..."
    "$PYTHON" -m venv "$REPO_ROOT/.venv"
fi
log "installing Python dependencies..."
"$REPO_ROOT/.venv/bin/pip" install --upgrade pip --quiet
"$REPO_ROOT/.venv/bin/pip" install -r "$REPO_ROOT/requirements.txt" --quiet
"$REPO_ROOT/.venv/bin/pip" install -r "$REPO_ROOT/devtools/testenv/requirements.txt" --quiet

# --- system libraries for voice (libopus + PortAudio) ---
have_lib() { ldconfig -p 2>/dev/null | grep -q "$1"; }

voice_libs_present() { have_lib libopus && have_lib libportaudio; }

os_id() {
    [ -f "$OS_RELEASE" ] && . "$OS_RELEASE" 2>/dev/null && echo "${ID:-}" || echo ""
}

offer_voice_libs() {
    local mgr="$1" pkgs="$2"
    log "Voice chat needs libopus and PortAudio (currently missing)."
    if ask "Install them now with sudo $mgr? ($pkgs)" y; then
        case "$mgr" in
            apt)    sudo apt-get install -y $pkgs ;;
            dnf)    sudo dnf install -y $pkgs ;;
            pacman) sudo pacman -S --noconfirm --needed $pkgs ;;
        esac
    fi
}

if voice_libs_present; then
    log "voice system libraries: present"
else
    case "$(os_id)" in
        steamos)
            # SteamOS may or may not have its root filesystem unlocked.
            if command -v steamos-readonly >/dev/null 2>&1 && \
                    steamos-readonly status 2>/dev/null | grep -qi disabled; then
                log "SteamOS with an unlocked root filesystem detected."
                log "NOTE: SteamOS updates can remove pacman-installed packages;"
                log "      re-run this script after an OS update to restore them."
                offer_voice_libs pacman "opus portaudio"
            else
                log "SteamOS with a read-only root filesystem detected."
                log "Voice audio needs system libraries this script can't install here."
                log "Either run everything inside a Distrobox:"
                log "    distrobox create --name trench --image ubuntu:24.04"
                log "    distrobox enter trench   # then re-run ./setup.sh in there"
                log "or unlock the rootfs first (sudo steamos-readonly disable) and re-run."
                log "Continuing without them -- voice will be receive-silent on this host."
            fi
            ;;
        *)
            if command -v apt-get >/dev/null 2>&1; then
                offer_voice_libs apt "libopus0 libportaudio2"
            elif command -v dnf >/dev/null 2>&1; then
                offer_voice_libs dnf "opus portaudio"
            elif command -v pacman >/dev/null 2>&1; then
                offer_voice_libs pacman "opus portaudio"
            else
                log "NOTE: couldn't detect a package manager. Install libopus and"
                log "      PortAudio manually for audible voice chat."
            fi
            ;;
    esac
    if ! voice_libs_present; then
        have_lib libopus || log "NOTE: libopus still missing -- voice will be receive-silent."
        have_lib libportaudio || log "NOTE: PortAudio still missing -- voice will be receive-silent."
    fi
fi

# --- Flutter SDK ---
find_flutter() {
    if command -v flutter >/dev/null 2>&1; then
        command -v flutter
    elif [ -x "$SDK_DIR/flutter/bin/flutter" ]; then
        echo "$SDK_DIR/flutter/bin/flutter"
    fi
}

FLUTTER="$(find_flutter || true)"
if [ -z "$FLUTTER" ] && [ ! -f "$REPO_ROOT/flutter_ui/build/web/index.html" ]; then
    log "No Flutter SDK found and no client build present."
    if ask "Download the Flutter SDK (~1 GB) into $SDK_DIR?" y; then
        archive=$(curl -sSL "$FLUTTER_RELEASES_URL/releases_linux.json" | "$PYTHON" -c "
import json, sys
d = json.load(sys.stdin)
stable = d['current_release']['stable']
print(next(r['archive'] for r in d['releases'] if r['hash'] == stable))
")
        log "downloading $archive ..."
        mkdir -p "$SDK_DIR"
        curl -SL "$FLUTTER_RELEASES_URL/$archive" | tar xJ -C "$SDK_DIR"
        FLUTTER="$SDK_DIR/flutter/bin/flutter"
        # The SDK ships as a git checkout; git refuses to run in it when the
        # ownership check trips (containers, sudo). Scoped to the SDK only.
        git config --global --add safe.directory "$SDK_DIR/flutter" 2>/dev/null || true
        "$FLUTTER" config --no-analytics >/dev/null 2>&1 || true
    fi
fi

# --- client build ---
if [ -n "$FLUTTER" ]; then
    if [ ! -f "$REPO_ROOT/flutter_ui/build/web/index.html" ]; then
        log "building the Flutter web client (one-time)..."
        (cd "$REPO_ROOT/flutter_ui" && "$FLUTTER" build web)
    else
        log "Flutter web client build: present"
    fi
    if [ ! -x "$REPO_ROOT/flutter_ui/build/linux/x64/release/bundle/flutter_ui" ]; then
        if pkg-config --exists gtk+-3.0 2>/dev/null && command -v clang >/dev/null 2>&1 \
                && command -v cmake >/dev/null 2>&1 && command -v ninja >/dev/null 2>&1; then
            if ask "Also build the native Linux desktop client?" n; then
                (cd "$REPO_ROOT/flutter_ui" && "$FLUTTER" build linux)
            fi
        else
            log "(native desktop build available later: install clang cmake ninja-build"
            log " pkg-config libgtk-3-dev, then run: cd flutter_ui && flutter build linux)"
        fi
    fi
fi

# --- launch ---
echo ""
if [ -f "$REPO_ROOT/flutter_ui/build/web/index.html" ] || \
        [ -x "$REPO_ROOT/flutter_ui/build/linux/x64/release/bundle/flutter_ui" ]; then
    ENTRY="main_flutter.py"
else
    log "No client build (no Flutter SDK) -- launching the legacy Qt app instead."
    ENTRY="main.py"
fi
log "Setup complete. Launching TrenchChat ($ENTRY)..."
echo ""
if [ "${TRENCHCHAT_SETUP_NO_LAUNCH:-}" = "1" ]; then
    log "TRENCHCHAT_SETUP_NO_LAUNCH=1 -- would run: .venv/bin/python $ENTRY $*"
    exit 0
fi
exec "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/$ENTRY" "$@"
