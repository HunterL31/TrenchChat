"""
Propagation filter: decides whether an inbound LXMF message should be
stored by the local propagation node based on its channel hash.

Field 0x01 in TrenchChat LXMF messages carries the channel hash (bytes).
"""

from trenchchat.config import Config

# LXMF fields key for channel hash
FIELD_CHANNEL_HASH = 0x01


class PropagationFilter:
    def __init__(self, config: Config):
        self._config = config

    def allows(self, message) -> bool:
        """Return True if this message should be stored by the propagation node."""
        mode = self._config.channel_filter_mode

        if mode == "all":
            return True

        # allowlist mode: check fields[0x01] against configured hashes
        fields = getattr(message, "fields", None) or {}
        channel_hash_bytes = fields.get(FIELD_CHANNEL_HASH)

        if not channel_hash_bytes:
            return False

        if isinstance(channel_hash_bytes, bytes):
            channel_hex = channel_hash_bytes.hex()
        else:
            channel_hex = str(channel_hash_bytes)

        return channel_hex in self._config.channel_filter_hashes
