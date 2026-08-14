"""
Per-channel sync progress tracking.

Sync runs entirely in the background: SyncManager asks every reachable peer on a
channel for anything we missed, and the answers arrive as ordinary stored
messages.  This module records what was asked of whom and what came back, so a
frontend can tell the user whether a channel is up to date, still filling in, or
missing history it can't reach right now.

Nothing here touches the network -- it only observes calls SyncManager already
makes.  A channel's state is derived from its peer records:

    any peer still pending           -> SYNCING
    a known gap or a truncated batch -> INCOMPLETE
    at least one peer answered       -> SYNCED
    every peer unreachable           -> WAITING
    asked, nobody answered           -> INCOMPLETE

SYNCED requires a peer to have actually answered.  A peer with nothing to send
replies with an empty sync response for exactly this reason: without it,
"caught up" and "never answered" are the same silence.

SYNCED is scoped to peers we know about.  A peer whose announce never reached
us is never asked and can't be accounted for -- on a partition-tolerant mesh
there is no way to enumerate everyone who might hold history, so SYNCED means
"every peer we know about answered and had nothing more," not "no history
exists anywhere." get_status()'s "answered_peers" count says how many peers
that claim rests on.
"""

import threading
import time
from enum import Enum

import RNS

# How long a peer has to answer a sync request before it's counted as silent.
# Sized against SYNC_RESPONSE_WINDOW_SECS in sync.py, which is how long the
# response itself stays answerable.
PEER_RESPONSE_TIMEOUT_SECS = 300


class SyncState(str, Enum):
    """Overall sync state of a single channel."""
    UNKNOWN    = "unknown"       # never attempted
    SYNCING    = "syncing"       # at least one request outstanding
    SYNCED     = "synced"        # a peer answered and reported nothing further
    INCOMPLETE = "incomplete"    # known gap we can't close right now
    WAITING    = "waiting"       # no reachable peer to sync from


class PeerSyncState(str, Enum):
    """State of one peer we asked for history."""
    PENDING     = "pending"
    ANSWERED    = "answered"
    SILENT      = "silent"
    UNREACHABLE = "unreachable"


class _PeerRecord:
    __slots__ = ("state", "requested_at", "messages_received", "truncated")

    def __init__(self):
        self.state = PeerSyncState.PENDING
        self.requested_at = 0.0
        self.messages_received = 0
        self.truncated = False


class _ChannelRecord:
    __slots__ = ("peers", "gap", "received_count", "attempted")

    def __init__(self):
        self.peers: dict[str, _PeerRecord] = {}
        self.gap = False
        self.received_count = 0
        self.attempted = False


class SyncStatusTracker:
    """Records sync activity per channel and notifies listeners when it changes."""

    def __init__(self, storage):
        self._storage = storage
        self._channels: dict[str, _ChannelRecord] = {}
        self._lock = threading.Lock()
        self._callbacks: list = []

    # --- public API ---

    def add_status_callback(self, cb) -> None:
        """Register a callback invoked with (channel_hash_hex) whenever status changes."""
        self._callbacks.append(cb)

    def note_no_peers(self, channel_hash_hex: str) -> None:
        """A sync was attempted on a channel with no known peers to ask."""
        with self._lock:
            before = self._snapshot_locked(channel_hash_hex)
            rec = self._channels.setdefault(channel_hash_hex, _ChannelRecord())
            rec.attempted = True
            changed = before != self._snapshot_locked(channel_hash_hex)
        if changed:
            self._fire(channel_hash_hex)

    def request_sent(self, channel_hash_hex: str, peer_hex: str,
                     continuation: bool = False, reaching_back: bool = False) -> None:
        """A sync request went out to this peer.

        Re-asking a peer that already answered, on a channel that is already up
        to date, is a routine re-check -- every peer announce triggers one. It
        leaves the reported state alone, so a settled channel doesn't flicker
        for the life of the session; an answer carrying anything new still
        moves it.

        *reaching_back* marks a request for history older than our watermark.
        That is not a re-check but a real attempt to fill a gap, so it must
        show -- otherwise a round where every peer stays silent still reports
        the channel as up to date.
        """
        with self._lock:
            before = self._snapshot_locked(channel_hash_hex)
            rec = self._channels.setdefault(channel_hash_hex, _ChannelRecord())
            rec.attempted = True
            peer = rec.peers.setdefault(peer_hex, _PeerRecord())

            quiet = (not continuation
                     and not reaching_back
                     and peer.state == PeerSyncState.ANSWERED
                     and self._derive_locked(channel_hash_hex) == SyncState.SYNCED)
            peer.requested_at = time.time()
            if not quiet:
                peer.state = PeerSyncState.PENDING
                peer.truncated = False
            changed = before != self._snapshot_locked(channel_hash_hex)
        if changed:
            self._fire(channel_hash_hex)

    def request_unreachable(self, channel_hash_hex: str, peer_hex: str) -> None:
        """A sync request could not be sent because the peer's path is unknown."""
        with self._lock:
            before = self._snapshot_locked(channel_hash_hex)
            rec = self._channels.setdefault(channel_hash_hex, _ChannelRecord())
            rec.attempted = True
            peer = rec.peers.setdefault(peer_hex, _PeerRecord())
            peer.state = PeerSyncState.UNREACHABLE
            changed = before != self._snapshot_locked(channel_hash_hex)
        if changed:
            self._fire(channel_hash_hex)

    def response_received(self, channel_hash_hex: str, peer_hex: str, *,
                          received: int, inserted: int, truncated: bool) -> None:
        """A peer answered with *received* messages, *inserted* of them new.

        *truncated* means the responder hit its per-response cap and holds more.
        """
        with self._lock:
            before = self._snapshot_locked(channel_hash_hex)
            rec = self._channels.setdefault(channel_hash_hex, _ChannelRecord())
            peer = rec.peers.setdefault(peer_hex, _PeerRecord())
            peer.state = PeerSyncState.ANSWERED
            peer.messages_received += received
            peer.truncated = truncated
            rec.received_count += inserted
            changed = before != self._snapshot_locked(channel_hash_hex)
        if changed:
            self._fire(channel_hash_hex)

    def response_malformed(self, channel_hash_hex: str, peer_hex: str) -> None:
        """A peer answered, but the payload was unusable.

        The request is resolved either way -- nothing else will ever answer
        it -- but the peer told us nothing, so it counts as silent rather
        than answered.
        """
        with self._lock:
            before = self._snapshot_locked(channel_hash_hex)
            rec = self._channels.setdefault(channel_hash_hex, _ChannelRecord())
            peer = rec.peers.setdefault(peer_hex, _PeerRecord())
            peer.state = PeerSyncState.SILENT
            changed = before != self._snapshot_locked(channel_hash_hex)
        if changed:
            self._fire(channel_hash_hex)

    def note_gap(self, channel_hash_hex: str) -> None:
        """Record that history is known to be missing on this channel."""
        self._set_gap(channel_hash_hex, True)

    def clear_gap(self, channel_hash_hex: str) -> None:
        """Record that the known gap has been filled."""
        self._set_gap(channel_hash_hex, False)

    def prune(self) -> None:
        """Mark peers that never answered within the timeout as silent."""
        now = time.time()
        changed: list[str] = []
        with self._lock:
            for channel_hash_hex, rec in self._channels.items():
                before = self._snapshot_locked(channel_hash_hex)
                for peer_hex, peer in rec.peers.items():
                    if (peer.state == PeerSyncState.PENDING
                            and now - peer.requested_at > PEER_RESPONSE_TIMEOUT_SECS):
                        peer.state = PeerSyncState.SILENT
                        RNS.log(
                            f"TrenchChat [sync]: no sync response from "
                            f"{peer_hex[:12]}… for {channel_hash_hex[:12]}…",
                            RNS.LOG_DEBUG,
                        )
                if before != self._snapshot_locked(channel_hash_hex):
                    changed.append(channel_hash_hex)
        for channel_hash_hex in changed:
            self._fire(channel_hash_hex)

    def get_state(self, channel_hash_hex: str) -> SyncState:
        """Return the overall sync state of a channel."""
        with self._lock:
            return self._derive_locked(channel_hash_hex)

    def get_status(self, channel_hash_hex: str) -> dict:
        """Return the full status of a channel, including per-peer detail."""
        with self._lock:
            rec = self._channels.get(channel_hash_hex)
            state = self._derive_locked(channel_hash_hex)
            peers = [
                {
                    "identity_hash":     peer_hex,
                    "state":             peer.state.value,
                    "messages_received": peer.messages_received,
                    "requested_at":      peer.requested_at,
                }
                for peer_hex, peer in sorted(rec.peers.items())
            ] if rec else []
            pending = sum(1 for p in peers if p["state"] == PeerSyncState.PENDING.value)
            answered = sum(1 for p in peers if p["state"] == PeerSyncState.ANSWERED.value)
            received_count = rec.received_count if rec else 0

        return {
            "channel_hash":    channel_hash_hex,
            "state":           state.value,
            "peers":           peers,
            "pending_peers":   pending,
            "answered_peers":  answered,
            "received_count":  received_count,
            "last_synced_at":  self._storage.get_last_sync(channel_hash_hex),
        }

    # --- private helpers ---

    def _set_gap(self, channel_hash_hex: str, present: bool) -> None:
        with self._lock:
            rec = self._channels.get(channel_hash_hex)
            if rec is None:
                if not present:
                    return
                rec = self._channels.setdefault(channel_hash_hex, _ChannelRecord())
            if rec.gap == present:
                return
            rec.gap = present
        self._fire(channel_hash_hex)

    def _derive_locked(self, channel_hash_hex: str) -> SyncState:
        """Must be called with self._lock held."""
        rec = self._channels.get(channel_hash_hex)
        if rec is None:
            return SyncState.UNKNOWN
        if not rec.peers:
            if rec.gap:
                return SyncState.INCOMPLETE
            return SyncState.WAITING if rec.attempted else SyncState.UNKNOWN

        states = [p.state for p in rec.peers.values()]
        if PeerSyncState.PENDING in states:
            return SyncState.SYNCING
        if rec.gap or any(p.truncated for p in rec.peers.values()):
            return SyncState.INCOMPLETE
        if PeerSyncState.ANSWERED in states:
            # One peer answering says nothing about what a peer that went
            # silent was holding, so a single answer can't certify the channel
            # while others never replied.
            if all(s in (PeerSyncState.ANSWERED, PeerSyncState.UNREACHABLE)
                   for s in states):
                return SyncState.SYNCED
            return SyncState.INCOMPLETE
        if states and all(s == PeerSyncState.UNREACHABLE for s in states):
            return SyncState.WAITING
        return SyncState.INCOMPLETE

    def _snapshot_locked(self, channel_hash_hex: str) -> tuple:
        """Comparable summary used to decide whether a change is worth reporting."""
        rec = self._channels.get(channel_hash_hex)
        if rec is None:
            return (SyncState.UNKNOWN, (), False, 0)
        peers = tuple(
            (peer_hex, peer.state, peer.messages_received, peer.truncated)
            for peer_hex, peer in sorted(rec.peers.items())
        )
        return (self._derive_locked(channel_hash_hex), peers, rec.gap, rec.received_count)

    def _fire(self, channel_hash_hex: str) -> None:
        for cb in self._callbacks:
            try:
                cb(channel_hash_hex)
            except Exception as e:
                RNS.log(f"TrenchChat [sync]: status callback error: {e}", RNS.LOG_ERROR)
