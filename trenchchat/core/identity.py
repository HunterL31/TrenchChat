"""
Thin wrapper around RNS.Identity.

TrenchChat does not manage its own key files. Reticulum owns the identity.
We create a persistent named destination so the identity hash stays stable
across restarts (Reticulum stores the keypair in its own keystore).
"""

import RNS

from trenchchat import APP_NAME
from trenchchat.config import Config

# The aspect used to derive TrenchChat's stable delivery destination.
_DELIVERY_ASPECT = "delivery"


class Identity:
    def __init__(self, config: Config):
        self._config = config
        # RNS must already be initialised before this is constructed.
        self._identity: RNS.Identity = RNS.Identity()
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
