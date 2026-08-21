# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for TrenchChat.

Freezes main_flutter.py: the headless backend, the API server, and the
Flutter client launcher. The release workflow builds the Flutter client
first; the web build is collected here under flutter_web/, and the
platform's desktop bundle is staged into the frozen output afterwards as
flutter_client/ (see .github/workflows/release.yml). APP_VERSION in the
environment sets the macOS bundle version.

Build with:
    pyinstaller trenchchat.spec

Produces a onedir bundle in dist/TrenchChat/ that is then wrapped by the
platform-specific installer (Inno Setup / dpkg-deb / create-dmg).
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

REPO = Path(SPECPATH).resolve()
for p in (str(REPO), str(REPO / "devtools" / "testenv")):
    if p not in sys.path:
        sys.path.insert(0, p)

APP_VERSION = os.environ.get("APP_VERSION", "0.0.0")

# ---------------------------------------------------------------------------
# Hidden imports
#
# RNS uses `from RNS.Interfaces import *` on non-Android platforms, which
# PyInstaller cannot statically analyse. collect_submodules() walks the
# installed package tree and adds every submodule explicitly, which is the
# correct fix for wildcard-import packages. uvicorn and websockets select
# protocol implementations dynamically and need the same treatment. The
# audio stack (sounddevice/numpy/opuslib) is imported lazily behind runtime
# probes, so it must be named explicitly too.
# ---------------------------------------------------------------------------
hidden_imports = (
    collect_submodules("RNS")
    + collect_submodules("LXMF")
    + collect_submodules(
        "trenchchat", filter=lambda name: not name.startswith("trenchchat.gui"))
    + collect_submodules("uvicorn")
    + collect_submodules("websockets")
    + [
        # testenv backend modules main_flutter.py imports (via pathex)
        "api",
        "backend_core",
        # msgpack (may have C extension; include pure-Python fallback too)
        "msgpack",
        "msgpack.fallback",
        # voice audio stack, imported lazily behind availability probes
        "sounddevice",
        "numpy",
        "opuslib",
        # stdlib modules sometimes missed in frozen builds
        "sqlite3",
        "json",
        "pathlib",
        "hashlib",
        "hmac",
        "socket",
        "ssl",
        "threading",
        "queue",
        "logging",
    ]
)

# Collect any data files shipped with RNS/LXMF (e.g. vendor libs, config schemas)
datas = []
datas += collect_data_files("RNS")
datas += collect_data_files("LXMF")

web_build = REPO / "flutter_ui" / "build" / "web"
if web_build.is_dir():
    datas += [(str(web_build), "flutter_web")]
else:
    print("WARNING: flutter_ui/build/web missing -- web client not bundled")

# Voice libraries staged by CI (opus.dll / libopus.dylib); found at runtime
# by packaging/hooks/rthook_voice_libs.py. Empty outside CI.
binaries = []
voicelibs = REPO / "packaging" / "voicelibs"
if voicelibs.is_dir():
    binaries += [(str(f), ".") for f in voicelibs.iterdir() if f.is_file()]

# ---------------------------------------------------------------------------
# Platform-specific settings
# ---------------------------------------------------------------------------
is_windows = sys.platform == "win32"
is_macos = sys.platform == "darwin"

win_icon = REPO / "packaging" / "windows" / "assets" / "trenchchat.ico"
icon_path = str(win_icon) if is_windows and win_icon.is_file() else None

# No console window on GUI platforms; keep it on Linux for terminal users
no_console = is_windows or is_macos

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    ["main_flutter.py"],
    pathex=[".", "devtools/testenv"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=["packaging/hooks"],
    runtime_hooks=[
        "packaging/hooks/rthook_rns_interfaces.py",
        "packaging/hooks/rthook_voice_libs.py",
    ],
    excludes=[
        # Exclude heavy packages that are not used
        "tkinter",
        "PyQt6",
        "pydoc",
        "doctest",
        "ftplib",
        "imaplib",
        "poplib",
        "smtplib",
        "telnetlib",
        "nntplib",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TrenchChat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=not no_console,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="TrenchChat",
)

# macOS: also produce a .app bundle that create-dmg can consume
if is_macos:
    app = BUNDLE(
        coll,
        name="TrenchChat.app",
        icon=icon_path,
        bundle_identifier="com.trenchchat.app",
        info_plist={
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            "NSMicrophoneUsageDescription":
                "TrenchChat uses the microphone for voice chat.",
        },
    )
