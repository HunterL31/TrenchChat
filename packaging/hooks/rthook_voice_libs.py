"""
Runtime hook: let ctypes find the voice libraries bundled with the app.

opuslib loads libopus via ctypes.util.find_library, which only searches
system locations. When frozen, check the bundle directory first so the
opus.dll / libopus.dylib shipped in the installer is found without a
system-wide install. Linux is untouched: the .deb declares libopus0 in
Depends and the system search finds it.
"""

import os
import sys

if getattr(sys, "frozen", False):
    import ctypes.util

    _bundle_dir = getattr(sys, "_MEIPASS", "")
    _orig_find_library = ctypes.util.find_library

    def _find_library(name):
        for filename in (f"lib{name}.dylib", f"{name}.dll", f"lib{name}.so"):
            candidate = os.path.join(_bundle_dir, filename)
            if os.path.isfile(candidate):
                return candidate
        return _orig_find_library(name)

    ctypes.util.find_library = _find_library
