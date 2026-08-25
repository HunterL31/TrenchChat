"""
Reticulum announce handlers for channel discovery, peer reconnect detection,
finding the propagation nodes offline direct messages can be left with, and
answering a peer we have just met so they can hear us back.
"""

import threading
import time

import RNS
import msgpack

from trenchchat import APP_NAME, APP_ASPECT_CHANNEL, APP_ASPECT_USER
from trenchchat.core.protocol import unpack_wire

# Path table index for the receiving interface (from RNS.Transport constants).
_IDX_PT_RVCD_IF = 5


def _parse_channel_app_data(app_data: bytes) -> dict:
    try:
        return unpack_wire(app_data)
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
                parsed = unpack_wire(app_data)
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


class PropagationAnnounceHandler:
    """
    Listens for LXMF propagation node announces and reports each one with the
    number of hops to it, so a client can pick the nearest node to hand offline
    direct messages to (see core/propagation.py).

    LXMF registers its own handler on this aspect for its propagation-node
    mode; this one is additive and read-only, and does not disturb it.
    """

    aspect_filter = "lxmf.propagation"

    def __init__(self, on_node_heard):
        self._callback = on_node_heard

    def received_announce(self, destination_hash: bytes,
                          announced_identity: RNS.Identity,
                          app_data: bytes,
                          announce_packet_hash: bytes):
        if announced_identity is None:
            return
        try:
            hops = RNS.Transport.hops_to(destination_hash)
        except Exception:
            hops = 0
        try:
            self._callback(destination_hash.hex(), hops)
        except Exception as e:
            RNS.log(f"TrenchChat: propagation announce callback error: {e}",
                    RNS.LOG_ERROR)


class PathResponseHandler:
    """
    Notices an LXMF peer's identity arriving as a *path response* rather than a
    live announce, and fires on_identity_resolved(identity_hash_hex).

    This is what rescues a first message from a peer we have never heard.
    Router quarantines such a message -- LXMF cannot verify a signature against
    an identity it cannot recall -- and requests a path for its source. The
    path response teaches RNS the identity, but RNS only calls announce
    handlers for it when they ask, so without this the held message sits until
    it expires, and the peer looks like it never wrote.

    Deliberately separate from PeerAnnounceHandler rather than setting
    receive_path_responses on it: that handler drives SyncManager's fan-out
    across every shared channel, which is far too much work to repeat on every
    path response the stack happens to receive.
    """

    aspect_filter = "lxmf.delivery"
    receive_path_responses = True

    def __init__(self, on_identity_resolved):
        self._callback = on_identity_resolved

    def received_announce(self, destination_hash: bytes,
                          announced_identity: RNS.Identity,
                          app_data: bytes,
                          announce_packet_hash: bytes):
        if announced_identity is None:
            return
        try:
            self._callback(announced_identity.hash.hex())
        except Exception as e:
            RNS.log(f"TrenchChat: path response callback error: {e}", RNS.LOG_ERROR)


# How long to wait before answering, so meeting several peers at once costs one
# announce rather than one each.
FIRST_CONTACT_COALESCE_SECS = 2.0

# Peers remembered as already answered. Identities are free to mint, so this
# cannot grow without bound; answering an evicted peer a second time is
# harmless.
MAX_ANSWERED_PEERS = 512


class FirstContactAnnouncer:
    """Announces once when we first hear a peer, so they can hear us back.

    A peer that has never heard our announce cannot recall our identity, so
    LXMF cannot verify anything we send them: our first message is quarantined
    at their end and dropped when it expires. Re-announcing every 15 minutes is
    frugal with airtime and useless for meeting somebody -- and answering
    *every* announce instead would leave two idle clients replying to each
    other's replies for ever.

    Answering only the first time we hear a given peer settles after exactly
    two announces: they hear us, we are no longer new to them, and it stops.
    """

    def __init__(self, router, channel_mgr, self_hex: str,
                 coalesce_secs: float = FIRST_CONTACT_COALESCE_SECS,
                 max_answered: int = MAX_ANSWERED_PEERS) -> None:
        self._router = router
        self._channel_mgr = channel_mgr
        self._self_hex = self_hex
        self._coalesce = coalesce_secs
        self._max_answered = max_answered
        self._lock = threading.Lock()
        # peer hex -> when we last heard them, for bounded eviction
        self._answered: dict[str, float] = {}
        self._pending_since: float | None = None
        self._pending_iface = None
        self._pending_count = 0

    def note_peer(self, peer_hex: str, iface=None, now: float | None = None) -> bool:
        """Record a peer we have heard. True if this queued an announce.

        Safe to call from an announce handler thread.
        """
        if not peer_hex or peer_hex == self._self_hex:
            return False
        now = time.time() if now is None else now
        with self._lock:
            if peer_hex in self._answered:
                self._answered[peer_hex] = now
                return False
            if len(self._answered) >= self._max_answered:
                oldest = min(self._answered, key=self._answered.get)
                del self._answered[oldest]
            self._answered[peer_hex] = now

            if self._pending_since is None:
                self._pending_since = now
                self._pending_iface = iface
            elif iface is not self._pending_iface:
                # Two interfaces cannot be targeted at once, so fall back to
                # announcing on all of them, as main_window.py does.
                self._pending_iface = None
            self._pending_count += 1
        return True

    def tick(self, now: float | None = None) -> bool:
        """Send a queued announce once it has had time to coalesce."""
        now = time.time() if now is None else now
        with self._lock:
            if self._pending_since is None or now - self._pending_since < self._coalesce:
                return False
            iface, count = self._pending_iface, self._pending_count
            self._pending_since = None
            self._pending_iface = None
            self._pending_count = 0

        self._announce(iface)
        RNS.log(
            f"TrenchChat: announced after meeting {count} new peer(s)"
            + (f" on {iface}" if iface is not None else ""),
            RNS.LOG_DEBUG,
        )
        return True

    def _announce(self, iface) -> None:
        try:
            self._router.announce(attached_interface=iface)
            self._router.announce_user(attached_interface=iface)
            if self._channel_mgr is not None:
                self._channel_mgr.announce_all_owned(attached_interface=iface)
        except Exception as e:
            RNS.log(f"TrenchChat: first-contact announce failed: {e}", RNS.LOG_WARNING)
