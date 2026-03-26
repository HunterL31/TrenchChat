"""
Thin wrapper around RNS.Identity.

The keypair is persisted to ~/.trenchchat/identity so the identity hash
stays stable across restarts.  On first launch the file is created; on
subsequent launches it is loaded from disk.

The identity file contains the raw private key material.  We enforce
owner-only permissions (0o600 on POSIX) both when creating a new file and
when loading an existing one, so that installations predating this change
are hardened automatically on next launch.
"""

import os
import stat
from pathlib import Path

import RNS
import msgpack

from trenchchat import APP_NAME
from trenchchat.config import Config, DATA_DIR

# The aspect used to derive TrenchChat's stable delivery destination.
_DELIVERY_ASPECT = "delivery"

_IDENTITY_PATH = DATA_DIR / "identity"

# Owner read+write only — no group or other access.
_IDENTITY_FILE_MODE = 0o600


def _secure_identity_file(path: Path) -> None:
    """Enforce owner-only permissions on the identity file.

    On POSIX systems (Linux, macOS) this sets mode 0o600.  On Windows the
    POSIX chmod call is a no-op for group/other bits, so we additionally
    strip the read-only ACL entries using the standard ``stat`` module
    approach that works without third-party dependencies.  If the platform
    does not support the operation we log a warning and continue — a
    permission failure must never prevent the application from starting.
    """
    try:
        if os.name == "nt":
            # On Windows, remove the read-only flag and rely on the user's
            # home-directory ACL for broader protection.  A full ACL
            # manipulation would require pywin32 which is not a declared
            # dependency; the chmod below at least removes the read-only bit.
            current = os.stat(path).st_mode
            os.chmod(path, current | stat.S_IWRITE)
        else:
            os.chmod(path, _IDENTITY_FILE_MODE)
    except OSError as e:
        RNS.log(
            f"TrenchChat: could not set permissions on identity file {path}: {e}",
            RNS.LOG_WARNING,
        )


class Identity:
    def __init__(self, config: Config, identity_path=None):
        self._config = config
        path = identity_path or _IDENTITY_PATH
        # RNS must already be initialised before this is constructed.
        if path.exists():
            self._identity: RNS.Identity = RNS.Identity.from_file(str(path))
            # Harden existing installations that predate permission enforcement.
            _secure_identity_file(path)
        else:
            self._identity = RNS.Identity()
            self._identity.to_file(str(path))
            _secure_identity_file(path)
        self._destination: RNS.Destination = RNS.Destination(
            self._identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            APP_NAME,
            _DELIVERY_ASPECT,
        )

    @property
    def rns_identity(self) -> RNS.Identity:
        return self._identity

    @property
    def destination(self) -> RNS.Destination:
        return self._destination

    @property
    def hash(self) -> bytes:
        return self._identity.hash

    @property
    def hash_hex(self) -> str:
        return self._identity.hash.hex()

    @property
    def display_name(self) -> str:
        return self._config.display_name

    @display_name.setter
    def display_name(self, value: str):
        self._config.display_name = value

    def announce_data(self) -> bytes:
        """Serialised app_data payload included in announces."""
        return msgpack.packb({"display_name": self.display_name}, use_bin_type=True)
