"""
Reticulum announce handlers for channel discovery, peer reconnect detection,
finding the propagation nodes offline direct messages can be left with, and
answering a peer we have just met so they can hear us back.
"""

import threading
import time

import LXMF
import RNS
import msgpack

from trenchchat import APP_NAME, APP_ASPECT_USER
from trenchchat.core.protocol import unpack_wire
from trenchchat.core.rrc_wire import (
    HUB_APP_NAME as RRC_HUB_APP_NAME, HUB_ASPECT as RRC_HUB_ASPECT,
    MAX_HUB_NAME_BYTES,
)

# Path table index for the receiving interface (from RNS.Transport constants).
_IDX_PT_RVCD_IF = 5


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


class NodeAnnounceHandler:
    """
    Listens for Nomad Network node announces (nomadnetwork.node).

    Fires on_node_discovered(node_hash_hex, display_name, interface).
    node_hash_hex is the node's *destination* hash, what a page browser
    dials, not the identity hash. app_data is the unsigned node name:
    presentation only, never authority.
    """

    aspect_filter = "nomadnetwork.node"

    def __init__(self, on_node_discovered):
        self._callback = on_node_discovered

    def received_announce(self, destination_hash: bytes,
                          announced_identity: RNS.Identity,
                          app_data: bytes,
                          announce_packet_hash: bytes):
        if announced_identity is None:
            return
        display_name = ""
        if app_data:
            try:
                decoded = app_data.decode("utf-8", errors="replace")
                display_name = "".join(
                    c for c in decoded if c.isprintable())[:64]
            except Exception:
                pass
        try:
            iface = _receiving_interface_for(destination_hash)
            self._callback(destination_hash.hex(), display_name, iface)
        except Exception as e:
            RNS.log(f"TrenchChat: node announce callback error: {e}", RNS.LOG_ERROR)


class HubAnnounceHandler:
    """
    Listens for RRC hub announces (rrc.hub).

    Fires on_hub_discovered(hub_hash_hex, hub_name, interface). The hash is
    the hub's *destination* hash, which is what a client dials. The name is
    whatever the hub put in its app_data: unsigned, unverified, presentation
    only, and read defensively because the specification fixes the aspect
    but not the payload.
    """

    aspect_filter = f"{RRC_HUB_APP_NAME}.{RRC_HUB_ASPECT}"

    def __init__(self, on_hub_discovered):
        self._callback = on_hub_discovered

    def received_announce(self, destination_hash: bytes,
                          announced_identity: RNS.Identity,
                          app_data: bytes,
                          announce_packet_hash: bytes):
        if announced_identity is None:
            return
        try:
            iface = _receiving_interface_for(destination_hash)
            self._callback(destination_hash.hex(), _hub_name(app_data), iface)
        except Exception as e:
            RNS.log(f"TrenchChat [rrc]: hub announce callback error: {e}",
                    RNS.LOG_ERROR)


def _hub_name(app_data: bytes) -> str:
    """The display name from a hub announce, or "" if it carries none.

    rrcd has announced a plain UTF-8 name and a msgpack map at different
    points, so both are read and anything else is simply no name.
    """
    if not app_data:
        return ""
    try:
        decoded = msgpack.unpackb(app_data, raw=False)
        if isinstance(decoded, dict):
            decoded = decoded.get("name", "")
        if isinstance(decoded, bytes):
            decoded = decoded.decode("utf-8", errors="replace")
        if isinstance(decoded, str):
            return _printable(decoded)
    except Exception:
        pass
    try:
        return _printable(app_data.decode("utf-8", errors="replace"))
    except Exception:
        return ""


def _printable(value: str) -> str:
    return "".join(c for c in value if c.isprintable())[:MAX_HUB_NAME_BYTES]


def lxmf_display_name(identity_hash: bytes) -> str:
    """Name a peer last announced on lxmf.delivery, or "" if none is known.

    Parsed with LXMF's own reader, so the name is read exactly as Sideband and
    MeshChat read it. RNS remembers announce app_data before dispatching
    handlers, so this is current from inside an announce callback.
    """
    delivery_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
    try:
        name = LXMF.display_name_from_app_data(RNS.Identity.recall_app_data(delivery_hash))
    except Exception as e:
        RNS.log(f"TrenchChat: unreadable lxmf.delivery app_data from "
                f"{identity_hash.hex()[:12]}…: {e}", RNS.LOG_DEBUG)
        return ""
    return name or ""


class UserAnnounceHandler:
    """
    Listens for trenchchat.user announces from TrenchChat peers.

    Fires on_user_discovered(identity_hash_hex, display_name) so the user
    directory can be populated with confirmed TrenchChat peers.  Only
    TrenchChat instances broadcast on this aspect, so the directory will
    not contain generic LXMF clients.

    The announce carries no payload: it is the aspect that says "this identity
    runs TrenchChat". The display name comes from the peer's lxmf.delivery
    announce, the same place every other LXMF client publishes it. Peers that
    predate this still send a name in the app_data, honoured only when no
    delivery announce has been heard yet.
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
        display_name = lxmf_display_name(announced_identity.hash)
        if not display_name and app_data:
            display_name = _legacy_user_announce_name(app_data)
        try:
            iface = _receiving_interface_for(destination_hash)
            self._callback(announced_identity.hash.hex(), display_name, iface)
        except Exception as e:
            RNS.log(f"TrenchChat: user announce callback error: {e}", RNS.LOG_ERROR)


def _legacy_user_announce_name(app_data: bytes) -> str:
    try:
        parsed = unpack_wire(app_data)
    except Exception:
        return ""
    if not isinstance(parsed, dict):
        return ""
    name = parsed.get("name", "")
    if isinstance(name, bytes):
        name = name.decode(errors="replace")
    return str(name)


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
    at their end and dropped when it expires. The periodic re-announce is hours
    apart, frugal with airtime and useless for meeting somebody -- and answering
    *every* announce instead would leave two idle clients replying to each
    other's replies for ever.

    Answering only the first time we hear a given peer settles after exactly
    two announces: they hear us, we are no longer new to them, and it stops.
    """

    def __init__(self, router, self_hex: str,
                 coalesce_secs: float = FIRST_CONTACT_COALESCE_SECS,
                 max_answered: int = MAX_ANSWERED_PEERS) -> None:
        self._router = router
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
                # announcing on all of them.
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
        except Exception as e:
            RNS.log(f"TrenchChat: first-contact announce failed: {e}", RNS.LOG_WARNING)
