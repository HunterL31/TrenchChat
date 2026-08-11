#!/usr/bin/env bash
# Vendor the trenchchat/core subset that identity.py and storage.py need
# into python/app/trenchchat/, so serious_python can package it as part of
# the app bundle. Re-run after pulling upstream changes to those files.
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SPIKE_DIR/../.." && pwd)"
DEST="$SPIKE_DIR/python/app/trenchchat"

rm -rf "$DEST"
mkdir -p "$DEST/core"

cp "$REPO_ROOT/trenchchat/__init__.py" "$DEST/__init__.py"
cp "$REPO_ROOT/trenchchat/config.py" "$DEST/config.py"
cp "$REPO_ROOT/trenchchat/core/__init__.py" "$DEST/core/__init__.py"
cp "$REPO_ROOT/trenchchat/core/fileutils.py" "$DEST/core/fileutils.py"
cp "$REPO_ROOT/trenchchat/core/lockbox.py" "$DEST/core/lockbox.py"
cp "$REPO_ROOT/trenchchat/core/permissions.py" "$DEST/core/permissions.py"
cp "$REPO_ROOT/trenchchat/core/identity.py" "$DEST/core/identity.py"
cp "$REPO_ROOT/trenchchat/core/storage.py" "$DEST/core/storage.py"

echo "Vendored trenchchat core subset into $DEST"
