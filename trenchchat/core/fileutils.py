"""
Filesystem utility helpers shared across TrenchChat's core modules.
"""

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import RNS

# Owner read+write only: no group or other access.
OWNER_RW_MODE = 0o600

# Longest file name kept after cleaning. A name is a label chosen by whoever
# sent it, so it is bounded like any other inbound string.
MAX_FILENAME_CHARS = 128


def clean_filename(value, max_len: int = MAX_FILENAME_CHARS) -> str | None:
    """A remote-supplied name reduced to a bare, printable basename.

    The name is chosen by a peer and ends up in a Content-Disposition header
    and a save dialog, so nothing that could steer a path or a header
    survives. None when nothing usable is left.
    """
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return None
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    value = "".join(c for c in value
                    if c.isprintable() and c not in '"\\')
    value = value.strip().strip(".")
    return value[:max_len] or None


def _secure_file_windows(path: Path) -> None:
    """Restrict a file's ACL to the current user.

    os.chmod on Windows only toggles the read-only attribute, so it cannot
    restrict access at all. icacls avoids a pywin32 dependency: /inheritance:r
    drops inherited entries, /grant:r replaces the rest with this user only.
    """
    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    principal = f"{domain}\\{user}" if domain and user else user
    if not principal:
        RNS.log(
            f"TrenchChat: cannot determine current user to secure {path}",
            RNS.LOG_WARNING,
        )
        return

    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:F"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        RNS.log(
            f"TrenchChat: could not restrict ACL on {path}: "
            f"{result.stderr.strip() or result.stdout.strip()}",
            RNS.LOG_WARNING,
        )


def secure_file(path: Path) -> None:
    """Enforce owner-only access on a sensitive file.

    POSIX gets mode 0o600; Windows gets an ACL restricted to the current user
    (see _secure_file_windows).

    If the operation fails for any reason (e.g. the file lives on a
    filesystem that does not support permissions) the error is logged as a
    warning and silently ignored, a permission failure must never prevent
    the application from starting.
    """
    try:
        if os.name == "nt":
            _secure_file_windows(path)
        else:
            os.chmod(path, OWNER_RW_MODE)
    except (OSError, ValueError) as e:
        RNS.log(
            f"TrenchChat: could not set permissions on {path}: {e}",
            RNS.LOG_WARNING,
        )


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically, restrictive from the moment of creation.

    A plain write_bytes truncates in place (a failed write destroys the
    existing file) and creates at the process umask, leaving a window in which
    the file is world-readable.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        try:
            os.fchmod(fd, OWNER_RW_MODE)
        except (AttributeError, OSError):
            # os.fchmod is POSIX-only; Windows is handled by secure_file below.
            pass
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        secure_file(tmp_path)
        os.replace(str(tmp_path), str(path))
    except BaseException:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise

    secure_file(path)
