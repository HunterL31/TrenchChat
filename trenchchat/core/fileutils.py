"""
Filesystem utility helpers shared across TrenchChat's core modules.
"""

import os
import stat
from pathlib import Path

import RNS

# Owner read+write only — no group or other access.
OWNER_RW_MODE = 0o600


def secure_file(path: Path) -> None:
    """Enforce owner-only permissions on a sensitive file.

    On POSIX systems (Linux, macOS) this sets mode 0o600.  On Windows the
    POSIX chmod call is a no-op for group/other bits, so we at minimum strip
    the read-only flag; broader ACL protection relies on the home-directory
    permissions set by Windows.  A full ACL manipulation would require
    pywin32, which is not a declared dependency.

    If the operation fails for any reason (e.g. the file lives on a
    filesystem that does not support permissions) the error is logged as a
    warning and silently ignored — a permission failure must never prevent
    the application from starting.
    """
    try:
        if os.name == "nt":
            current = os.stat(path).st_mode
            os.chmod(path, current | stat.S_IWRITE)
        else:
            os.chmod(path, OWNER_RW_MODE)
    except OSError as e:
        RNS.log(
            f"TrenchChat: could not set permissions on {path}: {e}",
            RNS.LOG_WARNING,
        )
