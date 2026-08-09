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

    aspect_filter is deliberately None (receive every announce on the
    network) rather than "trenchchat.channel". RNS.Transport dispatches
    announces by exact hash match: it computes
    hash_from_name_and_identity(aspect_filter, announced_identity) and only
    calls received_announce() if that equals the announce's own destination
    hash. A channel's real destination hash is derived from a *three*
    component aspect path -- "trenchchat.channel.<sanitised-name>", the
    channel name is part of what makes the hash unique per channel -- so a
    fixed two-component aspect_filter can never equal any real channel's
    hash; "trenchchat.channel" alone matches nothing, ever. There is no
    prefix/wildcard form of aspect_filter to express "any channel name", so
    the only way to actually receive these announces is to take everything
    and filter by app_data shape instead (below). This is not a strictly
    verified filter -- any node could shape-match a payload -- but channel
    discovery was never a trust boundary: the creator's identity and any
    channel content are independently verified elsewhere (signed member
    list documents, LXMF message signatures), not by the discovery
    mechanism itself.
    """

    aspect_filter = None

    def __init__(self, on_channel_discovered):
        self._callback = on_channel_discovered

    def received_announce(self, destination_hash: bytes,
                          announced_identity: RNS.Identity,
                          app_data: bytes,
                          announce_packet_hash: bytes):
        if not app_data:
            return
        metadata = _parse_channel_app_data(app_data)
        # Cheap shape check so the network-wide firehose (every RNS
        # announce, not just TrenchChat's) doesn't do real work -- storage
        # writes, callback dispatch -- for the vast majority of announces
        # that aren't ours.
        if not isinstance(metadata, dict) or "name" not in metadata or "access" not in metadata:
            return
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
