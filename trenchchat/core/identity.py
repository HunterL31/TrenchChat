"""
Thin wrapper around RNS.Identity.

The keypair is persisted to ~/.trenchchat/identity so the identity hash
stays stable across restarts.  On first launch the file is created; on
subsequent launches it is loaded from disk.
"""

import RNS

from trenchchat import APP_NAME
from trenchchat.config import Config, DATA_DIR

# The aspect used to derive TrenchChat's stable delivery destination.
_DELIVERY_ASPECT = "delivery"

_IDENTITY_PATH = DATA_DIR / "identity"


class Identity:
    def __init__(self, config: Config, identity_path=None):
        self._config = config
        path = identity_path or _IDENTITY_PATH
        # RNS must already be initialised before this is constructed.
        if path.exists():
            self._identity: RNS.Identity = RNS.Identity.from_file(str(path))
        else:
            self._identity = RNS.Identity()
            self._identity.to_file(str(path))
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
        import msgpack
        return msgpack.packb({"display_name": self.display_name}, use_bin_type=True)
