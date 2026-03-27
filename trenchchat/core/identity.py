"""
Thin wrapper around RNS.Identity.

The keypair is persisted to ~/.trenchchat/identity so the identity hash
stays stable across restarts.  On first launch the file is created; on
subsequent launches it is loaded from disk.

When a PIN lock is active the private key bytes are Fernet-encrypted at
rest.  The 32-byte raw key derived from the PIN must be supplied as
``encryption_key``.  When no key is supplied (no PIN set) the file is stored
as plain binary, preserving backward compatibility.

We enforce owner-only permissions (0o600 on POSIX) both when creating a new
file and when loading an existing one, so that installations predating this
change are hardened automatically on next launch.
"""

import RNS
import msgpack

from trenchchat import APP_NAME
from trenchchat.config import Config, DATA_DIR
from trenchchat.core.fileutils import secure_file
from trenchchat.core.lockbox import encrypt_bytes, decrypt_bytes

# The aspect used to derive TrenchChat's stable delivery destination.
_DELIVERY_ASPECT = "delivery"

_IDENTITY_PATH = DATA_DIR / "identity"


def _load_identity(path, encryption_key: bytes | None) -> RNS.Identity:
    """Load an RNS.Identity from *path*, decrypting if a key is provided.

    When *encryption_key* is None the file is read as raw key material
    (legacy / no PIN mode).  When a key is provided the file is Fernet-
    decrypted first, then the resulting 64-byte blob is loaded via
    ``load_private_key``.
    """
    raw = path.read_bytes()
    if encryption_key is not None:
        raw = decrypt_bytes(raw, encryption_key)
    identity = RNS.Identity()
    identity.load_private_key(raw)
    return identity


def _save_identity(identity: RNS.Identity, path, encryption_key: bytes | None) -> None:
    """Persist an RNS.Identity to *path*, encrypting if a key is provided."""
    raw = identity.get_private_key()
    if encryption_key is not None:
        raw = encrypt_bytes(raw, encryption_key)
    path.write_bytes(raw)
    secure_file(path)


class Identity:
    """Wrapper around RNS.Identity that handles persistence and optional PIN encryption."""

    def __init__(self, config: Config, identity_path=None,
                 encryption_key: bytes | None = None):
        """Initialise the identity, loading or creating the key file.

        *encryption_key* is the 32-byte raw key derived from the user's PIN.
        Pass None when no PIN lock is active (unencrypted mode).
        """
        self._config = config
        self._encryption_key = encryption_key
        path = identity_path or _IDENTITY_PATH

        # RNS must already be initialised before this is constructed.
        if path.exists():
            self._identity: RNS.Identity = _load_identity(path, encryption_key)
            # Re-secure permissions on existing files (hardens pre-PIN installs).
            secure_file(path)
        else:
            self._identity = RNS.Identity()
            _save_identity(self._identity, path, encryption_key)

        self._destination: RNS.Destination = RNS.Destination(
            self._identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            APP_NAME,
            _DELIVERY_ASPECT,
        )

    @property
    def rns_identity(self) -> RNS.Identity:
        """The underlying RNS.Identity instance."""
        return self._identity

    @property
    def destination(self) -> RNS.Destination:
        """The inbound LXMF delivery destination for this identity."""
        return self._destination

    @property
    def hash(self) -> bytes:
        """The identity hash bytes."""
        return self._identity.hash

    @property
    def hash_hex(self) -> str:
        """The identity hash as a lowercase hex string."""
        return self._identity.hash.hex()

    @property
    def display_name(self) -> str:
        """The configured display name."""
        return self._config.display_name

    @display_name.setter
    def display_name(self, value: str):
        self._config.display_name = value

    def announce_data(self) -> bytes:
        """Serialised app_data payload included in announces."""
        return msgpack.packb({"display_name": self.display_name}, use_bin_type=True)

    def reencrypt(self, identity_path=None, *,
                  old_key: bytes | None, new_key: bytes | None) -> None:
        """Re-encrypt (or decrypt) the identity file with a new key.

        Used when the user sets, changes, or removes their PIN.  Pass
        *new_key=None* to strip encryption; pass *old_key=None* when
        transitioning from a plain file to an encrypted one.
        """
        path = identity_path or _IDENTITY_PATH
        _save_identity(self._identity, path, new_key)
        self._encryption_key = new_key
        RNS.log("TrenchChat [identity]: identity file re-encrypted", RNS.LOG_DEBUG)
