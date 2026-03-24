#!/usr/bin/env bash
# Build a .deb package from the PyInstaller onedir output.
#
# Usage:
#   bash packaging/linux/build-deb.sh <version>
#
# Expects:
#   dist/TrenchChat/   — PyInstaller onedir output (run pyinstaller trenchchat.spec first)
#
# Produces:
#   dist/trenchchat-<version>-amd64.deb
#
# User data at ~/.trenchchat/ is never referenced or modified.
# Installing over an existing version replaces /opt/trenchchat/ in-place
# because the control file declares Replaces: trenchchat.

set -euo pipefail

VERSION="${1:?Usage: $0 <version>}"
PKG_NAME="trenchchat"
ARCH="amd64"
INSTALL_ROOT="/opt/trenchchat"
STAGING="dist/deb-staging"
DEB_OUT="dist/${PKG_NAME}-${VERSION}-${ARCH}.deb"

echo "Building .deb: ${DEB_OUT}"

# --- Clean staging area ---
rm -rf "${STAGING}"

# --- Populate directory tree ---
# Application binaries -> /opt/trenchchat/
mkdir -p "${STAGING}${INSTALL_ROOT}"
cp -r dist/TrenchChat/. "${STAGING}${INSTALL_ROOT}/"
chmod +x "${STAGING}${INSTALL_ROOT}/TrenchChat"

# Symlink -> /usr/local/bin/trenchchat
mkdir -p "${STAGING}/usr/local/bin"
ln -sf "${INSTALL_ROOT}/TrenchChat" "${STAGING}/usr/local/bin/trenchchat"

# Desktop entry -> /usr/share/applications/
mkdir -p "${STAGING}/usr/share/applications"
cp packaging/linux/trenchchat.desktop "${STAGING}/usr/share/applications/"

# DEBIAN control files
mkdir -p "${STAGING}/DEBIAN"
# Inject the version into the control file
sed "s/^Version:.*/Version: ${VERSION}/" \
    packaging/linux/DEBIAN/control > "${STAGING}/DEBIAN/control"

# postinst: update desktop database so the launcher appears immediately
cat > "${STAGING}/DEBIAN/postinst" << 'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database > /dev/null 2>&1; then
    update-desktop-database /usr/share/applications
fi
EOF
chmod 0755 "${STAGING}/DEBIAN/postinst"

# prerm: nothing that touches ~/.trenchchat/
cat > "${STAGING}/DEBIAN/prerm" << 'EOF'
#!/bin/sh
# Intentionally empty — user data in ~/.trenchchat/ is preserved.
exit 0
EOF
chmod 0755 "${STAGING}/DEBIAN/prerm"

# Fix permissions: DEBIAN/ scripts must be owned by root in the final package
find "${STAGING}" -type d -exec chmod 0755 {} \;

# --- Build ---
dpkg-deb --root-owner-group --build "${STAGING}" "${DEB_OUT}"

echo "Done: ${DEB_OUT}"
