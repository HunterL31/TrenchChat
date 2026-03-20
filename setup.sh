#!/usr/bin/env bash
set -e

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
        echo "Quad4 interface already present in Reticulum config, skipping."
    else
        printf "Add Quad4 TCP node to Reticulum config? (y/N) "
        read -r REPLY
        if [[ "$REPLY" =~ ^[Yy]$ ]]; then
            printf "%s\n" "$QUAD4_BLOCK" >> "$RETICULUM_CONFIG"
            echo "Quad4 interface added."
        else
            echo "Skipping Quad4 interface."
        fi
    fi
else
    echo "Reticulum config not found at $RETICULUM_CONFIG — skipping Quad4 setup."
    echo "(Run the app once to generate the config, then re-run this script.)"
fi

echo ""

# Require Python 3.10+
PYTHON=$(command -v python3 || command -v python || true)
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3 not found. Install Python 3.10 or newer and try again."
    exit 1
fi

PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)")
PY_MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "ERROR: Python 3.10+ required (found $PY_VERSION)."
    exit 1
fi

echo "Using Python $PY_VERSION"

# Create virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    "$PYTHON" -m venv .venv
else
    echo "Virtual environment already exists, skipping creation."
fi

# Activate and install dependencies
echo "Installing dependencies..."
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements.txt --quiet

echo ""
echo "Setup complete. Launching TrenchChat..."
echo ""
exec .venv/bin/python main.py "$@"
