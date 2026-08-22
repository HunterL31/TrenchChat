"""
Propagation filter: decides whether an inbound LXMF message should be
stored by the local propagation node based on its channel hash.

Field 0x01 in TrenchChat LXMF messages carries the channel hash (bytes).

The decision has to be applied where LXMF takes messages *into the
propagation store*, not in its delivery callback: a delivery callback's
return value is ignored, so a verdict returned there filtered nothing while
the Settings UI presented it as controlling what this node relays for other
people. Router.enable_propagation wraps LXMRouter.lxmf_propagation with
allows_packed for that reason.
"""

import LXMF
import RNS

from trenchchat.config import Config
from trenchchat.core.protocol import F_CHANNEL_HASH


class PropagationFilter:
    def __init__(self, config: Config):
        self._config = config

    def relays_nothing(self) -> bool:
        """True when the current filter can never store anything.

        Allowlist mode with an empty allowlist rejects every message, so a
        node in this state relays nothing for anyone -- worth warning about,
        since it is also the default and silent otherwise.
        """
        return (self._config.channel_filter_mode == "allowlist"
                and not self._config.channel_filter_hashes)

    def allows(self, message) -> bool:
        """Return True if this message should be stored by the propagation node."""
        mode = self._config.channel_filter_mode

        if mode == "all":
            return True

        # allowlist mode: check fields[0x01] against configured hashes
        fields = getattr(message, "fields", None) or {}
        channel_hash_bytes = fields.get(F_CHANNEL_HASH)

        if not channel_hash_bytes:
            return False

        if isinstance(channel_hash_bytes, bytes):
            channel_hex = channel_hash_bytes.hex()
        else:
            channel_hex = str(channel_hash_bytes)

        return channel_hex in self._config.channel_filter_hashes

    def allows_packed(self, lxmf_data: bytes) -> bool:
        """Same decision, made from a message's packed bytes.

        Fails closed on anything that will not unpack: allowlist mode means
        "relay these channels", and bytes we cannot read are not one of them.
        """
        if self._config.channel_filter_mode == "all":
            return True
        try:
            message = LXMF.LXMessage.unpack_from_bytes(lxmf_data)
        except Exception as e:
            RNS.log(
                f"TrenchChat [propagation]: refusing an unreadable message "
                f"under an allowlist filter: {e}",
                RNS.LOG_DEBUG,
            )
            return False
        return self.allows(message)
