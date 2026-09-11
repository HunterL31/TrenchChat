"""
The seam between the managers and whatever path carries a message.

A manager names a peer by identity hash, hands over a field dict and gets a
send state back. How that peer is addressed, which stack carries the bytes and
what the path costs are the transport's business and never the manager's: that
is what lets the same manager run over Reticulum today and over a direct IP
session later without knowing which it is on.

Three types make up the seam. InboundMessage is what a handler receives in
place of an LXMF message. SendState is the transport's own word on what
happened to an outbound one. TransportLimits is the budget of the path a
message to a given peer will take, read at send time rather than compiled in.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache

# The path a message travelled or will travel. Reticulum is every peer's first
# path; "direct" is the per-pair upgrade, opened over IP when two peers can
# reach each other there.
PATH_RETICULUM = "reticulum"
PATH_DIRECT = "direct"

# Not a path a message travels: what a client shows for a member this node
# has neither a session with nor any sign of life from.
PATH_OFFLINE = "offline"


class SendState(Enum):
    """What the transport did with a message.

    SENT means it left this node. QUEUED means the transport is holding it and
    will try again itself. NO_PATH means the peer could not be addressed at
    all, which is the caller's cue to queue for retry, hint a missed delivery
    or fall back to a propagation node, exactly as it always has.
    """

    SENT = "sent"
    QUEUED = "queued"
    NO_PATH = "no_path"


@dataclass
class InboundMessage:
    """An authenticated message, as a handler sees it.

    source_hex is the sender's identity hash: already authenticated by the
    transport, and never a delivery-destination alias. fields is the unwrapped
    protocol dict for a TrenchChat message, and the wire fields as they arrived
    for anything else (a direct message from a foreign LXMF client carries its
    envelope in LXMF's own field keys, which messaging.py reads directly).
    trenchchat_protocol says which of those two it is.
    """

    source_hex: str
    fields: dict
    content: str = ""
    timestamp: float = 0.0
    hash: bytes = b""
    trenchchat_protocol: bool = False
    path: str = PATH_RETICULUM


@dataclass(frozen=True)
class TransportLimits:
    """Every budget that depends on the path a message takes.

    Read per peer at send time (Router.limits_for) so a manager never compiles
    a bandwidth assumption in. The Reticulum path's values are the constants
    the owning modules already declare; see reticulum_limits below.
    """

    inline_payload_bytes: int
    shared_file_bytes: int
    file_chunk_bytes: int
    file_request_max_chunks: int
    sync_response_messages: int
    sync_response_bytes: int
    sync_description_budget_bytes: int
    sync_window_days: int
    voice_bitrate_bps: int
    voice_frames_per_packet: int
    voice_packet_bytes: int
    ephemeral_control: bool
    control_messages_per_minute: int


@lru_cache(maxsize=1)
def reticulum_limits() -> TransportLimits:
    """The Reticulum path's budgets, read from the modules that own them.

    Imported inside the call rather than at module scope: the managers holding
    these constants import the transport, so a top-level import would close a
    cycle. Nothing here is a second copy of a value; a constant changed in its
    own module changes the limit.
    """
    from trenchchat.core import sync, sync_ranges
    from trenchchat.core.image import MAX_IMAGE_BYTES
    from trenchchat.core.protocol import (
        FILE_CHUNK_BYTES, MAX_SHARED_FILE_BYTES, SYNC_WINDOW_DAYS,
    )
    from trenchchat.network.file_transport import FILE_REQUEST_MAX_CHUNKS
    from trenchchat.network.router import CONTROL_RATE_BURST
    from trenchchat.network.voice_wire import (
        VOICE_FRAMES_PER_PACKET, VOICE_MAX_PACKET_PAYLOAD,
        VOICE_MESH_MAX_BITRATE,
    )

    return TransportLimits(
        inline_payload_bytes=MAX_IMAGE_BYTES,
        shared_file_bytes=MAX_SHARED_FILE_BYTES,
        file_chunk_bytes=FILE_CHUNK_BYTES,
        file_request_max_chunks=FILE_REQUEST_MAX_CHUNKS,
        sync_response_messages=sync.MAX_RESPONSE_MESSAGES,
        sync_response_bytes=sync.MAX_RESPONSE_BYTES,
        sync_description_budget_bytes=sync_ranges.SYNC_DESCRIPTION_BUDGET_BYTES,
        sync_window_days=SYNC_WINDOW_DAYS,
        voice_bitrate_bps=VOICE_MESH_MAX_BITRATE,
        voice_frames_per_packet=VOICE_FRAMES_PER_PACKET,
        voice_packet_bytes=VOICE_MAX_PACKET_PAYLOAD,
        ephemeral_control=False,
        control_messages_per_minute=CONTROL_RATE_BURST,
    )


# --- the direct path's budgets ---
#
# The Reticulum column of the plan's limits table is every constant's own
# module; this is the direct column, which no other module owns yet. A message
# is the same size on either path, because any member must be able to serve it
# over either one; what the direct path raises is how much moves per exchange.
DIRECT_SHARED_FILE_BYTES = 200 * 1024 * 1024
DIRECT_FILE_REQUEST_MAX_CHUNKS = 256
DIRECT_SYNC_RESPONSE_MESSAGES = 500
DIRECT_SYNC_RESPONSE_BYTES = 8 * 1024 * 1024
DIRECT_SYNC_DESCRIPTION_BUDGET_BYTES = 64 * 1024
# Full history. A description on this path reaches behind the recent window
# with one fingerprint per calendar year and then per month
# (core/sync_ranges.history_ranges), so a whole transcript costs a re-check a
# handful of ranges and nothing at all when the two sides agree. Expressed as
# days because that is what the limit is counted in; a century is "all of it"
# for any node that will ever run this.
DIRECT_SYNC_WINDOW_DAYS = 365 * 100

DIRECT_VOICE_BITRATE_BPS = 64000
DIRECT_VOICE_FRAMES_PER_PACKET = 1
DIRECT_VOICE_PACKET_BYTES = 1200
DIRECT_CONTROL_MESSAGES_PER_MINUTE = 600


@lru_cache(maxsize=1)
def direct_limits() -> TransportLimits:
    """The direct path's budgets, as the plan's limits table sets them.

    The sync window is how far back a description reaches, not where a request
    starts: a routine re-check begins at the recent window on every path,
    because one that began at the start of history would be a deep ask every
    time and be paced as one (tests/test_sync_reconcile.py). What this widens
    is the span a node offers for comparison behind that start, which the
    calendar ladder makes cheap.

    Imported inside the call for the same reason reticulum_limits is: the
    modules owning the unchanged values import the transport.
    """
    from trenchchat.core.image import MAX_IMAGE_BYTES
    from trenchchat.core.protocol import FILE_CHUNK_BYTES

    return TransportLimits(
        inline_payload_bytes=MAX_IMAGE_BYTES,
        shared_file_bytes=DIRECT_SHARED_FILE_BYTES,
        file_chunk_bytes=FILE_CHUNK_BYTES,
        file_request_max_chunks=DIRECT_FILE_REQUEST_MAX_CHUNKS,
        sync_response_messages=DIRECT_SYNC_RESPONSE_MESSAGES,
        sync_response_bytes=DIRECT_SYNC_RESPONSE_BYTES,
        sync_description_budget_bytes=DIRECT_SYNC_DESCRIPTION_BUDGET_BYTES,
        sync_window_days=DIRECT_SYNC_WINDOW_DAYS,
        voice_bitrate_bps=DIRECT_VOICE_BITRATE_BPS,
        voice_frames_per_packet=DIRECT_VOICE_FRAMES_PER_PACKET,
        voice_packet_bytes=DIRECT_VOICE_PACKET_BYTES,
        ephemeral_control=True,
        control_messages_per_minute=DIRECT_CONTROL_MESSAGES_PER_MINUTE,
    )


class Transport(ABC):
    """One path a message can take between two identities.

    Everything a manager can ask of a path is here. The message plane is
    abstract because no path exists without it; the announce, discovery and
    propagation surfaces carry defaults, because they are Reticulum's own and a
    path that has none of them should not have to say so.
    """

    # --- message plane ---

    @abstractmethod
    def send(self, dest_hex: str, fields: dict, content: str = "", *,
             on_delivered=None, on_failed=None, propagated: bool = False,
             envelope: bool = True) -> SendState:
        """Send one message to a peer named by identity hash.

        fields is the TrenchChat field dict, wrapped in the protocol envelope
        unless envelope is False, which sends the keys exactly as given (a
        direct message, so a client that is not TrenchChat can read it).
        on_delivered and on_failed take the destination hex.
        """

    @abstractmethod
    def can_reach(self, dest_hex: str) -> bool:
        """Whether a send to this peer can be addressed right now."""

    @abstractmethod
    def request_path(self, dest_hex: str) -> None:
        """Ask the network where a peer is. Never blocks, never waits."""

    @abstractmethod
    def limits_for(self, dest_hex: str) -> TransportLimits:
        """The budgets a message to this peer travels under."""

    @abstractmethod
    def drain(self, timeout: float) -> int:
        """Wait for outbound messages to stop moving. Returns how many settled."""

    @abstractmethod
    def set_inbound_callback(self, callback) -> None:
        """Register the single callback every authenticated message arrives on."""

    @abstractmethod
    def stop(self) -> None:
        """Persist state and tear the path down. Safe to call twice."""

    # --- peer keys ---

    def public_key_for(self, peer_hex: str) -> bytes | None:
        """A peer's public key, if this path knows it."""
        return None

    def resolve_address(self, address_hex: str) -> str | None:
        """The identity hash behind a path-specific address, if known."""
        return None

    # --- peer events ---

    def set_peer_event_callbacks(self, *, peer_appeared=None,
                                 identity_resolved=None, channel_discovered=None,
                                 user_discovered=None, node_discovered=None,
                                 propagation_node_heard=None,
                                 path_changed=None) -> None:
        """Register the fan-outs a path fires when it learns about a peer."""

    # --- announces ---

    def announce(self, attached_interface=None) -> None:
        """Announce this node's own address on the path."""

    def announce_user(self, attached_interface=None) -> None:
        """Announce that this identity runs TrenchChat."""

    def register_channel(self, channel_hash_hex: str, aspect_name: str) -> None:
        """Take ownership of a channel's address on this path."""

    def announce_channel(self, channel_hash_hex: str, app_data: bytes,
                         attached_interface=None) -> None:
        """Announce one owned channel with its discovery metadata."""

    def set_display_name(self, display_name: str) -> None:
        """Update the name carried in this node's announces."""

    def release_quarantined(self, identity_hex: str) -> None:
        """Re-deliver anything held for an identity that has just resolved."""

    # --- propagation ---

    def enable_propagation(self) -> None:
        """Host a store for other nodes' mail."""
        raise NotImplementedError

    def disable_propagation(self) -> None:
        """Stop hosting a store."""
        raise NotImplementedError

    @property
    def outbound_propagation_node(self) -> bytes | None:
        """The node this client leaves offline messages with, if any."""
        return None

    def set_outbound_propagation_node(self, destination_hash: bytes) -> None:
        """Choose the node to leave offline messages with."""

    def request_propagation_sync(self) -> bool:
        """Ask the chosen node for mail held for us. False if there is none."""
        return False

    def propagation_sync_state(self) -> int:
        """The transfer state of the last collection attempt."""
        return 0
