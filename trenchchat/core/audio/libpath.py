"""
Point ctypes at voice libraries dropped into the repo.

opuslib resolves libopus with ctypes.util.find_library at import time,
which only searches system locations. The frozen app ships the library
and hooks the search (packaging/hooks/rthook_voice_libs.py); a source
checkout gets the same by placing the library in packaging/voicelibs/ —
the one workable spot on Windows, which has no package manager to
install libopus from.
"""

import ctypes.util
from pathlib import Path

_DEFAULT_LIB_DIR = Path(__file__).resolve().parents[3] / "packaging" / "voicelibs"
_hooked_dirs: set[Path] = set()


def ensure_voice_libs_findable(lib_dir: Path | None = None) -> None:
    """Extend ctypes.util.find_library to check the repo's voicelibs dir.

    Must run before opuslib is imported — it binds find_library's result
    once, at its own import. A missing directory is a no-op; hooking the
    same directory twice is too.
    """
    lib_dir = (lib_dir or _DEFAULT_LIB_DIR).resolve()
    if lib_dir in _hooked_dirs or not lib_dir.is_dir():
        return
    _hooked_dirs.add(lib_dir)
    original = ctypes.util.find_library

    def find_library(name):
        for filename in (f"{name}.dll", f"lib{name}.dll", f"lib{name}-0.dll",
                         f"lib{name}.dylib", f"lib{name}.so"):
            candidate = lib_dir / filename
            if candidate.is_file():
                return str(candidate)
        return original(name)

    ctypes.util.find_library = find_library
