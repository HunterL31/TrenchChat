"""
Gossip-based message gap sync.

Three mechanisms work together:
  1. Messaging.flush_pending(): sender retries queued messages when peer reappears
  2. MT_MISSED_DELIVERY broadcast: sender tells online peers which subscriber missed a message
  3. MT_SYNC_REQUEST / MT_SYNC_RESPONSE: reconnecting peer pulls missing messages from any peer

Flow when B reconnects:
  - PeerAnnounceHandler fires on_peer_appeared(B)
  - SyncManager calls messaging.flush_pending(B)      [Mechanism 1]
  - SyncManager sends MT_SYNC_REQUEST to B's channels [Mechanism 3 – B pulls from us]
  - B's own SyncManager sends MT_SYNC_REQUEST to us   [Mechanism 3 – B pulls from all peers]

Flow when A fails to deliver to B:
  - Messaging calls missed_delivery_callback(channel, B, msg_id, all_subs)
  - SyncManager sends MT_MISSED_DELIVERY to all online subscribers
  - Each peer stores the hint in missed_deliveries table

When B later sends MT_SYNC_REQUEST:
  - Peer checks missed_deliveries hints for B → sends exact missing messages
  - The request describes the rows B already holds, as timestamp ranges
    summarised by fingerprint or spelled out by id prefix (core/sync_ranges.py),
    and the answer is the difference between that and what the peer may serve.
    Ranges that still differ are split and re-asked, so a peer with a gap
    behind its own watermark still gets it: a watermark can only say "since
    when", never "which".
  - A request carrying no ranges at all (a peer that predates them) falls back
    to the timestamp sweep below, unchanged.

Reconciliation runs both ways from one exchange. The responder answers with
what the requester lacks, and asks, in the same message, for anything the
requester named that it does not hold itself (F_SYNC_NEED). Nothing is ever
pushed unasked: a need is answered by a response, and the responder records a
request of its own before sending one, so that answer is claimable.

A request reaching further back than SYNC_WINDOW_SECS ("deep" backfill) is
still answered -- there's no hard wall -- but rate-limited per (channel,
peer): DEEP_SYNC_COOLDOWN_SECS between deep sweeps this responder will serve
a given peer, so a flood of requests can't repeatedly force a full
timestamp sweep. A request within the recent window is unaffected and
always answered immediately.

Every authorised request is answered, including with an empty message list.
Silence would otherwise be ambiguous -- "nothing for you", "never received",
and "not allowed" would look identical -- and SyncStatusTracker could never
honestly report a channel as up to date.  Requests we refuse (unauthorised,
or throttled) stay silent so neither leaks a signal.

A response that hits MAX_RESPONSE_MESSAGES carries F_SYNC_TRUNCATED, and the
requester immediately asks the same peer for the next batch from its newly
advanced watermark.  Without that, everything past the cap waits for an
unrelated announce to trigger the next request.

A watermark only ever advances over messages actually accepted.  Rows the
responder withholds are skipped by the responder's own sweep instead, so a
grant that was still propagating when the request landed can be picked up
later rather than being lost behind a watermark that ran past it.
"""

import math
import re
import threading
import time

import RNS
import LXMF
import msgpack

from trenchchat.core.control_retry import ControlRetryQueue
from trenchchat.core.identity import Identity
from trenchchat.core.authorship import (
    public_key_for, remember_relayed_key, verify_message,
)
from trenchchat.core.image import MAX_IMAGE_BYTES, inbound_image_is_sane
from trenchchat.core.messaging import CAUSAL_WINDOW_SECS, Messaging, _compute_message_id
from trenchchat.core.permissions import (
    FULL_SYNC, has_permission, is_open_join, permissions_from_json,
)
from trenchchat.core.protocol import (
    F_AUTHOR_KEYS, F_CHANNEL_HASH, F_MSG_TYPE,
    F_SYNC_WINDOW_START, F_SYNC_MESSAGES, F_SYNC_TRUNCATED, F_SYNC_SCAN_CURSOR,
    F_SYNC_RANGES, F_SYNC_NEED, F_SYNC_CONTINUES, F_SYNC_PROBE, F_MISSED_FOR,
    F_MISSED_MSG_ID, MT_PRESENCE,
    MAX_CLOCK_SKEW_SECS, MT_MISSED_DELIVERY, MT_SYNC_REQUEST, MT_SYNC_RESPONSE,
    RANGE_IDLIST, SYNC_WINDOW_SECS,
    message_id_from_wire, message_id_to_wire, pack_fields, unpack_wire,
    wire_timestamp,
)
from trenchchat.core.reaction import is_custom_emoji_hash
from trenchchat.core.storage import Storage
from trenchchat.core import sync_ranges
from trenchchat.core.sync_status import SyncStatusTracker
from trenchchat.network.router import Router

# Maximum messages returned in a single sync response (LXMF size budget)
MAX_RESPONSE_MESSAGES = 50

# Byte budget for one response. Every other cap here counts rows, but the cost
# is bytes: MAX_RESPONSE_MESSAGES rows each carrying a MAX_IMAGE_BYTES image is
# ~45 MB, far past what unpack_wire will accept at the other end -- so a window
# holding a few images could never be synced at all, and each attempt put tens
# of megabytes on a shared medium. Sits under MAX_WIRE_PAYLOAD with room for
# msgpack framing. A single group still ships whole, so one oversized row is
# never stranded.
MAX_RESPONSE_BYTES = 1_000_000

# Cap on the {author: public key} map a sync response may carry. One key per
# author in a batch, so it can never legitimately exceed one per row.
MAX_AUTHOR_KEYS = MAX_RESPONSE_MESSAGES

# Reactions ride along with each synced message so backfilled history isn't
# stripped of them. Capped per message to bound the response size.
MAX_REACTIONS_PER_MESSAGE = 32

# How long an issued sync request stays answerable.  A response arriving
# outside this window is treated as unsolicited.  Generous enough to cover a
# slow multi-hop mesh round trip without leaving the window open indefinitely.
SYNC_RESPONSE_WINDOW_SECS = 300

# How far back a responder will look, beyond a never-before-served peer's own
# claimed window start, before it's trusted as given. A first-time peer's
# claim can be inflated -- borrowed from a different responder's disjoint
# answer landing first in the same reconnect round (A1, test_sync_multipeer.py)
# -- so a responder they've never actually served widens its floor to absorb
# that, but only within this bound; a claim off by more than this reflects the
# peer's own, unrelated history and is trusted as given rather than re-swept.
PEER_TRUST_HORIZON_SECS = 300

# How often a single peer may trigger a deep (pre-SYNC_WINDOW_SECS) backfill
# sweep on a given channel from this responder. A soft mitigation against a
# flood of costly full timestamp sweeps, not a hard security boundary --
# tenure and full_sync already gate what a peer is authorised to see; this
# only paces how fast they can pull it.
DEEP_SYNC_COOLDOWN_SECS = 60

# How long an unanswered sync request waits before it is asked again. Longer
# than the responder's deep-sync cooldown, so a retry lands in the next window
# that cooldown will actually serve rather than inside the one that refused.
SYNC_RETRY_SECS = 90.0

# Minimum spacing of announce-driven sync requests per (channel, peer).
# PeerAnnounceHandler fires on_peer_appeared on every announce, not just
# transitions, so without this every repeat announce costs a full LXMF
# request/response round trip per shared channel. Only paces the announce
# trigger -- startup, join, link-return and retry requests are unaffected,
# and a peer gone longer than this always gets a fresh request on return.
# Matches MIN_RESYNC_INTERVAL_SECS in connectivity.py.
ANNOUNCE_SYNC_COOLDOWN_SECS = 120.0

# Idle (channel, peer) announce-cooldown entries older than this are pruned
# during recording, so the map stays bounded over a long session.
ANNOUNCE_SYNC_PRUNE_SECS = 3600.0

# A probe on a peer's presence beacon that matched what we hold stands in for
# the announce-driven re-check of that (channel, peer) for this long: the
# re-check would ask the question the probe just answered.
PROBE_AGREE_SECS = ANNOUNCE_SYNC_COOLDOWN_SECS

# A message names the newest message its author had seen (last_seen_id). Not
# holding that one is evidence of a gap, and its author is a peer known to
# hold it. Suspected gaps are asked about together on the next tick, after
# the causal window has had its chance to deliver the row the ordinary way,
# and bounded so a flood of dangling references cannot grow the set.
SUSPECTED_GAP_GRACE_SECS = CAUSAL_WINDOW_SECS
MAX_SUSPECTED_GAPS = 64

# Consecutive unanswered re-asks to one peer on one channel before we stop.
# A peer that keeps its path resolvable and simply never answers would
# otherwise draw one whole-transcript request out of every member, forever --
# and since our progress with them never advances, every retry asks for
# everything. Resets as soon as they answer.
MAX_SYNC_RETRIES = 5

# Multiplier applied per consecutive unanswered re-ask, so a silent peer is
# asked at a widening interval rather than a flat one.
SYNC_RETRY_BACKOFF = 2.0

# How long an idle (channel, peer) cooldown entry is kept before being
# pruned, so the cooldown map doesn't grow unbounded over a long session
# with many distinct peers.
DEEP_SYNC_COOLDOWN_PRUNE_SECS = 24 * 3600

# How many message rows one sweep will scan before giving up, however many of
# them the requester turns out to be entitled to. Bounds the work a request can
# cost when a long run of history is withheld from the requester.
MAX_SWEEP_SCAN = MAX_RESPONSE_MESSAGES * 20

# How many times a truncated response may chain another request to the same
# peer on the same channel. Bounds the work a peer can induce by setting
# F_SYNC_TRUNCATED on every response; MAX_RESPONSE_MESSAGES per batch makes
# this enough to backfill a substantial history in one reconnect.
MAX_SYNC_CONTINUATIONS = 20

# How many messages of one deep exchange this responder will serve inside a
# cooldown window. A deep reconcile is no longer a single sweep: the requester
# summarises what it holds, we answer where we differ, and it narrows. Refusing
# those narrowing steps strands it mid-exchange with no way to finish, so an
# exchange already accepted is allowed to run, bounded by the requester's own
# continuation budget plus the ask that started it.
DEEP_SYNC_BURST = MAX_SYNC_CONTINUATIONS + 1

# How many missed-delivery hints are queued for retry against a single
# unreachable peer before the oldest is dropped, so a burst of failed
# deliveries can't grow the retry queue without bound.
MAX_QUEUED_HINTS_PER_PEER = 50

# How far past our own clock a request's ranges reach, so a message a peer
# stamped moments ago on a clock running ahead of ours is still inside the
# span we describe rather than falling off the end of it.
SYNC_CLOCK_SKEW_SECS = MAX_CLOCK_SKEW_SECS

# How many rows one message may ask for by id prefix.
MAX_NEED_IDS = sync_ranges.MAX_SYNC_NEEDS


_IDENTITY_HEX_RE = re.compile(r"^[0-9a-f]{32}$")
_MESSAGE_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def _coerce_str(value) -> str:
    """Decode a msgpack field that may arrive as bytes rather than str."""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value) if value is not None else ""


def _is_identity_hex(value: str) -> bool:
    return bool(_IDENTITY_HEX_RE.match(value))


def _is_message_id(value: str) -> bool:
    return bool(_MESSAGE_ID_RE.match(value))


def _row_is_signed(row) -> bool:
    """Whether a row carries an author signature.

    Reads either a full message row or the lightweight index row
    Storage.get_message_index returns, which carries the answer as has_sig
    rather than the signature itself.
    """
    keys = row.keys()
    if "has_sig" in keys:
        return bool(row["has_sig"])
    return "author_sig" in keys and bool(row["author_sig"])


def row_wire_size(row) -> int:
    """Roughly what a row costs on the wire, dominated by its attachment."""
    try:
        image = row["image_data"]
    except (KeyError, IndexError):
        image = None
    try:
        content = row["content"] or ""
    except (KeyError, IndexError):
        content = ""
    return len(image or b"") + len(content.encode(errors="replace")) + 256


def _cut_index(rows: list) -> int:
    """First index past the rows that fit both the row and byte budgets."""
    used = 0
    for i, row in enumerate(rows):
        used += row_wire_size(row)
        if i >= MAX_RESPONSE_MESSAGES or (i > 0 and used > MAX_RESPONSE_BYTES):
            return i
    return len(rows)


def _truncate_at_group_boundary(rows: list) -> tuple[list, bool]:
    """Cut a batch to the response caps without splitting a timestamp group.

    Returns (rows, dropped_any). F_SYNC_WINDOW_START is a bare float and
    Storage.get_messages_after filters on a strict timestamp >, so messages
    sharing one timestamp have to travel together: whichever half landed on
    the far side of a cut would be skipped by every future sweep. A single
    group over either budget ships whole rather than stalling forever --
    the same rule _collect_permitted_rows applies while sweeping.
    """
    cut = _cut_index(rows)
    if cut >= len(rows):
        return rows, False

    boundary_ts = rows[cut]["timestamp"]
    while cut > 0 and rows[cut - 1]["timestamp"] == boundary_ts:
        cut -= 1
    if cut == 0:
        while cut < len(rows) and rows[cut]["timestamp"] == boundary_ts:
            cut += 1
    return rows[:cut], cut < len(rows)


class SyncManager:
    def __init__(self, identity: Identity, storage: Storage, router: Router,
                 messaging: Messaging, subscription_mgr, invite_mgr,
                 reaction_mgr=None):
        self._identity = identity
        self._storage = storage
        self._router = router
        self._messaging = messaging
        self._subscription_mgr = subscription_mgr
        self._invite_mgr = invite_mgr
        self._reaction_mgr = reaction_mgr

        # (channel_hash_hex, peer_hex) -> list of (issued_at, since_ts,
        # dest_hex, request_id) entries.  A peer may have more than one
        # outstanding request at once (e.g. a startup sync and an
        # announce-driven request racing each other), so this is a FIFO
        # queue per key rather than a single slot -- each legitimate
        # response claims and removes exactly one entry.  The same entry is
        # recorded under both key forms a peer may be identified by (see
        # _peer_key_forms); claiming it under either form removes both.
        self._pending_requests: dict[tuple[str, str], list[tuple]] = {}
        self._pending_requests_lock = threading.Lock()
        self._pending_request_seq = 0

        # dest_hex -> list of missed-delivery hint field dicts that could not
        # be sent because the peer's identity was momentarily unresolvable.
        # Retried from on_peer_appeared once the peer is reachable again.
        self._pending_hints: dict[str, list[dict]] = {}
        self._pending_hints_lock = threading.Lock()

        # (channel_hash_hex, peer_hex) -> (when this deep window opened, the
        # policy it opened under, messages served in it). In-memory only; a
        # restart resets it, which is acceptable for a soft rate limit.
        self._deep_sync_last_served: dict[tuple[str, str], tuple[float, str, int]] = {}
        self._deep_sync_lock = threading.Lock()

        # (channel_hash_hex, peer_hex) -> last announce-driven sync request.
        # In-memory only, same rationale as the deep-sync map.
        self._announce_sync_times: dict[tuple[str, str], float] = {}
        self._announce_sync_lock = threading.Lock()
        # Held no longer than a requester will accept an answer for: past
        # that they discard it as unsolicited and ask again anyway.
        self._retry = ControlRetryQueue("sync", SYNC_RESPONSE_WINDOW_SECS)
        # (channel_hash_hex, peer_hex) -> consecutive unanswered re-asks.
        self._retry_counts: dict[tuple[str, str], int] = {}

        # (channel_hash_hex, peer_hex) -> continuation requests chained so far
        self._continuations: dict[tuple[str, str], int] = {}
        self._continuations_lock = threading.Lock()

        # (channel_hash_hex, peer_hex) -> signature of the last question we
        # put to that peer. An identical question can only get an identical
        # answer, so this is what ends a narrowing exchange.
        self._last_asked: dict[tuple[str, str], tuple] = {}
        self._last_asked_lock = threading.Lock()
        # (channel_hash_hex, peer_hex) pairs where a difference was left
        # undescribed for the budget and must be asked about again once the
        # narrowing in flight has run its course.
        self._deferred: set[tuple[str, str]] = set()
        self._deferred_lock = threading.Lock()

        # What a peer's presence beacon last said about a channel, and when
        # it last agreed with us: (channel_hash_hex, peer_hex) -> (at, fp)
        # and -> at. A mismatch the cooldown would not let us act on yet
        # waits in _probe_pending for tick().
        self._probe_seen: dict[tuple[str, str], tuple[float, bytes]] = {}
        self._probe_agreed: dict[tuple[str, str], float] = {}
        self._probe_pending: set[tuple[str, str]] = set()
        self._probe_lock = threading.Lock()

        # (channel_hash_hex, message_id) -> (ask_after, peer_hex, referenced_from_ts)
        # for a last_seen_id we did not hold when a message naming it arrived.
        self._suspected: dict[tuple[str, str], tuple[float, str, float]] = {}
        self._suspected_lock = threading.Lock()

        # channel_hash_hex -> the entitlement the last request was issued under
        self._sync_policy: dict[str, tuple[str, str]] = {}
        self._sync_policy_lock = threading.Lock()

        self._status = SyncStatusTracker(storage)

        messaging.set_missed_delivery_callback(self._on_missed_delivery_event)
        messaging.add_message_callback(self._on_message_stored)
        router.add_delivery_callback(self._on_lxmf_message)
        invite_mgr.add_member_list_callback(self._on_member_list_updated)
        invite_mgr.add_channel_joined_callback(self._on_channel_joined)

        # Purge stale hints from previous sessions on startup
        self._storage.purge_old_missed_deliveries(time.time() - SYNC_WINDOW_SECS)

    # --- public API ---

    @property
    def status(self) -> SyncStatusTracker:
        """Per-channel sync progress, for frontends that display it."""
        return self._status

    def request_sync_all(self):
        """
        On startup: send MT_SYNC_REQUEST for every subscribed channel to all
        known-online peers (those whose RNS path is already resolved).

        Also runs when our own link returns (LinkWatcher). Either way any
        standing announce cooldown predates the world we are catching up
        with, so it must not suppress the announce-driven requests that
        follow -- a peer unreachable here (path not yet resolved) has only
        those left.
        """
        with self._announce_sync_lock:
            self._announce_sync_times.clear()
        for sub in self._storage.get_subscriptions():
            self._request_sync_for_channel(sub["channel_hash"])

    def _on_channel_joined(self, channel_hash_hex: str, channel_name: str):
        """
        Fired when we auto-join a channel via an accepted invite. Without
        this, a new member never sees any message sent before they joined:
        request_sync_all() only runs once, 3s after app startup, over
        whatever channels are already subscribed at that moment -- a
        channel joined later in the session is never covered by anything.

        A fresh join has no last_sync_at yet, so ask for everything (0.0) --
        the responder decides how far back it's actually willing to look;
        see _handle_sync_request's deep-sync cooldown.
        """
        self._request_sync_for_channel(channel_hash_hex, 0.0)

    def _sync_policy_for(self, channel_hash_hex: str) -> tuple[str, str]:
        """What our entitlement to this channel's history currently depends on."""
        channel = self._storage.get_channel(channel_hash_hex)
        role = self._storage.get_role(channel_hash_hex, self._identity.hash_hex)
        return (channel["permissions"] if channel else "", role or "")

    def _request_sync_for_channel(self, channel_hash_hex: str, since_ts: float | None = None):
        """Ask every known peer on this channel for anything new.

        *since_ts*, when given, overrides each peer's individual progress and
        is sent to every peer uniformly -- used for a fresh channel join
        (nothing to resume from yet) and below, when entitlement changes
        (history withheld under an earlier role must be re-asked from the
        start for every peer, not just resumed incrementally). Otherwise each
        peer is asked from its own per-(channel, peer) sync progress, not the
        channel-wide watermark -- see A1 in test_sync_multipeer.py for why a
        shared watermark strands disjoint history held by different peers.
        """
        with self._sync_policy_lock:
            previous = self._sync_policy.get(channel_hash_hex)
        if previous is not None and previous != self._sync_policy_for(channel_hash_hex):
            RNS.log(
                f"TrenchChat [sync]: sync entitlement changed for "
                f"{channel_hash_hex[:12]}… — re-asking from the start",
                RNS.LOG_NOTICE,
            )
            since_ts = 0.0

        peers = self._get_channel_peers(channel_hash_hex)
        if not peers:
            self._status.note_no_peers(channel_hash_hex)
            return
        for peer_hex in peers:
            peer_since = since_ts if since_ts is not None else \
                self._storage.get_peer_sync_progress(channel_hash_hex, peer_hex)
            self._send_sync_request(peer_hex, channel_hash_hex, peer_since)

    def _on_message_stored(self, channel_hash_hex: str, message_id: str):
        """Clear a hinted gap once the messages it named have all arrived,
        and note a gap a message's last_seen_id reveals.

        A hint says a message never reached us; the sender's own retry queue
        usually delivers it directly, which is not a sync response. Clearing
        the gap only from sync left every hinted channel reading INCOMPLETE
        for the rest of the session, long after the message turned up.
        """
        self._note_reference(channel_hash_hex, message_id)
        if not self._status.has_gap(channel_hash_hex):
            return
        my_hex = self._identity.hash_hex
        outstanding = self._storage.get_missed_message_ids(channel_hash_hex, my_hex)
        # No hints means the gap was recorded for rows we refused, which the
        # arrival of some other message says nothing about.
        if not outstanding or any(not self._storage.has_message(channel_hash_hex, mid)
                                  for mid in outstanding):
            return
        self._storage.clear_missed_deliveries(channel_hash_hex, my_hex)
        self._status.clear_gap(channel_hash_hex)

    def _note_reference(self, channel_hash_hex: str, message_id: str) -> None:
        """Remember a last_seen_id we do not hold, to ask its author for.

        The row it came from says who saw the missing message, so that peer
        can be asked for exactly it, by prefix, without any description of
        the window. Asked on a later tick rather than now: within the causal
        window the message is more likely still in flight than lost.
        """
        row = self._storage.get_message(channel_hash_hex, message_id)
        if row is None:
            return
        referenced = row["last_seen_id"]
        author = row["sender_hash"]
        if (not referenced or not author or author == self._identity.hash_hex
                or not _is_message_id(referenced)
                or self._storage.has_message(channel_hash_hex, referenced)):
            return
        key = (channel_hash_hex, referenced)
        with self._suspected_lock:
            if key in self._suspected or len(self._suspected) >= MAX_SUSPECTED_GAPS:
                return
            self._suspected[key] = (time.time() + SUSPECTED_GAP_GRACE_SECS, author,
                                    float(row["timestamp"]))

    def _ask_suspected_gaps(self) -> None:
        """Ask each author for the referenced rows still missing, one need
        request per (channel, author). Asked once: if the answer never comes,
        the ordinary reconcile still covers the gap."""
        now = time.time()
        due: dict[tuple[str, str], list] = {}
        with self._suspected_lock:
            for (channel_hash_hex, referenced), (ask_after, author, ts) in list(
                    self._suspected.items()):
                if ask_after > now:
                    continue
                del self._suspected[(channel_hash_hex, referenced)]
                if self._storage.has_message(channel_hash_hex, referenced):
                    continue
                due.setdefault((channel_hash_hex, author), []).append(
                    [ts - SYNC_WINDOW_SECS, ts + SYNC_CLOCK_SKEW_SECS,
                     sync_ranges.id_prefix(referenced)]
                )
        for (channel_hash_hex, author), needs in due.items():
            if not self._storage.is_subscribed(channel_hash_hex):
                continue
            if not self._peer_may_participate(channel_hash_hex, author):
                continue
            since_ts = self._storage.get_peer_sync_progress(channel_hash_hex, author)
            self._send_sync_request(author, channel_hash_hex, since_ts,
                                    needs=needs[:MAX_NEED_IDS])

    def _on_member_list_updated(self, channel_hash_hex: str):
        """Clear pending outbound messages for this channel if we were removed.

        When a member list update is accepted and the local identity is no
        longer in the roster, any queued messages for that channel cannot be
        delivered to legitimate members.  Discarding them prevents the kicked
        client from accumulating messages during the gap that could later be
        replayed after re-admission.
        """
        my_hex = self._identity.hash_hex
        if not self._storage.is_member(channel_hash_hex, my_hex):
            self._messaging.cancel_pending_for_channel(channel_hash_hex)
            RNS.log(
                f"TrenchChat [sync]: local identity removed from "
                f"{channel_hash_hex[:12]}… — pending outbound messages cleared",
                RNS.LOG_NOTICE,
            )

    def on_peer_appeared(self, peer_hex: str):
        """
        Called by PeerAnnounceHandler when a peer broadcasts their delivery
        destination.  Flush any pending outbound messages for them, then ask
        them for anything we may have missed on shared channels.
        """
        if peer_hex == self._identity.hash_hex:
            return

        # The announce carries this peer's identity, so anything of theirs we
        # quarantined can now be verified.
        self._router.release_quarantined(peer_hex)

        self._messaging.flush_pending(peer_hex)
        self._retry.flush(peer_hex, self._send_raw)
        self._flush_pending_hints(peer_hex)

        # Send sync requests for every channel we share with this peer, from
        # our own progress with this specific peer -- not the channel-wide
        # watermark, which a different peer's disjoint history may have
        # advanced past everything this one holds (A1, test_sync_multipeer.py).
        for sub in self._storage.get_subscriptions():
            channel_hash_hex = sub["channel_hash"]
            if peer_hex not in self._get_channel_peers(channel_hash_hex):
                continue
            if not self._announce_sync_due(channel_hash_hex, peer_hex):
                continue
            # A beacon probe that matched is the answer this request would
            # have fetched; asking anyway spends a request and a reply on it.
            if self._probe_agreed_recently(channel_hash_hex, peer_hex):
                continue
            since_ts = self._storage.get_peer_sync_progress(channel_hash_hex, peer_hex)
            if self._send_sync_request(peer_hex, channel_hash_hex, since_ts):
                self._record_announce_sync(channel_hash_hex, peer_hex)

    # --- missed-delivery hint broadcast ---

    def _on_missed_delivery_event(self, channel_hash_hex: str, missed_peer_hex: str,
                                   msg_id: str, subscriber_hashes: list[str]):
        """
        Called by Messaging when delivery to missed_peer_hex failed.
        Broadcast a MT_MISSED_DELIVERY hint to all currently-reachable
        subscribers so they can serve the message when B reconnects.
        """
        for dest_hex in subscriber_hashes:
            if dest_hex in (self._identity.hash_hex, missed_peer_hex):
                continue
            fields = {
                F_MSG_TYPE:      MT_MISSED_DELIVERY,
                F_CHANNEL_HASH:  bytes.fromhex(channel_hash_hex),
                F_MISSED_FOR:    missed_peer_hex,
                F_MISSED_MSG_ID: message_id_to_wire(msg_id),
            }
            if not self._send_raw(dest_hex, fields):
                self._queue_hint_for_retry(dest_hex, fields)

        # Record the hint locally too (we are also a potential responder)
        self._storage.record_missed_delivery(channel_hash_hex, missed_peer_hex, msg_id)

    def _queue_hint_for_retry(self, dest_hex: str, fields: dict):
        """Request the peer's path and queue a hint that failed to send.

        Mirrors Messaging._on_delivery_failed's request_path + queue pattern:
        unlike a chat message, a missed-delivery hint has no other way back
        once _send_raw fails, so without this it is lost even after the
        target peer becomes reachable again.
        """
        try:
            identity_hash = bytes.fromhex(dest_hex)
            delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
            RNS.Transport.request_path(delivery_dest_hash)
        except (ValueError, TypeError) as e:
            RNS.log(
                f"TrenchChat [sync]: could not request path for {dest_hex[:12]}…: {e}",
                RNS.LOG_WARNING,
            )
            return
        with self._pending_hints_lock:
            queued = self._pending_hints.setdefault(dest_hex, [])
            queued.append(fields)
            if len(queued) > MAX_QUEUED_HINTS_PER_PEER:
                del queued[0]

    def _flush_pending_hints(self, dest_hex: str):
        """Retry any missed-delivery hints queued for a peer now reachable."""
        with self._pending_hints_lock:
            queued = self._pending_hints.pop(dest_hex, [])
        for fields in queued:
            if not self._send_raw(dest_hex, fields):
                self._queue_hint_for_retry(dest_hex, fields)

    # --- inbound message handler ---

    def _on_lxmf_message(self, message: LXMF.LXMessage):
        fields = message.fields or {}
        msg_type = fields.get(F_MSG_TYPE)
        if msg_type is None:
            return
        if isinstance(msg_type, bytes):
            msg_type = msg_type.decode(errors="replace")
        if msg_type == MT_PRESENCE:
            if F_SYNC_PROBE in fields:
                self._handle_probes(fields, self._sender_hex(message))
            return
        if msg_type not in (MT_MISSED_DELIVERY, MT_SYNC_REQUEST, MT_SYNC_RESPONSE):
            return

        channel_hash_bytes = fields.get(F_CHANNEL_HASH)
        if not channel_hash_bytes:
            return
        channel_hash_hex = (channel_hash_bytes.hex()
                            if isinstance(channel_hash_bytes, bytes)
                            else str(channel_hash_bytes))
        sender_hex = self._sender_hex(message)

        if msg_type == MT_MISSED_DELIVERY:
            self._handle_missed_delivery(fields, channel_hash_hex, sender_hex)
        elif msg_type == MT_SYNC_REQUEST:
            self._handle_sync_request(fields, channel_hash_hex, sender_hex)
        elif msg_type == MT_SYNC_RESPONSE:
            self._handle_sync_response(fields, channel_hash_hex, sender_hex)

    @staticmethod
    def _sender_hex(message: LXMF.LXMessage) -> str:
        """The sender's identity hex, falling back to its delivery hash."""
        sender_identity = (RNS.Identity.recall(message.source_hash)
                           if message.source_hash else None)
        if sender_identity:
            return sender_identity.hash.hex()
        return message.source_hash.hex() if message.source_hash else ""

    # --- probes ---

    def local_probe(self, channel_hash_hex: str, now: float | None = None) -> list:
        """Our own [count, fingerprint] for a channel over the probed span."""
        floor = sync_ranges.probe_floor(time.time() if now is None else now)
        rows = sync_ranges.signed_rows(
            self._storage.get_message_index(channel_hash_hex, floor, math.inf)
        )
        return sync_ranges.probe(rows)

    def _handle_probes(self, fields: dict, sender_hex: str) -> None:
        """Compare what a peer's beacon says it holds against what we hold.

        Agreement stands in for the next announce-driven re-check of that
        pair. Disagreement is acted on once per change in what the peer
        reports: a peer whose view legitimately differs from ours (a member
        with less history entitlement, say) would otherwise cost an exchange
        on every beacon, and a peer inventing a fingerprint can draw no more
        than the announce cooldown already allows.
        """
        probes = sync_ranges.unpack_probes(fields.get(F_SYNC_PROBE))
        if probes is None:
            RNS.log(
                f"TrenchChat [sync]: ignoring malformed probes on a beacon from "
                f"{sender_hex[:12]}…",
                RNS.LOG_WARNING,
            )
            return
        if not sender_hex:
            return
        now = time.time()
        to_ask: list[tuple[str, str]] = []
        for channel_bytes, count, digest in probes:
            channel_hash_hex = channel_bytes.hex()
            if not self._storage.is_subscribed(channel_hash_hex):
                continue
            if not self._peer_may_participate(channel_hash_hex, sender_hex):
                continue
            key = (channel_hash_hex, sender_hex)
            mine = self.local_probe(channel_hash_hex, now)
            with self._probe_lock:
                self._prune_probes_locked(now)
                seen = self._probe_seen.get(key)
                self._probe_seen[key] = (now, digest)
                if mine == [count, digest]:
                    self._probe_agreed[key] = now
                    self._probe_pending.discard(key)
                    continue
                if seen is not None and seen[1] == digest:
                    continue
                self._probe_pending.add(key)
                to_ask.append(key)
        for key in to_ask:
            self._ask_after_probe(*key)

    def _ask_after_probe(self, channel_hash_hex: str, peer_hex: str) -> bool:
        """Send the ladder to a peer whose probe disagreed, if the cooldown
        allows; otherwise it stays pending for tick()."""
        if not self._announce_sync_due(channel_hash_hex, peer_hex):
            return False
        since_ts = self._storage.get_peer_sync_progress(channel_hash_hex, peer_hex)
        sent = self._send_sync_request(peer_hex, channel_hash_hex, since_ts, ladder=True)
        if sent:
            self._record_announce_sync(channel_hash_hex, peer_hex)
            with self._probe_lock:
                self._probe_pending.discard((channel_hash_hex, peer_hex))
        return sent

    def _flush_probe_pending(self) -> None:
        with self._probe_lock:
            pending = list(self._probe_pending)
        for channel_hash_hex, peer_hex in pending:
            if not self._storage.is_subscribed(channel_hash_hex):
                with self._probe_lock:
                    self._probe_pending.discard((channel_hash_hex, peer_hex))
                continue
            self._ask_after_probe(channel_hash_hex, peer_hex)

    def _probe_agreed_recently(self, channel_hash_hex: str, peer_hex: str) -> bool:
        with self._probe_lock:
            at = self._probe_agreed.get((channel_hash_hex, peer_hex))
        return at is not None and time.time() - at < PROBE_AGREE_SECS

    def _prune_probes_locked(self, now: float) -> None:
        for key, (at, _digest) in list(self._probe_seen.items()):
            if now - at > ANNOUNCE_SYNC_PRUNE_SECS:
                del self._probe_seen[key]
        for key, at in list(self._probe_agreed.items()):
            if now - at > ANNOUNCE_SYNC_PRUNE_SECS:
                del self._probe_agreed[key]

    # --- handlers ---

    def _peer_may_participate(self, channel_hash_hex: str, peer_hex: str) -> bool:
        """Return True if peer_hex is entitled to take part in this channel's sync.

        An unknown channel is treated as closed, so a missing record can never
        widen access.
        """
        if not peer_hex:
            return False
        channel = self._storage.get_channel(channel_hash_hex)
        if channel is None:
            return False
        if is_open_join(permissions_from_json(channel["permissions"])):
            return True
        return self._storage.is_member(channel_hash_hex, peer_hex)

    def _handle_missed_delivery(self, fields: dict, channel_hash_hex: str,
                                sender_hex: str):
        if not self._peer_may_participate(channel_hash_hex, sender_hex):
            return
        missed_for = _coerce_str(fields.get(F_MISSED_FOR, ""))
        missed_msg_id = message_id_from_wire(fields.get(F_MISSED_MSG_ID))
        # Both are free-form strings on the wire and each distinct pair is a
        # persistent row, so they are checked for shape rather than taken as
        # given: a message_id is the hex digest messaging computes, and a
        # recipient is an identity hash.
        if not _is_identity_hex(missed_for) or not _is_message_id(missed_msg_id):
            RNS.log(
                f"TrenchChat [sync]: ignoring a malformed missed-delivery hint "
                f"from {sender_hex[:12]}…",
                RNS.LOG_WARNING,
            )
            return
        self._storage.record_missed_delivery(channel_hash_hex, missed_for, missed_msg_id)
        if (missed_for == self._identity.hash_hex
                and not self._storage.has_message(channel_hash_hex, missed_msg_id)):
            self._status.note_gap(channel_hash_hex)

    def _handle_sync_request(self, fields: dict, channel_hash_hex: str,
                              requester_hex: str):
        # Every refusal below is silent on the wire -- the requester cannot
        # tell one from a lost packet -- so each says why in the log. Without
        # that, "nobody answered" is indistinguishable from "nobody heard".
        if not self._storage.is_subscribed(channel_hash_hex):
            RNS.log(
                f"TrenchChat [sync]: ignoring request from {requester_hex[:12]}… "
                f"for {channel_hash_hex[:12]}… — we are not subscribed to it",
                RNS.LOG_DEBUG,
            )
            return

        # Fails closed on an unknown channel.
        if not self._peer_may_participate(channel_hash_hex, requester_hex):
            RNS.log(
                f"TrenchChat [sync]: refusing request from {requester_hex[:12]}… "
                f"for {channel_hash_hex[:12]}… — not a participant",
                RNS.LOG_DEBUG,
            )
            return
        channel = self._storage.get_channel(channel_hash_hex)

        window_start_raw = fields.get(F_SYNC_WINDOW_START, 0.0)
        try:
            window_start = float(window_start_raw)
        except (TypeError, ValueError):
            window_start = time.time() - SYNC_WINDOW_SECS
        window_start = max(window_start, 0.0)

        # How far we have actually served this peer. Read from sync_served,
        # not sync_progress: the latter records what we received *from* them,
        # and using it here collapsed the two directions onto one row (A7).
        served_progress = self._storage.get_peer_served_progress(
            channel_hash_hex, requester_hex
        )

        ranges, needs, malformed = self._read_reconcile_fields(fields)
        if malformed:
            RNS.log(
                f"TrenchChat [sync]: refusing a malformed request from "
                f"{requester_hex[:12]}… for {channel_hash_hex[:12]}…",
                RNS.LOG_WARNING,
            )
            return
        reconciling = ranges is not None or needs is not None
        continues = reconciling and bool(fields.get(F_SYNC_CONTINUES, False))

        # Hints name exact messages this peer missed, including ones older than
        # their window.  They supplement the timestamp sweep rather than
        # replacing it: a hint naming a message we don't hold resolves to
        # nothing, and letting that stand as the whole answer would starve the
        # requester of everything else we have until the hint ages out.
        missed_ids = self._storage.get_missed_message_ids(channel_hash_hex, requester_hex)
        hinted_rows = self._filter_rows_by_tenure(
            channel, channel_hash_hex, requester_hex,
            self._get_messages_by_ids(channel_hash_hex, missed_ids),
        ) if missed_ids else []

        # A request reaching further back than the recent window is a
        # "deep" backfill; rate-limited so a flood of requests can't
        # repeatedly force a full timestamp sweep. A recent request is
        # unaffected and always answered immediately. A request that names
        # ranges is judged by how far back they actually reach, with the same
        # allowance for clock skew that built them: our own clock decides
        # this, and a peer whose clock trails ours must not be classified deep
        # for asking about the window it believes it is in.
        if reconciling:
            deep = any(lo < time.time() - SYNC_WINDOW_SECS - SYNC_CLOCK_SKEW_SECS
                       for lo, _hi, _mode, _payload in (ranges or []))
        else:
            deep = window_start < time.time() - SYNC_WINDOW_SECS
        if deep and not self._deep_sync_allowed(channel_hash_hex, requester_hex,
                                                continues):
            RNS.log(
                f"TrenchChat [sync]: deep sync request from "
                f"{requester_hex[:12]}… for {channel_hash_hex[:12]}… "
                f"throttled — cooldown active",
                RNS.LOG_DEBUG,
            )
            return

        reply_ranges: list = []
        my_needs: list = []
        scan_cursor = 0.0
        if reconciling:
            # No frontier rule here: a hint rides along with the diff, and the
            # requester no longer resumes from a watermark that serving one
            # out of order could run past.
            diff_rows, reply_ranges, my_needs, truncated = self._reconcile_for_requester(
                channel, channel_hash_hex, requester_hex, ranges, needs
            )
            rows = self._merge_rows(hinted_rows, diff_rows)
        else:
            # A peer we've never actually served before gets the benefit of
            # the doubt for PEER_TRUST_HORIZON_SECS behind their claimed
            # window_start -- their own channel-wide watermark can be inflated
            # by a different responder's disjoint answer landing first in the
            # same reconnect round (A1, test_sync_multipeer.py). A peer we
            # HAVE served keeps resuming from what we actually gave them, not
            # from this horizon. A reconciled request needs none of this: it
            # says which rows it holds rather than where it left off.
            trust_floor = max(served_progress,
                              window_start - PEER_TRUST_HORIZON_SECS, 0.0)
            sweep_start = min(window_start, trust_floor)
            swept_rows, truncated, scan_cursor = self._collect_permitted_rows(
                channel, channel_hash_hex, requester_hex, sweep_start
            )

            # A hinted message newer than the sweep reached has to wait for the
            # sweep to reach it. The requester advances its watermark to the newest
            # message in the response, so sending one out of band would strand
            # every message between it and where the sweep actually got to.
            frontier = swept_rows[-1]["timestamp"] if swept_rows else sweep_start
            rows = self._merge_rows(
                [r for r in hinted_rows if r["timestamp"] <= frontier], swept_rows
            )
        rows, capped = _truncate_at_group_boundary(rows)
        truncated = truncated or capped

        packed = msgpack.packb(
            [self._row_to_payload(r) for r in rows],
            use_bin_type=True,
        )
        response_fields = {
            F_MSG_TYPE:       MT_SYNC_RESPONSE,
            F_CHANNEL_HASH:   bytes.fromhex(channel_hash_hex),
            F_SYNC_MESSAGES:  packed,
            F_SYNC_TRUNCATED: truncated,
        }
        author_keys = self._author_keys_for(rows)
        if author_keys:
            response_fields[F_AUTHOR_KEYS] = author_keys
        if reply_ranges:
            response_fields[F_SYNC_RANGES] = sync_ranges.pack(reply_ranges)
        if my_needs:
            response_fields[F_SYNC_NEED] = sync_ranges.pack(my_needs)
        # Lets the requester's next request resume past a run of rows it was
        # scanned but not entitled to, without touching its persisted
        # watermark -- see _handle_sync_response's resume_ts handling.
        if truncated and not reconciling:
            response_fields[F_SYNC_SCAN_CURSOR] = scan_cursor

        # Asking for rows in a response makes this an outstanding request of
        # our own: the requester answers it with a response, which is dropped
        # as unsolicited unless we recorded it first.
        need_req_id = (self._record_pending_request(channel_hash_hex, requester_hex)
                       if my_needs else None)
        sent = self._send_raw(requester_hex, response_fields)
        if my_needs:
            if sent:
                self._status.request_sent(channel_hash_hex, requester_hex)
            else:
                self._drop_pending_request(channel_hash_hex, requester_hex, need_req_id)
        RNS.log(
            f"TrenchChat [sync]: {'answered' if sent else 'held answer for'} "
            f"{requester_hex[:12]}… on {channel_hash_hex[:12]}… — {len(rows)} row(s), "
            f"truncated={truncated}",
            RNS.LOG_DEBUG,
        )

        # Remember how far we've actually scanned for this peer, so a later
        # request from them resumes from real, confirmed progress instead of
        # re-widening under the trust horizon every time. scan_cursor tracks
        # this regardless of what was withheld, which is what lets a chain of
        # truncated, all-withheld batches (D2, TestSweepScanCap) keep advancing
        # instead of re-triggering deep-sync classification on every batch.
        if sent:
            advance_to = scan_cursor
            if rows:
                advance_to = max(advance_to, rows[-1]["timestamp"])
            if advance_to > served_progress:
                self._storage.advance_peer_served_progress(
                    channel_hash_hex, requester_hex, advance_to
                )

        # A hint we just served has done its job. The requester clears only its
        # own hints, and a hint naming them is never broadcast to them, so
        # without this the row lives here until the window purge.
        if sent and missed_ids:
            served = {r["message_id"] for r in rows}
            if served.issuperset(missed_ids):
                self._storage.clear_missed_deliveries(channel_hash_hex, requester_hex)

    @staticmethod
    def _read_reconcile_fields(fields: dict) -> tuple[list | None, list | None, bool]:
        """The ranges and needs a message carries, and whether either is bad.

        A field that does not parse is refused whole rather than in part:
        acting on the half that parsed would mean answering against a set the
        peer never actually described.
        """
        ranges_raw = fields.get(F_SYNC_RANGES)
        needs_raw = fields.get(F_SYNC_NEED)
        ranges = sync_ranges.unpack_ranges(ranges_raw) if ranges_raw is not None else None
        needs = sync_ranges.unpack_needs(needs_raw) if needs_raw is not None else None
        malformed = ((ranges_raw is not None and ranges is None)
                     or (needs_raw is not None and needs is None))
        return ranges, needs, malformed

    def _reconcile_for_requester(self, channel, channel_hash_hex: str,
                                 requester_hex: str, ranges: list | None,
                                 needs: list | None) -> tuple[list, list, list, bool]:
        """Compare a requester's description of its own rows against ours.

        Returns the rows it is missing, how to describe any range that still
        differs, the rows we are missing ourselves, and whether more remains
        than one response can carry: rows past the cap, or descriptions past
        the budget. Either way the requester asks again from what it then
        holds, so nothing left out here is lost.

        What we may send is the serving view: signed rows this requester's
        tenure entitles it to, so a withheld row is simply absent from every
        description we produce. What we ask for is measured against everything
        we hold, so a row we withhold is never requested back.
        """
        send_ids: list[str] = []
        reply_ranges: list = []
        my_needs: list = []
        deferred = False

        for lo, hi, mode, payload in ranges or []:
            index = self._storage.get_message_index(channel_hash_hex, lo, hi)
            serving = self._filter_rows_by_tenure(
                channel, channel_hash_hex, requester_hex, index
            )
            if mode == RANGE_IDLIST:
                theirs_missing = sync_ranges.ids_they_lack(serving, payload)
                send_ids.extend(theirs_missing)
                for prefix in sync_ranges.prefixes_we_lack(index, payload):
                    if len(my_needs) < MAX_NEED_IDS:
                        my_needs.append([lo, hi, prefix])
            elif not sync_ranges.matches_fingerprint(serving, payload[0], payload[1]):
                if not sync_ranges.append_ranges(
                        reply_ranges, sync_ranges.describe(serving, lo, hi)):
                    deferred = True

        rows = self._get_messages_by_ids(channel_hash_hex, send_ids)
        if needs:
            rows = self._merge_rows(
                rows, self._rows_for_needs(channel, channel_hash_hex,
                                           requester_hex, needs)
            )
        more = deferred or len(send_ids) > MAX_RESPONSE_MESSAGES
        return rows, reply_ranges, my_needs, more

    def _rows_for_needs(self, channel, channel_hash_hex: str, peer_hex: str,
                        needs: list) -> list:
        """The rows a peer named by id prefix, filtered as any answer is.

        Needs are grouped by the span they name so a message asking for fifty
        rows costs one index read per span, not fifty.
        """
        by_span: dict[tuple[float, float], set[bytes]] = {}
        for lo, hi, prefix in needs[:MAX_NEED_IDS]:
            by_span.setdefault((lo, hi), set()).add(prefix)

        wanted: list[str] = []
        for (lo, hi), prefixes in by_span.items():
            for row in self._storage.get_message_index(channel_hash_hex, lo, hi):
                if sync_ranges.id_prefix(row["message_id"]) in prefixes:
                    wanted.append(row["message_id"])
        rows = self._get_messages_by_ids(channel_hash_hex, wanted)
        return self._filter_rows_by_tenure(channel, channel_hash_hex, peer_hex, rows)

    def _merge_rows(self, *row_sets: list) -> list:
        """Combine row lists, dropping duplicate ids and ordering oldest first."""
        by_id: dict = {}
        for rows in row_sets:
            for row in rows:
                by_id.setdefault(row["message_id"], row)
        return sorted(by_id.values(), key=lambda r: r["timestamp"])

    def _collect_permitted_rows(self, channel, channel_hash_hex: str,
                                requester_hex: str,
                                window_start: float) -> tuple[list, bool, float]:
        """Sweep forward from window_start for rows this requester may see.

        Scanning past withheld rows here, instead of returning a batch that
        tenure filtering emptied, is what keeps a requester from stalling
        behind history they will never be shown.  The alternative -- telling
        them to resume past what we withheld -- would bake a permission
        decision into their watermark permanently, so history withheld while
        a role or grant was still propagating could never be recovered.

        Rows are grouped by timestamp as they're scanned, and a group is
        only ever included, or resumed from, as a whole. since_ts travels
        over the wire as a bare float with no row-id tie-breaker, so a batch
        or a scan cursor that split a tied-timestamp group down the middle
        would strand whichever half landed on the wrong side forever -- a
        real risk on coarse clocks. A single group larger than
        MAX_RESPONSE_MESSAGES is still shipped whole rather than stalling.
        The internal (timestamp, row id) cursor into Storage.get_messages_after
        is what lets a group spanning an internal page boundary be scanned
        as one run in the first place.

        Returns the rows to send, whether more remain beyond them, and the
        furthest timestamp whose group was fully resolved -- which may be
        past every row returned, when an entire group was withheld outright.
        """
        permitted: list = []
        permitted_bytes = 0
        scan_cursor = window_start
        cursor_ts = window_start
        cursor_id: int | None = None
        scanned = 0
        truncated = False

        run_ts: float | None = None
        run_rows: list = []

        def try_flush_run() -> bool:
            """Add the just-finished timestamp group to permitted, unless
            doing so would exceed the response cap -- in which case the
            whole group is left for the next request instead of being split.
            """
            nonlocal scan_cursor, truncated, permitted_bytes
            if not run_rows:
                return True
            filtered = self._filter_rows_by_tenure(
                channel, channel_hash_hex, requester_hex, run_rows
            )
            group_bytes = sum(row_wire_size(r) for r in filtered)
            over_rows = len(permitted) + len(filtered) > MAX_RESPONSE_MESSAGES
            over_bytes = permitted and permitted_bytes + group_bytes > MAX_RESPONSE_BYTES
            if over_rows or over_bytes:
                truncated = True
                return False
            permitted.extend(filtered)
            permitted_bytes += group_bytes
            scan_cursor = run_ts
            return True

        while True:
            page = self._storage.get_messages_after(
                channel_hash_hex, cursor_ts, MAX_RESPONSE_MESSAGES, after_id=cursor_id
            )
            if not page:
                try_flush_run()
                break

            stop = False
            for row in page:
                if run_ts is not None and row["timestamp"] != run_ts:
                    flushed = try_flush_run()
                    run_rows = []
                    if not flushed:
                        stop = True
                        break
                    if scanned >= MAX_SWEEP_SCAN:
                        truncated = True
                        stop = True
                        break

                run_ts = row["timestamp"]
                run_rows.append(row)
                cursor_ts = row["timestamp"]
                cursor_id = row["id"]
                scanned += 1

            if stop:
                break
            if len(page) < MAX_RESPONSE_MESSAGES:
                try_flush_run()
                break

        if truncated:
            RNS.log(
                f"TrenchChat [sync]: sweep for {requester_hex[:12]}… on "
                f"{channel_hash_hex[:12]}… stopped after {scanned} rows scanned, "
                f"{len(permitted)} permitted",
                RNS.LOG_DEBUG,
            )

        return permitted, truncated, scan_cursor

    def _filter_rows_by_tenure(self, channel, channel_hash_hex: str,
                               requester_hex: str, rows: list) -> list:
        """Drop rows we will not relay: unsigned ones, then tenure failures.

        An unsigned row is withheld because the requester would reject it on
        arrival, and a rejected row advances nothing -- they would re-request
        the same window forever. Withholding lets the sweep scan past it and
        report a scan cursor instead, which is exactly how tenure-withheld
        rows already behave. Rows predating author signatures are the only
        ones this affects.

        The tenure checks below are only applied when tenure data exists for
        the channel (skips open-join
        channels and channels bootstrapped before this feature). Two
        independent checks:
          - sender: the claimed author must actually have been a member at
            that timestamp, or the message could be a kicked member's replay
            or an outright forgery.
          - requester (unless they hold the full_sync permission): the peer
            asking for sync must themselves have been a member at that
            timestamp, or sync becomes a way to backfill history from before
            they ever joined. full_sync is a per-role permission (like
            send_message/invite/...), off by default -- an admin grants it to
            whichever role(s) should be able to backfill full history, e.g.
            admin but not member.
        """
        perms = permissions_from_json(channel["permissions"]) if channel else {}

        signed_rows = []
        for r in rows:
            if not _row_is_signed(r):
                RNS.log(
                    f"TrenchChat [sync]: withholding unsigned message "
                    f"{r['message_id'][:12]}… — it cannot be verified by the "
                    f"requester",
                    RNS.LOG_DEBUG,
                )
                continue
            signed_rows.append(r)
        rows = signed_rows

        if not self._storage.has_any_tenure(channel_hash_hex):
            return rows

        requester_role = self._storage.get_role(channel_hash_hex, requester_hex)
        full_sync = has_permission(perms, requester_role, FULL_SYNC)
        valid_rows = []
        for r in rows:
            if not self._storage.was_member_at(channel_hash_hex, r["sender_hash"],
                                                r["timestamp"]):
                RNS.log(
                    f"TrenchChat [sync]: omitting message {r['message_id'][:12]}… "
                    f"from sync response — sender {r['sender_hash'][:12]}… "
                    f"was not a member at ts={r['timestamp']:.0f}",
                    RNS.LOG_WARNING,
                )
                continue
            if not full_sync and not self._storage.was_member_at(
                channel_hash_hex, requester_hex, r["timestamp"]
            ):
                RNS.log(
                    f"TrenchChat [sync]: omitting message {r['message_id'][:12]}… "
                    f"from sync response — requester {requester_hex[:12]}… "
                    f"was not yet a member at ts={r['timestamp']:.0f}",
                    RNS.LOG_DEBUG,
                )
                continue
            valid_rows.append(r)
        return valid_rows

    def _handle_sync_response(self, fields: dict, channel_hash_hex: str,
                              responder_hex: str = ""):
        if not self._storage.is_subscribed(channel_hash_hex):
            return

        # Membership can change while a request is in flight, and tenure only
        # says what we were entitled to when each message was sent -- this is
        # the only check on whether we are still entitled to receive any of it.
        if not self._peer_may_participate(channel_hash_hex, self._identity.hash_hex):
            RNS.log(
                f"TrenchChat [sync]: dropping sync response for "
                f"{channel_hash_hex[:12]}… — we are no longer a member",
                RNS.LOG_WARNING,
            )
            return

        # The gate is that we asked this peer for this channel, not that they
        # are a member: by design any reachable peer may serve history and our
        # local roster need not list them.
        claim = self._claim_pending_request(channel_hash_hex, responder_hex)
        if claim is None:
            RNS.log(
                f"TrenchChat [sync]: dropping unsolicited sync response for "
                f"{channel_hash_hex[:12]}… from {responder_hex[:12]}…",
                RNS.LOG_WARNING,
            )
            return
        requested_since, peer_hex = claim
        self._clear_retry_budget(channel_hash_hex, peer_hex)

        # A malformed answer still answers the request.  Leaving the peer
        # pending would strand the channel reporting "syncing" for the rest of
        # the session, since nothing else ever resolves that request.
        packed = fields.get(F_SYNC_MESSAGES)
        messages = None
        if packed:
            try:
                unpacked = unpack_wire(packed)
            except Exception as e:
                RNS.log(f"TrenchChat: sync_response unpack error: {e}", RNS.LOG_WARNING)
            else:
                if isinstance(unpacked, list):
                    messages = unpacked
                else:
                    RNS.log("TrenchChat: sync_response payload is not a list",
                            RNS.LOG_WARNING)
        if messages is None:
            self._status.response_malformed(channel_hash_hex, peer_hex)
            return

        has_tenure = self._storage.has_any_tenure(channel_hash_hex)
        my_hex = self._identity.hash_hex
        full_sync = False
        channel = self._storage.get_channel(channel_hash_hex)
        perms = permissions_from_json(channel["permissions"]) if channel else {}
        if has_tenure and channel:
            my_role = self._storage.get_role(channel_hash_hex, my_hex)
            full_sync = has_permission(perms, my_role, FULL_SYNC)
        self._learn_author_keys(fields.get(F_AUTHOR_KEYS))
        inserted_count = 0
        # Rows refused for failing verification -- as opposed to ones our own
        # tenure checks withheld, which we are simply not entitled to. Only
        # the former means history is missing.
        rejected_count = 0
        accepted_ts: list[float] = []
        failed_ts: float | None = None
        for m in messages:
            try:
                sender_hash = m.get("sender_hash", "")
                msg_id = message_id_from_wire(m.get("message_id"))
                # Dropped rather than clamped, unlike direct delivery: an
                # accepted row advances our persisted watermark, so taking a
                # far-future timestamp here would stop us ever asking this
                # peer for older history again.
                checked_ts = wire_timestamp(m.get("timestamp"))
                if checked_ts is None:
                    RNS.log(
                        f"TrenchChat [sync]: dropping synced message "
                        f"{msg_id[:12]}… — implausible "
                        f"timestamp {m.get('timestamp')!r}",
                        RNS.LOG_WARNING,
                    )
                    rejected_count += 1
                    continue
                msg_ts = checked_ts

                # Validate tenure for invite-only channels with tenure data.
                # Mirrors _handle_sync_request's two checks -- applied again
                # here on receipt (not just by whoever responded) so a
                # malicious or buggy responder can't hand us history we
                # aren't entitled to just by skipping its own filtering.
                if has_tenure and not self._storage.was_member_at(
                    channel_hash_hex, sender_hash, msg_ts
                ):
                    RNS.log(
                        f"TrenchChat [sync]: dropping synced message "
                        f"{msg_id[:12]}… — sender "
                        f"{sender_hash[:12]}… was not a member at ts={msg_ts:.0f}",
                        RNS.LOG_WARNING,
                    )
                    continue
                if has_tenure and not full_sync and not self._storage.was_member_at(
                    channel_hash_hex, my_hex, msg_ts
                ):
                    RNS.log(
                        f"TrenchChat [sync]: dropping synced message "
                        f"{msg_id[:12]}… — we were not "
                        f"yet a member at ts={msg_ts:.0f}",
                        RNS.LOG_DEBUG,
                    )
                    continue

                image_data = m.get("image_data")
                if isinstance(image_data, str):
                    image_data = image_data.encode()
                if not image_data:
                    image_data = None

                content = m.get("content", "")
                if isinstance(content, bytes):
                    content = content.decode(errors="replace")
                reply_to = message_id_from_wire(m.get("reply_to")) or None
                last_seen_id = message_id_from_wire(m.get("last_seen_id")) or None

                # Recomputed exactly as the direct path does: the id *is* the
                # hash of the row's own content, so a relayed row whose id was
                # minted for another sender's message can't squat the UNIQUE id
                # column and suppress the genuine copy forever.
                expected_id = _compute_message_id(content, sender_hash, msg_ts)
                if not msg_id:
                    msg_id = expected_id
                elif msg_id != expected_id:
                    RNS.log(
                        f"TrenchChat [sync]: dropping synced message "
                        f"{msg_id[:12]}… — message_id is not the hash of its "
                        f"content",
                        RNS.LOG_WARNING,
                    )
                    rejected_count += 1
                    failed_ts = msg_ts if failed_ts is None else min(failed_ts, msg_ts)
                    continue

                # Checked against the row exactly as the responder sent it,
                # before anything is stripped. This is what makes a relayed
                # message verifiable independently of who relayed it: the
                # peer handing it over is almost never its author.
                author_sig = m.get("author_sig")
                if not verify_message(
                        self._storage, sender_hash, author_sig,
                        channel_hash_hex, msg_id, msg_ts,
                        content, reply_to, last_seen_id, image_data):
                    RNS.log(
                        f"TrenchChat [sync]: dropping synced message "
                        f"{msg_id[:12]}… — author "
                        f"signature missing or invalid",
                        RNS.LOG_WARNING,
                    )
                    rejected_count += 1
                    # Bounds the watermark, exactly like a failed insert: the
                    # row is real history we could not verify *yet* -- an
                    # author whose key we have never learned reads the same as
                    # a forgery here -- and resuming past it would hide it from
                    # every future sweep. A row with an implausible timestamp
                    # above is different: it cannot be placed at all, so
                    # letting it bound anything would let one bad row freeze
                    # sync for good.
                    failed_ts = msg_ts if failed_ts is None else min(failed_ts, msg_ts)
                    continue

                image_stripped = False
                if image_data is not None and (
                        len(image_data) > MAX_IMAGE_BYTES
                        or not inbound_image_is_sane(image_data)):
                    image_data = None
                    author_sig = None
                    image_stripped = True

                inserted = self._storage.insert_message(
                    channel_hash=channel_hash_hex,
                    sender_hash=sender_hash,
                    sender_name=m.get("sender_name", ""),
                    content=content,
                    timestamp=msg_ts,
                    message_id=msg_id,
                    reply_to=reply_to,
                    last_seen_id=last_seen_id,
                    received_at=time.time(),
                    image_data=image_data,
                    author_sig=author_sig,
                    image_stripped=image_stripped,
                )
                # A duplicate we already hold is still "accepted" -- the
                # watermark should move past it. A failed insert for an id
                # that lives in another channel is not: nothing landed here,
                # so advancing would skip history we never received.
                if inserted or self._storage.has_message(
                    channel_hash_hex, msg_id
                ):
                    accepted_ts.append(msg_ts)
                    self._apply_synced_reactions(
                        channel_hash_hex, m, responder_hex
                    )

                if inserted:
                    inserted_count += 1
                    self._storage.touch_channel(channel_hash_hex)
                    self._messaging.notify_message_received(
                        channel_hash_hex, msg_id
                    )
                    if self._reaction_mgr is not None:
                        self._reaction_mgr.request_missing_from_content(
                            responder_hex, m.get("content", "")
                        )
            except Exception as e:
                RNS.log(f"TrenchChat: sync_response insert error: {e}", RNS.LOG_WARNING)
                try:
                    msg_ts = float(m.get("timestamp"))
                except (TypeError, ValueError):
                    continue
                failed_ts = msg_ts if failed_ts is None else min(failed_ts, msg_ts)

        if inserted_count:
            # Clear hints now that we have the messages
            self._storage.clear_missed_deliveries(channel_hash_hex, self._identity.hash_hex)
            self._status.clear_gap(channel_hash_hex)

        # Advance to the newest message we actually accepted, not wall-clock
        # time -- a response capped at MAX_RESPONSE_MESSAGES otherwise strands
        # everything past the cap forever, since the next request would start
        # from "now" instead of resuming right after this batch.  Never past a
        # message we didn't get: the responder sweeps past what it withholds
        # (see _collect_permitted_rows), so a watermark that ran ahead of the
        # transcript could only mean history lost for good.
        # Never backwards, either: a hint can serve a message older than
        # everything we already hold, and rewinding the watermark over it would
        # re-request history we have on every future sync.
        # And never past a message whose insert failed -- the rows after it
        # landed, but resuming beyond the gap would hide it from every future
        # sweep, since get_messages_after filters on a strict timestamp >.
        usable_ts = ([t for t in accepted_ts if t < failed_ts]
                     if failed_ts is not None else accepted_ts)
        newest_ts = max(usable_ts) if usable_ts else 0.0

        if newest_ts > self._storage.get_last_sync(channel_hash_hex):
            self._storage.update_last_sync(channel_hash_hex, newest_ts)
        if newest_ts > 0:
            self._storage.advance_peer_sync_progress(channel_hash_hex, peer_hex, newest_ts)

        truncated = bool(fields.get(F_SYNC_TRUNCATED, False))
        self._status.response_received(
            channel_hash_hex, peer_hex,
            received=len(messages), inserted=inserted_count, truncated=truncated,
            rejected=rejected_count,
        )

        # A truncated response whose scan ran entirely through rows withheld
        # from us never advances newest_ts, since nothing was accepted -- but
        # the responder still made forward progress worth resuming from.
        # F_SYNC_SCAN_CURSOR carries that progress for the next request only;
        # it never feeds the persisted watermark above, so a withheld run
        # is re-scanned (bounded, indexed) rather than ever being skipped.
        resume_ts = newest_ts
        scan_cursor_raw = fields.get(F_SYNC_SCAN_CURSOR)
        if scan_cursor_raw is not None:
            try:
                scan_cursor = float(scan_cursor_raw)
            except (TypeError, ValueError):
                scan_cursor = None
            if scan_cursor is not None and scan_cursor > resume_ts:
                resume_ts = scan_cursor

        # Answer whatever the responder said it was missing. It recorded a
        # request of its own before asking, so this response is claimable.
        peer_ranges, peer_needs, malformed = self._read_reconcile_fields(fields)
        if malformed:
            RNS.log(
                f"TrenchChat [sync]: ignoring malformed ranges in a response "
                f"from {peer_hex[:12]}… on {channel_hash_hex[:12]}…",
                RNS.LOG_WARNING,
            )
            peer_ranges, peer_needs = None, None
        if peer_needs:
            self._answer_needs(channel_hash_hex, peer_hex, peer_needs)

        # Narrow anything the responder described that still differs from what
        # we hold -- which now includes everything this response just added.
        # Only chain a follow-up when there is something new to ask, or
        # somewhere new to resume from; a responder that repeats itself can't
        # induce a loop.
        cont_ranges, cont_needs, deferred = self._reconcile_from_response(
            channel_hash_hex, peer_ranges or []
        )
        # More remains when an answer was capped, or when either side left a
        # difference undescribed for the budget. That is remembered per peer
        # rather than acted on at once, because the narrowing this answer
        # also asked for has to run its course first; once it has, asking
        # again from what we now hold is a different question (a reconciled
        # request describes holdings, not a resume point). It is asked when
        # an answer has moved something: a row we did not hold, a difference
        # of our own, something the responder described, or a resume point
        # that advanced. An answer that moved nothing leaves it owed for the
        # next one that does (two peers reconciling each other at once fill
        # ranges out from under a step in flight, so a step can honestly
        # bring nothing while the wider difference remains), and a responder
        # repeating itself with nothing new never earns another request.
        key = (channel_hash_hex, peer_hex)
        if truncated or deferred:
            with self._deferred_lock:
                self._deferred.add(key)
        if not self._continue_reconcile(channel_hash_hex, peer_hex,
                                        cont_ranges, cont_needs):
            progress = (inserted_count or deferred or peer_ranges or peer_needs
                        or resume_ts > requested_since)
            with self._deferred_lock:
                owed = key in self._deferred and bool(progress)
                if owed:
                    self._deferred.discard(key)
            if owed:
                self._continue_sync(channel_hash_hex, peer_hex, resume_ts)

    # --- helpers ---

    def _reconcile_from_response(self, channel_hash_hex: str,
                                 ranges: list) -> tuple[list, list, bool]:
        """What to ask a responder next, given how it described its own rows.

        An id list resolves outright: what it names and we lack becomes a
        need. What we hold and it does not is not pushed at it -- nothing is
        ever sent unasked -- so we describe that range back instead, and it
        asks. A fingerprint says only that the range differs, so we describe
        our side of it and let the next round narrow it.

        Also returns whether anything that differed was left out for the
        budget, so the caller knows to ask again rather than treat the
        responder's next answer as the end of the exchange.
        """
        cont_ranges: list = []
        cont_needs: list = []
        deferred = False

        for lo, hi, mode, payload in ranges:
            index = self._storage.get_message_index(channel_hash_hex, lo, hi)
            signed = [r for r in index if _row_is_signed(r)]
            if mode == RANGE_IDLIST:
                for prefix in sync_ranges.prefixes_we_lack(index, payload):
                    if len(cont_needs) < MAX_NEED_IDS:
                        cont_needs.append([lo, hi, prefix])
                    else:
                        deferred = True
                if not sync_ranges.ids_they_lack(signed, payload):
                    continue
            elif sync_ranges.matches_fingerprint(signed, payload[0], payload[1]):
                continue
            if not sync_ranges.append_ranges(
                    cont_ranges, sync_ranges.describe(signed, lo, hi)):
                deferred = True
        return cont_ranges, cont_needs, deferred

    def _answer_needs(self, channel_hash_hex: str, peer_hex: str,
                      needs: list) -> None:
        """Send a peer the rows it named as missing, as a response to its ask."""
        if not self._peer_may_participate(channel_hash_hex, peer_hex):
            RNS.log(
                f"TrenchChat [sync]: refusing to answer {peer_hex[:12]}…'s "
                f"needs on {channel_hash_hex[:12]}… — not a participant",
                RNS.LOG_DEBUG,
            )
            return
        channel = self._storage.get_channel(channel_hash_hex)
        rows = self._rows_for_needs(channel, channel_hash_hex, peer_hex, needs)
        rows, capped = _truncate_at_group_boundary(rows)

        response_fields = {
            F_MSG_TYPE:       MT_SYNC_RESPONSE,
            F_CHANNEL_HASH:   bytes.fromhex(channel_hash_hex),
            F_SYNC_MESSAGES:  msgpack.packb(
                [self._row_to_payload(r) for r in rows], use_bin_type=True),
            F_SYNC_TRUNCATED: capped,
        }
        author_keys = self._author_keys_for(rows)
        if author_keys:
            response_fields[F_AUTHOR_KEYS] = author_keys
        sent = self._send_raw(peer_hex, response_fields)
        RNS.log(
            f"TrenchChat [sync]: {'answered' if sent else 'could not answer'} "
            f"{peer_hex[:12]}…'s needs on {channel_hash_hex[:12]}… — "
            f"{len(rows)} row(s)",
            RNS.LOG_DEBUG,
        )

    def _get_channel_peers(self, channel_hash_hex: str) -> set[str]:
        """Return identity hashes of all known peers on this channel (excl. self)."""
        peers: set[str] = set()

        # Public channels: subscribers tracked by SubscriptionManager
        subs = self._subscription_mgr.get_subscribers(channel_hash_hex)
        peers.update(subs)

        # Invite-only channels: from members table
        for row in self._storage.get_members(channel_hash_hex):
            peers.add(row["identity_hash"])

        # Channel owner (stored in channels table)
        channel = self._storage.get_channel(channel_hash_hex)
        if channel:
            peers.add(channel["creator_hash"])

        peers.discard(self._identity.hash_hex)
        return peers

    def _get_messages_by_ids(self, channel_hash_hex: str,
                              message_ids: list[str]) -> list:
        """Fetch message rows matching the given message_id list."""
        return self._storage.get_messages_by_ids(
            channel_hash_hex, message_ids[:MAX_RESPONSE_MESSAGES]
        )

    def _apply_synced_reactions(self, channel_hash_hex: str, m: dict,
                                responder_hex: str) -> None:
        """Store the reactions that rode along with a synced message.

        The body is bound to its author by a signature; these are not, and
        every field is the responder's to choose. So each is authorised the
        same way a directly delivered reaction is -- the reactor must be
        someone who could have sent it -- and an unresolvable custom emoji is
        ignored rather than fetched, since the reactor a fetch would be aimed
        at is exactly what the responder picked.
        """
        reactions = m.get("reactions")
        if not isinstance(reactions, list):
            return
        message_id = message_id_from_wire(m.get("message_id"))
        if not message_id:
            return

        for r in reactions[:MAX_REACTIONS_PER_MESSAGE]:
            if not isinstance(r, dict):
                continue
            emoji_key = _coerce_str(r.get("emoji", ""))
            reactor = _coerce_str(r.get("reactor", ""))
            if not emoji_key or not reactor:
                continue
            try:
                reacted_at = float(r.get("at", 0.0))
            except (TypeError, ValueError):
                continue

            if not _is_identity_hex(reactor):
                continue
            if self._reaction_mgr is not None and \
                    not self._reaction_mgr.may_react(channel_hash_hex, reactor):
                RNS.log(
                    f"TrenchChat [sync]: dropping synced reaction attributed to "
                    f"{reactor[:12]}… on {channel_hash_hex[:12]}… — not permitted",
                    RNS.LOG_WARNING,
                )
                continue

            self._storage.insert_reaction(
                message_id=message_id,
                emoji_hash=emoji_key,
                reactor_hash=reactor,
                channel_hash=channel_hash_hex,
                reacted_at=reacted_at,
            )
            if self._reaction_mgr is not None and \
                    is_custom_emoji_hash(emoji_key) and \
                    not self._storage.emoji_exists(emoji_key):
                self._reaction_mgr.request_emoji(responder_hex, emoji_key)

    def _author_keys_for(self, rows: list) -> dict:
        """Public keys for the authors of a batch, one entry per author.

        Sent with the batch because the requester usually cannot obtain them
        any other way: resolving an author needs an announce, and an author who
        has left the network will never send one again. Everything they wrote
        would then be unverifiable -- and so silently dropped -- for every peer
        who arrives after they go.

        One map per response rather than a key per row: a batch is usually a
        handful of authors and up to fifty messages, and on a 1 kbps radio the
        difference is the whole response over again.
        """
        keys: dict[str, bytes] = {}
        for row in rows:
            author = row["sender_hash"]
            if not author or author in keys:
                continue
            key = public_key_for(self._storage, author)
            if key:
                keys[author] = key
        return keys

    def _learn_author_keys(self, keys) -> None:
        """Cache the keys a responder sent with its batch, checking each one.

        Bounded because this map is the one bulk container carried as a bare
        LXMF dict rather than as bytes through unpack_wire, so nothing else
        caps it. Every pair is self-certifying, but an identity hash is
        derived *from* its key -- so a valid pair costs a hash, not a keypair,
        and an unbounded map is a free way to grow identity_keys for good.
        _author_keys_for never emits more than one key per row in a batch.
        """
        if not isinstance(keys, dict):
            return
        if len(keys) > MAX_AUTHOR_KEYS:
            RNS.log(
                f"TrenchChat [sync]: refusing an author-key map of {len(keys)} "
                f"entries (max {MAX_AUTHOR_KEYS})",
                RNS.LOG_WARNING,
            )
            return
        for author, key in keys.items():
            author_hex = _coerce_str(author)
            if not _is_identity_hex(author_hex):
                continue
            remember_relayed_key(self._storage, author_hex, key)

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = {
            "sender_hash":  row["sender_hash"],
            "sender_name":  row["sender_name"],
            "content":      row["content"],
            "timestamp":    row["timestamp"],
            "message_id":   message_id_to_wire(row["message_id"]),
            "reply_to":     message_id_to_wire(row["reply_to"]),
            "last_seen_id": message_id_to_wire(row["last_seen_id"]),
        }
        image_data = row["image_data"] if "image_data" in row.keys() else None
        if image_data:
            d["image_data"] = bytes(image_data)
        author_sig = row["author_sig"] if "author_sig" in row.keys() else None
        if author_sig:
            d["author_sig"] = bytes(author_sig)
        return d

    def _row_to_payload(self, row) -> dict:
        """_row_to_dict plus the reactions on that message, for a sync response."""
        d = self._row_to_dict(row)
        reactions = [
            {"emoji": r["emoji_hash"], "reactor": r["reactor_hash"],
             "at": r["reacted_at"]}
            for r in self._storage.get_reactions(row["message_id"])
        ][:MAX_REACTIONS_PER_MESSAGE]
        if reactions:
            d["reactions"] = reactions
        return d

    def _reconcile_window(self, since_ts: float) -> tuple[float, float]:
        """The span a request reconciles, given where the caller wants to start.

        The recent window normally, reaching further back only when the caller
        asks from before it (a fresh join, or an entitlement change, both of
        which pass 0.0). The upper bound allows for a peer whose clock runs
        ahead of ours.
        """
        now = time.time()
        floor = max(now - SYNC_WINDOW_SECS, 0.0)
        return min(max(since_ts, 0.0), floor), now + SYNC_CLOCK_SKEW_SECS

    def _describe_local(self, channel_hash_hex: str, lo: float, hi: float,
                        ladder: bool = False) -> list:
        """How we describe our own rows in [lo, hi) to a peer.

        A blind re-check, where nothing yet says the peer differs, is one
        fingerprint over the window. Once a difference is known (a beacon
        probe disagreed, or an answer said more remains) it is the *ladder*:
        the newest few rows by id, the rest fingerprinted in buckets that grow
        with age, so the answer can carry rows at once. A narrow description
        of a single range is built where the answer is read
        (_reconcile_from_response), never here.

        Only signed rows: an unsigned one cannot be relayed to anybody, so
        claiming it here would have peers withhold their own verifiable copy.
        """
        index = [r for r in self._storage.get_message_index(channel_hash_hex, lo, hi)
                 if _row_is_signed(r)]
        return sync_ranges.summarise(index, lo, hi, ladder=ladder)

    def _record_asked(self, channel_hash_hex: str, dest_hex: str,
                      ranges: list | None, needs: list | None) -> None:
        with self._last_asked_lock:
            self._last_asked[(channel_hash_hex, dest_hex)] = \
                sync_ranges.signature(ranges, needs)

    def _send_sync_request(self, dest_hex: str, channel_hash_hex: str, since_ts: float,
                           continuation: bool = False, ranges: list | None = None,
                           needs: list | None = None, ladder: bool = False) -> bool:
        """Ask a peer for anything on this channel we do not already hold.

        The request carries a description of our own rows, so the answer is
        the difference rather than everything past a timestamp: a summary of
        the window unless the caller passes *ranges* and *needs* it has
        already worked out. F_SYNC_WINDOW_START is still sent exactly as
        before, so a responder that predates ranges behaves as it always did.
        """
        if ranges is None and needs is None:
            lo, hi = self._reconcile_window(since_ts)
            ranges = self._describe_local(channel_hash_hex, lo, hi, ladder=ladder)

        if not continuation:
            with self._continuations_lock:
                self._continuations.pop((channel_hash_hex, dest_hex), None)

        with self._sync_policy_lock:
            self._sync_policy[channel_hash_hex] = self._sync_policy_for(channel_hash_hex)
        req_id = self._record_pending_request(channel_hash_hex, dest_hex, since_ts)
        request_fields = {
            F_MSG_TYPE:          MT_SYNC_REQUEST,
            F_CHANNEL_HASH:      bytes.fromhex(channel_hash_hex),
            F_SYNC_WINDOW_START: since_ts,
        }
        if ranges:
            request_fields[F_SYNC_RANGES] = sync_ranges.pack(ranges)
        if needs:
            request_fields[F_SYNC_NEED] = sync_ranges.pack(needs)
        if continuation:
            request_fields[F_SYNC_CONTINUES] = True
        else:
            with self._deferred_lock:
                self._deferred.discard((channel_hash_hex, dest_hex))
        sent = self._send_raw(dest_hex, request_fields)
        if sent:
            self._record_asked(channel_hash_hex, dest_hex, ranges, needs)
            # Asking from 0 is a request for the whole transcript, not a
            # re-check, even before we hold anything to compare against.
            # Compared against this specific peer's own progress, not the
            # channel-wide watermark -- a peer legitimately behind on a
            # multi-peer channel would otherwise look like a gap-filling
            # request on every routine re-check.
            reaching_back = (since_ts <= 0.0
                             or since_ts < self._storage.get_peer_sync_progress(
                                 channel_hash_hex, dest_hex))
            self._status.request_sent(
                channel_hash_hex, dest_hex, continuation=continuation,
                reaching_back=reaching_back,
            )
        else:
            # Nothing went out, so nothing can answer it.  Dropping the entry
            # also keeps a response from a peer we never reached from being
            # treated as solicited.
            self._drop_pending_request(channel_hash_hex, dest_hex, req_id)
            self._status.request_unreachable(channel_hash_hex, dest_hex)
        return sent

    def _continue_sync(self, channel_hash_hex: str, peer_hex: str, resume_ts: float,
                       ranges: list | None = None, needs: list | None = None) -> bool:
        """Ask the same peer again, for the next batch or a narrowed range.

        Without *ranges* or *needs* the next batch is asked for with a fresh
        ladder of what we hold now, which the rows just received have
        changed. MAX_SYNC_CONTINUATIONS bounds a responder that always reports
        more remaining.
        """
        key = (channel_hash_hex, peer_hex)
        with self._continuations_lock:
            count = self._continuations.get(key, 0)
            if count >= MAX_SYNC_CONTINUATIONS:
                RNS.log(
                    f"TrenchChat [sync]: continuation budget exhausted for "
                    f"{channel_hash_hex[:12]}… from {peer_hex[:12]}…",
                    RNS.LOG_WARNING,
                )
                return False
            self._continuations[key] = count + 1

        return self._send_sync_request(peer_hex, channel_hash_hex, resume_ts,
                                       continuation=True, ranges=ranges, needs=needs,
                                       ladder=True)

    def _continue_reconcile(self, channel_hash_hex: str, peer_hex: str,
                            ranges: list, needs: list) -> bool:
        """Ask a peer again, narrowed to whatever still differs.

        False when there is nothing new to ask. A question we have already put
        to this peer can only get the answer it already gave, so repeating it
        is how a responder whose description never changes would otherwise
        drive requests until the budget ran out.
        """
        if not ranges and not needs:
            return False
        key = (channel_hash_hex, peer_hex)
        asked = sync_ranges.signature(ranges, needs)
        with self._last_asked_lock:
            if self._last_asked.get(key) == asked:
                return False
        since_ts = min([r[0] for r in ranges] + [n[0] for n in needs])
        return self._continue_sync(channel_hash_hex, peer_hex, since_ts,
                                   ranges=ranges, needs=needs)

    # --- outstanding sync request tracking ---

    def _peer_key_forms(self, peer_hex: str) -> list[str]:
        """Return every hex form an inbound message may identify this peer by.

        Handlers resolve the sender via RNS.Identity.recall() but fall back to
        the raw source_hash, which is the delivery destination hash; these are
        different values for the same peer.
        """
        forms = [peer_hex]
        try:
            delivery = RNS.Destination.hash(bytes.fromhex(peer_hex), "lxmf", "delivery")
            forms.append(delivery.hex())
        except (ValueError, TypeError):
            pass
        return forms

    def tick(self) -> None:
        """Re-ask peers whose answer never came, act on probes the cooldown
        held back, and ask about gaps that messages have revealed.

        Every other trigger is an event: a peer announcing, or this node's own
        link returning. Both fire in a burst and then stop -- Reticulum
        suppresses announce replays for a destination it has already
        propagated -- so a request refused during that burst is never made
        again. A responder's deep-sync cooldown refuses silently and lasts a
        minute, which is long enough to swallow an entire burst, leaving a
        returning peer waiting forever on an answer nobody is going to send
        (sync2 in docs/testenv-scenarios.md).
        """
        self._flush_probe_pending()
        self._ask_suspected_gaps()

        now = time.time()
        stale: dict[str, set[str]] = {}
        with self._pending_requests_lock:
            for (channel_hash_hex, _form), entries in self._pending_requests.items():
                for asked_at, _since, dest_hex, _req_id in entries:
                    tries = self._retry_counts.get((channel_hash_hex, dest_hex), 0)
                    due = SYNC_RETRY_SECS * (SYNC_RETRY_BACKOFF ** tries)
                    if now - asked_at >= due:
                        stale.setdefault(channel_hash_hex, set()).add(dest_hex)

        for channel_hash_hex, peers in stale.items():
            if not self._storage.is_subscribed(channel_hash_hex):
                continue
            for peer_hex in peers:
                self._expire_pending_requests(channel_hash_hex, peer_hex, now)
                key = (channel_hash_hex, peer_hex)
                tries = self._retry_counts.get(key, 0)
                if tries >= MAX_SYNC_RETRIES:
                    RNS.log(
                        f"TrenchChat [sync]: giving up re-asking {peer_hex[:12]}… "
                        f"for {channel_hash_hex[:12]}… after {tries} tries",
                        RNS.LOG_DEBUG,
                    )
                    continue
                self._retry_counts[key] = tries + 1
                since_ts = self._storage.get_peer_sync_progress(channel_hash_hex, peer_hex)
                RNS.log(
                    f"TrenchChat [sync]: re-asking {peer_hex[:12]}… for "
                    f"{channel_hash_hex[:12]}… — no answer to the last request "
                    f"(try {tries + 1})",
                    RNS.LOG_DEBUG,
                )
                self._send_sync_request(peer_hex, channel_hash_hex, since_ts)

    def _expire_pending_requests(self, channel_hash_hex: str, dest_hex: str,
                                 now: float) -> None:
        """Drop this peer's timed-out entries, so a retry is not itself stale."""
        with self._pending_requests_lock:
            for form in self._peer_key_forms(dest_hex):
                entries = self._pending_requests.get((channel_hash_hex, form))
                if not entries:
                    continue
                entries[:] = [e for e in entries if now - e[0] < SYNC_RETRY_SECS]
                if not entries:
                    del self._pending_requests[(channel_hash_hex, form)]

    def _record_pending_request(self, channel_hash_hex: str, dest_hex: str,
                                since_ts: float = 0.0) -> int:
        """Remember that we asked dest_hex for history on this channel.

        A peer may have more than one request outstanding at once, so this
        appends to a per-(channel, peer-key-form) queue rather than replacing
        a single slot -- otherwise a second trigger racing an earlier one
        (e.g. startup sync and an announce-driven request) would overwrite
        the first request's entry before either could be claimed. Returns a
        request id so a failed send can retract exactly this entry.
        """
        with self._pending_requests_lock:
            self._pending_request_seq += 1
            req_id = self._pending_request_seq
            entry = (time.time(), since_ts, dest_hex, req_id)
            for form in self._peer_key_forms(dest_hex):
                self._pending_requests.setdefault((channel_hash_hex, form), []).append(entry)
        return req_id

    def _drop_pending_request(self, channel_hash_hex: str, dest_hex: str,
                              req_id: int | None = None):
        """Forget a request that was never actually sent.

        Drops exactly req_id when given; otherwise drops the most recently
        recorded entry for dest_hex on this channel.
        """
        with self._pending_requests_lock:
            for form in self._peer_key_forms(dest_hex):
                key = (channel_hash_hex, form)
                entries = self._pending_requests.get(key)
                if not entries:
                    continue
                if req_id is not None:
                    entries[:] = [e for e in entries if e[3] != req_id]
                else:
                    entries.pop()
                if not entries:
                    del self._pending_requests[key]

    def _clear_retry_budget(self, channel_hash_hex: str, peer_hex: str) -> None:
        """An answer means the peer is talking to us; start the count over."""
        self._retry_counts.pop((channel_hash_hex, peer_hex), None)

    def _claim_pending_request(self, channel_hash_hex: str,
                               responder_hex: str) -> tuple[float, str] | None:
        """Consume the oldest outstanding request this response could answer.

        Returns the window start we asked for and the identity hex we addressed
        the request to, or None if nothing was outstanding.  A response may
        identify its sender by either the identity or the delivery destination
        hash, so the recorded form is what the rest of the exchange keys on.
        Consuming the entry makes a single request answerable only once; a
        second, independently outstanding request to the same peer remains
        queued for a later response to claim.
        """
        now = time.time()
        with self._pending_requests_lock:
            for stale_key, entries in list(self._pending_requests.items()):
                fresh = [e for e in entries if now - e[0] <= SYNC_RESPONSE_WINDOW_SECS]
                if fresh:
                    self._pending_requests[stale_key] = fresh
                else:
                    del self._pending_requests[stale_key]

            claimed = None
            claimed_req_id = None
            for form in self._peer_key_forms(responder_hex):
                key = (channel_hash_hex, form)
                entries = self._pending_requests.get(key)
                if entries:
                    _issued, since, peer, claimed_req_id = entries.pop(0)
                    claimed = (since, peer)
                    if not entries:
                        del self._pending_requests[key]
                    break
            if claimed is not None:
                # The same request was recorded under the peer's other key
                # form too; remove that twin copy so it can't be claimed again.
                for form in self._peer_key_forms(claimed[1]):
                    key = (channel_hash_hex, form)
                    entries = self._pending_requests.get(key)
                    if not entries:
                        continue
                    entries[:] = [e for e in entries if e[3] != claimed_req_id]
                    if not entries:
                        del self._pending_requests[key]
            return claimed

    def _deep_sync_allowed(self, channel_hash_hex: str, requester_hex: str,
                           continues: bool = False) -> bool:
        """Rate-limit deep (pre-SYNC_WINDOW_SECS) backfill per (channel, peer).

        A fresh deep ask is served once per DEEP_SYNC_COOLDOWN_SECS: asking
        it again can only produce the answer already given, and a flood of
        them is what this exists to pace. A request flagged as continuing
        (F_SYNC_CONTINUES) is a step of an exchange this peer already opened,
        where a refusal leaves it stranded halfway through with nothing left
        to ask, so it is served while that window lasts and until
        DEEP_SYNC_BURST messages have gone into it. The flag is the
        requester's claim, so it buys nothing past that burst.

        A permissions change lifts the cooldown early: the refusal it would
        otherwise enforce was decided under a policy that no longer applies,
        and the requester would be made to wait it out for history they are
        now entitled to.
        """
        now = time.time()
        key = (channel_hash_hex, requester_hex)
        channel = self._storage.get_channel(channel_hash_hex)
        policy = channel["permissions"] if channel else ""
        with self._deep_sync_lock:
            for stale_key, entry in list(self._deep_sync_last_served.items()):
                if now - entry[0] > DEEP_SYNC_COOLDOWN_PRUNE_SECS:
                    del self._deep_sync_last_served[stale_key]
            opened = self._deep_sync_last_served.get(key)
            live = (opened is not None and now - opened[0] < DEEP_SYNC_COOLDOWN_SECS
                    and opened[1] == policy)
            if live:
                if not continues or opened[2] >= DEEP_SYNC_BURST:
                    return False
                self._deep_sync_last_served[key] = (opened[0], policy, opened[2] + 1)
                return True
            self._deep_sync_last_served[key] = (now, policy, 1)
            return True

    def _announce_sync_due(self, channel_hash_hex: str, peer_hex: str) -> bool:
        """True unless an announce-driven request for this (channel, peer)
        was sent within ANNOUNCE_SYNC_COOLDOWN_SECS."""
        now = time.time()
        with self._announce_sync_lock:
            last = self._announce_sync_times.get((channel_hash_hex, peer_hex))
            return last is None or now - last >= ANNOUNCE_SYNC_COOLDOWN_SECS

    def _record_announce_sync(self, channel_hash_hex: str, peer_hex: str) -> None:
        """Start the announce cooldown for this (channel, peer), pruning idle
        entries so the map stays bounded."""
        now = time.time()
        with self._announce_sync_lock:
            for stale_key, ts in list(self._announce_sync_times.items()):
                if now - ts > ANNOUNCE_SYNC_PRUNE_SECS:
                    del self._announce_sync_times[stale_key]
            self._announce_sync_times[(channel_hash_hex, peer_hex)] = now

    # Message types the generic retry queue must not hold. A sync request is
    # re-issued by on_peer_appeared and by tick(), and only through
    # _send_sync_request, which is what records a claimable pending entry --
    # a queued copy arrives with none, so its answer is dropped as unsolicited
    # while still consuming the responder's deep-sync budget for that pair.
    # Hints have their own _pending_hints queue, and holding them in both sent
    # each one twice to every reachable subscriber.
    _RETRY_EXEMPT_TYPES = (MT_SYNC_REQUEST, MT_MISSED_DELIVERY)

    def _send_raw(self, dest_hex: str, fields: dict) -> bool:
        """Send a control message to a peer. Returns False if it couldn't go out."""
        try:
            identity_hash = bytes.fromhex(dest_hex)
            delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
            dest_identity = RNS.Identity.recall(delivery_dest_hash)
            if dest_identity is None:
                # A peer that just came back asks everyone for what it missed,
                # and the answer dies here: the responder can read the request
                # but cannot yet address a reply. The requester sees only
                # silence, which it cannot tell from a refusal, and nothing
                # ever re-sends the answer (sync2 in docs/testenv-scenarios.md).
                RNS.Transport.request_path(delivery_dest_hash)
                if fields.get(F_MSG_TYPE) not in self._RETRY_EXEMPT_TYPES:
                    self._retry.queue(dest_hex, fields)
                return False
            dest = RNS.Destination(
                dest_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "lxmf",
                "delivery",
            )
            lxm = LXMF.LXMessage(
                dest,
                self._router.delivery_destination,
                "",
                desired_method=LXMF.LXMessage.DIRECT,
            )
            lxm.fields = pack_fields(fields)
            self._router.send(lxm)
            return True
        except Exception as e:
            RNS.log(f"TrenchChat: sync send error to {dest_hex}: {e}", RNS.LOG_WARNING)
            return False
