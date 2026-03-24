#!/usr/bin/env bash
# Build a macOS .dmg disk image from the PyInstaller .app bundle.
#
# Usage:
#   bash packaging/macos/build-dmg.sh <version>
#
# Expects:
#   dist/TrenchChat.app  — PyInstaller BUNDLE output (run pyinstaller trenchchat.spec first)
#   create-dmg           — installed via: brew install create-dmg
#
# Produces:
#   dist/TrenchChat-<version>-macos.dmg
#
# Upgrade path: the user drags TrenchChat.app to /Applications/, replacing the
# existing .app bundle. ~/.trenchchat/ is never touched.

set -euo pipefail

VERSION="${1:?Usage: $0 <version>}"
APP_BUNDLE="dist/TrenchChat.app"
DMG_OUT="dist/TrenchChat-${VERSION}-macos.dmg"

if [ ! -d "${APP_BUNDLE}" ]; then
    echo "Error: ${APP_BUNDLE} not found. Run pyinstaller trenchchat.spec first." >&2
    exit 1
fi

if ! command -v create-dmg > /dev/null 2>&1; then
    echo "Error: create-dmg not found. Install with: brew install create-dmg" >&2
    exit 1
fi

echo "Building .dmg: ${DMG_OUT}"

# Remove any previous attempt
rm -f "${DMG_OUT}"

create-dmg \
    --volname "TrenchChat ${VERSION}" \
    --volicon "${APP_BUNDLE}/Contents/Resources/trenchchat.icns" \
    --window-pos 200 120 \
    --window-size 600 400 \
    --icon-size 100 \
    --icon "TrenchChat.app" 150 185 \
    --hide-extension "TrenchChat.app" \
    --app-drop-link 450 185 \
    --no-internet-enable \
    "${DMG_OUT}" \
    "${APP_BUNDLE}"

echo "Done: ${DMG_OUT}"
