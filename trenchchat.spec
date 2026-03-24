# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for TrenchChat.

Build with:
    pyinstaller trenchchat.spec

Produces a onedir bundle in dist/TrenchChat/ that is then wrapped by the
platform-specific installer (Inno Setup / dpkg-deb / create-dmg).
"""

import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ---------------------------------------------------------------------------
# Hidden imports
#
# RNS uses `from RNS.Interfaces import *` on non-Android platforms, which
# PyInstaller cannot statically analyse. collect_submodules() walks the
# installed package tree and adds every submodule explicitly, which is the
# correct fix for wildcard-import packages.
#
# The manual list below covers msgpack and stdlib modules that are
# occasionally missed in frozen builds.
# ---------------------------------------------------------------------------
hidden_imports = (
    collect_submodules("RNS")
    + collect_submodules("LXMF")
    + [
        # msgpack (may have C extension; include pure-Python fallback too)
        "msgpack",
        "msgpack.fallback",
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

# ---------------------------------------------------------------------------
# Platform-specific settings
# ---------------------------------------------------------------------------
is_windows = sys.platform == "win32"
is_macos = sys.platform == "darwin"

icon_path = None

# No console window on GUI platforms; keep it on Linux for terminal users
no_console = is_windows or is_macos

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=["packaging/hooks"],
    hooksconfig={
        "PyQt6": {
            "include_plugins": [
                "platforms",
                "styles",
                "imageformats",
                "iconengines",
                "platformthemes",
                "xcbglintegrations",
            ],
        },
    },
    runtime_hooks=["packaging/hooks/rthook_rns_interfaces.py"],
    excludes=[
        # Exclude heavy packages that are not used
        "tkinter",
        "unittest",
        "email",
        "html",
        "http",
        "xmlrpc",
        "pydoc",
        "doctest",
        "difflib",
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
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "0.1.0",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
        },
    )
