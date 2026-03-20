"""
Reticulum announce handlers for channel discovery and peer reconnect detection.
"""

import RNS
import msgpack

from trenchchat import APP_NAME, APP_ASPECT_CHANNEL


def _parse_channel_app_data(app_data: bytes) -> dict:
    try:
        return msgpack.unpackb(app_data, raw=False)
    except Exception:
        return {}


class ChannelAnnounceHandler:
    """
    Listens for announces from any trenchchat.channel.* destination
    and fires on_channel_discovered(channel_hash, identity, metadata).
    """

    aspect_filter = f"{APP_NAME}.{APP_ASPECT_CHANNEL}"

    def __init__(self, on_channel_discovered):
        self._callback = on_channel_discovered

    def received_announce(self, destination_hash: bytes,
                          announced_identity: RNS.Identity,
                          app_data: bytes):
        metadata = _parse_channel_app_data(app_data) if app_data else {}
        try:
            self._callback(destination_hash, announced_identity, metadata)
        except Exception as e:
            RNS.log(f"TrenchChat: channel announce callback error: {e}", RNS.LOG_ERROR)


class PeerAnnounceHandler:
    """
    Listens for LXMF delivery-destination announces from any peer.
    Fires on_peer_appeared(identity_hash_hex) so the sync manager can
    flush pending messages and request a gap sync for shared channels.
    """

    aspect_filter = "lxmf.delivery"

    def __init__(self, on_peer_appeared):
        self._callback = on_peer_appeared

    def received_announce(self, destination_hash: bytes,
                          announced_identity: RNS.Identity,
                          app_data: bytes):
        if announced_identity is None:
            return
        try:
            self._callback(announced_identity.hash.hex())
        except Exception as e:
            RNS.log(f"TrenchChat: peer announce callback error: {e}", RNS.LOG_ERROR)
