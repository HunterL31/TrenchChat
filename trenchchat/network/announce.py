"""
Reticulum announce handlers for channel discovery and peer reconnect detection.
"""

import RNS
import msgpack

from trenchchat import APP_NAME, APP_ASPECT_CHANNEL, APP_ASPECT_USER

# Path table index for the receiving interface (from RNS.Transport constants).
_IDX_PT_RVCD_IF = 5


def _parse_channel_app_data(app_data: bytes) -> dict:
    try:
        return msgpack.unpackb(app_data, raw=False)
    except Exception as e:
        RNS.log(f"TrenchChat: failed to parse channel app_data: {e}", RNS.LOG_DEBUG)
        return {}


def _receiving_interface_for(destination_hash: bytes):
    """Return the interface an announce was received on, or None.

    Looks up the RNS path table entry for destination_hash and returns the
    stored receiving interface object.  Returns None if the path is unknown
    or the interface is no longer present.
    """
    try:
        entry = RNS.Transport.path_table.get(destination_hash)
        if entry is not None:
            return entry[_IDX_PT_RVCD_IF]
    except Exception:
        pass
    return None


class ChannelAnnounceHandler:
    """
    Listens for announces from any trenchchat.channel.* destination
    and fires on_channel_discovered(channel_hash, identity, metadata, interface).
    The interface argument is the RNS interface the announce arrived on, or
    None if it could not be determined.
    """

    aspect_filter = f"{APP_NAME}.{APP_ASPECT_CHANNEL}"

    def __init__(self, on_channel_discovered):
        self._callback = on_channel_discovered

    def received_announce(self, destination_hash: bytes,
                          announced_identity: RNS.Identity,
                          app_data: bytes,
                          announce_packet_hash: bytes):
        metadata = _parse_channel_app_data(app_data) if app_data else {}
        iface = _receiving_interface_for(destination_hash)
        try:
            self._callback(destination_hash, announced_identity, metadata, iface)
        except Exception as e:
            RNS.log(f"TrenchChat: channel announce callback error: {e}", RNS.LOG_ERROR)


class PeerAnnounceHandler:
    """
    Listens for LXMF delivery-destination announces from any peer.
    Fires on_peer_appeared(identity_hash_hex, interface) so the sync manager
    can flush pending messages and request a gap sync for shared channels.
    The interface argument is the RNS interface the announce arrived on, or
    None if it could not be determined.
    """

    aspect_filter = "lxmf.delivery"

    def __init__(self, on_peer_appeared):
        self._callback = on_peer_appeared

    def received_announce(self, destination_hash: bytes,
                          announced_identity: RNS.Identity,
                          app_data: bytes,
                          announce_packet_hash: bytes):
        if announced_identity is None:
            return
        try:
            iface = _receiving_interface_for(destination_hash)
            self._callback(announced_identity.hash.hex(), iface)
        except Exception as e:
            RNS.log(f"TrenchChat: peer announce callback error: {e}", RNS.LOG_ERROR)


class UserAnnounceHandler:
    """
    Listens for trenchchat.user announces from TrenchChat peers.

    Fires on_user_discovered(identity_hash_hex, display_name) so the user
    directory can be populated with confirmed TrenchChat peers.  Only
    TrenchChat instances broadcast on this aspect, so the directory will
    not contain generic LXMF clients.
    """

    aspect_filter = f"{APP_NAME}.{APP_ASPECT_USER}"

    def __init__(self, on_user_discovered):
        self._callback = on_user_discovered

    def received_announce(self, destination_hash: bytes,
                          announced_identity: RNS.Identity,
                          app_data: bytes,
                          announce_packet_hash: bytes):
        if announced_identity is None:
            return
        display_name = ""
        if app_data:
            try:
                parsed = msgpack.unpackb(app_data, raw=False)
                if isinstance(parsed, dict):
                    name = parsed.get("name", "")
                    if isinstance(name, bytes):
                        name = name.decode(errors="replace")
                    display_name = str(name)
            except Exception:
                pass
        try:
            iface = _receiving_interface_for(destination_hash)
            self._callback(announced_identity.hash.hex(), display_name, iface)
        except Exception as e:
            RNS.log(f"TrenchChat: user announce callback error: {e}", RNS.LOG_ERROR)
