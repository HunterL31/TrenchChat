#!/usr/bin/env bash
#
# Phase 0 spike: bundle the QUIC spike with PyInstaller and run the bundled client
# against an unbundled server, to see whether aioquic's compiled extensions survive
# a one-file build. Also prints the wheel matrix and license aioquic publishes on PyPI,
# which is what decides whether the dependency can ship on all three platforms.
#
#   ./pyinstaller_check.sh
#   PYTHON=/path/to/python ./pyinstaller_check.sh
#
# Run it once per platform. A one-file build is platform specific: passing here says
# nothing about Windows or macOS beyond what the wheel matrix says.

set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
PYTHON="${PYTHON:-$REPO/.venv/bin/python}"
WORK="${WORK:-/tmp/trenchchat-pyinstaller-spike}"
BINARY="$WORK/dist/tc-quic-spike"
PORT="${PORT:-45551}"

fail() { echo "pyinstaller_check.sh: $*" >&2; exit 1; }

[ -x "$PYTHON" ] || fail "no python at $PYTHON, set PYTHON="
"$PYTHON" -c "import PyInstaller" 2>/dev/null || fail "PyInstaller is not installed in $PYTHON"

echo "=== interpreter and package versions ==="
"$PYTHON" - <<'PY'
import platform
import aioquic
import cryptography
import PyInstaller
print(f"python        {platform.python_version()} on {platform.system()} {platform.machine()}")
print(f"aioquic       {aioquic.__version__}")
print(f"cryptography  {cryptography.__version__}")
print(f"pyinstaller   {PyInstaller.__version__}")
PY

echo
echo "=== one-file build ==="
rm -rf "$WORK"
mkdir -p "$WORK"
"$PYTHON" -m PyInstaller --onefile --noconfirm --clean \
    --name tc-quic-spike \
    --distpath "$WORK/dist" --workpath "$WORK/build" --specpath "$WORK" \
    "$HERE/quic_session.py" > "$WORK/build.log" 2>&1
status=$?
if [ "$status" != "0" ] || [ ! -x "$BINARY" ]; then
    tail -30 "$WORK/build.log"
    fail "the one-file build failed, full log in $WORK/build.log"
fi
echo "binary   $BINARY"
echo "size     $(du -h "$BINARY" | cut -f1)"
echo "warnings $(grep -ci 'WARNING' "$WORK/build.log" || true) lines in $WORK/build.log"

echo
echo "=== compiled extensions collected into the bundle ==="
"$PYTHON" - "$WORK/build/tc-quic-spike/PKG-00.toc" <<'PY'
import ast
import os
import sys

with open(sys.argv[1]) as handle:
    entries = ast.literal_eval(handle.read())[2]
wanted = ("aioquic", "pylsqpack", "cryptography", "_cffi", "ssl", "crypto")
for entry in entries:
    name, source, kind = entry[0], entry[1], entry[2]
    if kind not in ("BINARY", "EXTENSION"):
        continue
    if not any(key in name.lower() for key in wanted):
        continue
    size = os.path.getsize(source) // 1024 if source and os.path.exists(source) else 0
    print(f"  {kind:9} {name}  {size} KiB")
PY

echo
echo "=== bundled client against an unbundled server ==="
rm -rf "$WORK/run"
mkdir -p "$WORK/run"
"$PYTHON" "$HERE/quic_session.py" mint --peer-file "$WORK/run/client.json" > /dev/null
CLIENT_HASH="$("$PYTHON" -c "import json,sys;print(json.load(open(sys.argv[1]))['identity_hash'])" \
    "$WORK/run/client.json")"
"$PYTHON" "$HERE/quic_session.py" server --peer-file "$WORK/run/server.json" \
    --card "$WORK/run/server.card.json" --port "$PORT" --allow "$CLIENT_HASH" \
    --run-secs 120 > "$WORK/run/server.log" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null' EXIT
for _ in $(seq 1 50); do
    grep -q "listening " "$WORK/run/server.log" 2>/dev/null && break
    sleep 0.2
done
grep -q "listening " "$WORK/run/server.log" || {
    cat "$WORK/run/server.log"
    fail "server never listened"
}

"$BINARY" client --peer-file "$WORK/run/client.json" \
    --peer-card "$WORK/run/server.card.json" --bulk-bytes 10485760 --datagrams 50 \
    --out "$WORK/run/result.json"
client_status=$?
kill $SERVER_PID 2>/dev/null
echo "bundled client exit status: $client_status"

echo
echo "=== aioquic wheels and license on PyPI ==="
"$PYTHON" - <<'PY'
import json
import urllib.request

with urllib.request.urlopen("https://pypi.org/pypi/aioquic/json", timeout=30) as response:
    data = json.load(response)
version = data["info"]["version"]
print(f"latest release   {version}")
print(f"license          {data['info'].get('license_expression') or data['info'].get('license')}")
print(f"requires-python  {data['info'].get('requires_python')}")
print(f"dependencies     {', '.join(data['info'].get('requires_dist') or [])}")
print("wheels:")
for entry in sorted(data["releases"][version], key=lambda f: f["filename"]):
    if entry["packagetype"] == "bdist_wheel":
        print(f"  {entry['filename']}")
PY
