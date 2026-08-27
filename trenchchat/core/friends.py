"""
Saved contacts, and the handshake that makes a contact mutual.

A friend is an identity hash the user has chosen to remember, with an optional
nickname and free-text note. The nickname and note stay local; the friendship
itself does not. `state` is one of:

    accepted    -- we trust this identity. Direct messages from them are
                   accepted; from anyone else they are dropped.
    pending_out -- we asked them and have not heard back.
    pending_in  -- they asked us and the user has not decided.

Only `accepted` counts: is_friend() is the single gate direct messaging reads,
on both the outbound and the inbound side. Because each side enforces it
independently, a DM only flows when both peers hold the other as accepted --
one-sided trust delivers nothing.

add_friend() stays what it always was: an immediate local add, for a hash the
user obtained out of band. send_friend_request() is the wire path, and reaches
the same state with the peer's agreement rather than without it.

Last-seen tracking rides on PresenceManager.add_seen_callback, which fires on
every announce or inbound message, not just online/offline transitions -- see
presence.py.

The nickname stored here is friends-panel-only. It never feeds
presence.resolve_display_name(), which stays the peer's self-asserted name.
"""

import threading
import time

import RNS
import LXMF

from trenchchat.core.control_retry import ControlRetryQueue
from trenchchat.core.presence import resolve_display_name
from trenchchat.core.protocol import (
    F_DISPLAY_NAME, F_FRIEND_NOTE, F_MSG_TYPE, MAX_FRIEND_NOTE_CHARS,
    MT_FRIEND_ACCEPT, MT_FRIEND_DECLINE, MT_FRIEND_REQUEST,
)
from trenchchat.core.storage import (
    FRIEND_ACCEPTED, FRIEND_PENDING_IN, FRIEND_PENDING_OUT,
)

IDENTITY_HASH_HEX_LEN = 32

# Durable last-seen writes are throttled to this often per friend -- announces
# are frequent and a DB write per packet would be wasteful.
FRIEND_SEEN_WRITE_INTERVAL_SECS = 60

# Unanswered inbound requests held at once. Identities are free to mint, so a
# queue the user never empties must cost a fixed amount of storage. The
# router's per-sender control throttle bounds one peer; this bounds the rest.
MAX_PENDING_FRIEND_REQUESTS = 64

# A message from someone not yet accepted is held rather than dropped, so a
# client with no friend-request concept can still ask to be heard. Everything
# below bounds that, and all of it is load-bearing: a direct message carries no
# F_MSG_TYPE, which deliberately keeps it out of the router's per-sender
# control throttle, so this path has no rate limit of its own.
#
# Matched to the friend-request note so peer-written text is capped identically
# wherever it is shown.
MAX_REQUEST_BODY_CHARS = MAX_FRIEND_NOTE_CHARS
# A stranger gets to say a little, not to fill a screen. Oldest-first.
MAX_HELD_PER_SENDER = 3
MAX_HELD_MESSAGES = 128
MESSAGE_REQUEST_TTL_SECS = 30 * 24 * 3600


class FriendsManager:
    """Saved contacts, the friend-request handshake, and durable last-seen."""

    def __init__(self, storage, self_hex: str, presence_mgr=None, *,
                 identity=None, router=None) -> None:
        """*identity* and *router* enable the wire handshake.

        Without them the manager is local-only and every send is a no-op, which
        is what a headless or storage-only construction gets.
        """
        self._storage = storage
        self._self_hex = self_hex
        self._presence_mgr = presence_mgr
        self._identity = identity
        self._router = router
        self._lock = threading.Lock()
        self._friend_hashes: set[str] = storage.get_friend_hashes()
        self._last_write: dict[str, float] = {}
        # Unthrottled, so the offline flush records the true last sighting.
        self._last_seen: dict[str, float] = {}
        self._callbacks: list = []
        self._request_callbacks: list = []
        self._retry = ControlRetryQueue("friends")
        # Set by the frontend wiring; see set_message_filer.
        self._message_filer = None

        if router is not None:
            router.add_delivery_callback(self._on_lxmf_message)

    # --- public API: local contacts ---

    def add_friend(self, identity_hash_hex: str, nickname: str = "",
                   note: str = "") -> bool:
        """Add or update a friend directly, without asking them.

        For a hash the user obtained out of band. The peer is not told, and
        must add us for anything to flow between us. Accepting an inbound
        request routes through here too, and answers it.
        """
        if not self._is_valid_hash(identity_hash_hex) or identity_hash_hex == self._self_hex:
            return False
        was_pending_in = (
            self._storage.get_friend_state(identity_hash_hex) == FRIEND_PENDING_IN
        )
        self._storage.upsert_friend(identity_hash_hex, nickname, note, FRIEND_ACCEPTED)
        with self._lock:
            self._friend_hashes.add(identity_hash_hex)
        if was_pending_in and self._asked_us(identity_hash_hex):
            self._send(identity_hash_hex, MT_FRIEND_ACCEPT)
        self._file_held_messages(identity_hash_hex)
        self._fire_callbacks(identity_hash_hex)
        return True

    def update_friend(self, identity_hash_hex: str, *, nickname: str | None = None,
                      note: str | None = None) -> bool:
        """Apply only the given fields; None leaves that field unchanged.

        Returns False if the friend doesn't exist.
        """
        existing = self._storage.get_friend(identity_hash_hex)
        if existing is None:
            return False
        new_nickname = existing["nickname"] if nickname is None else nickname
        new_note = existing["note"] if note is None else note
        self._storage.upsert_friend(identity_hash_hex, new_nickname, new_note,
                                    existing.get("state", FRIEND_ACCEPTED))
        self._fire_callbacks(identity_hash_hex)
        return True

    def remove_friend(self, identity_hash_hex: str) -> bool:
        """Forget a contact. Returns False if the friend doesn't exist.

        Local only: the peer is never told. They find out when their messages
        stop being accepted, which tells them less than a notice would.
        """
        if self._storage.get_friend(identity_hash_hex) is None:
            return False
        self._storage.delete_friend(identity_hash_hex)
        self._storage.clear_message_requests(identity_hash_hex)
        with self._lock:
            self._friend_hashes.discard(identity_hash_hex)
            self._last_write.pop(identity_hash_hex, None)
            self._last_seen.pop(identity_hash_hex, None)
        self._fire_callbacks(identity_hash_hex)
        return True

    def is_friend(self, identity_hash_hex: str) -> bool:
        """True only for an accepted friend. The direct-message gate."""
        with self._lock:
            return identity_hash_hex in self._friend_hashes

    def get_friends(self) -> list[dict]:
        """One dict per accepted friend, joining the stored row with live presence.

        last_seen_at is the max of the stored (throttled) value and
        presence_mgr's live value, so a currently-online friend reads as
        live even before the throttled write lands.
        """
        return [self._present(row) for row in self._storage.get_friends()]

    # --- public API: handshake ---

    def send_friend_request(self, identity_hash_hex: str, note: str = "",
                            nickname: str = "") -> bool:
        """Ask a peer to add us. Returns False for a malformed hash or self.

        A request to someone already accepted re-sends nothing and changes
        nothing; a request to someone who already asked us accepts theirs.
        """
        if not self._is_valid_hash(identity_hash_hex) or identity_hash_hex == self._self_hex:
            return False

        state = self._storage.get_friend_state(identity_hash_hex)
        if state == FRIEND_ACCEPTED:
            return True
        if state == FRIEND_PENDING_IN:
            return self.accept_friend_request(identity_hash_hex, nickname=nickname)

        self._storage.upsert_friend(identity_hash_hex, nickname, note,
                                    FRIEND_PENDING_OUT)
        self._send(identity_hash_hex, MT_FRIEND_REQUEST, note=note)
        self._fire_callbacks(identity_hash_hex)
        return True

    def accept_friend_request(self, identity_hash_hex: str,
                              nickname: str = "") -> bool:
        """Accept a request we received. False if there is no request from them."""
        if self._storage.get_friend_state(identity_hash_hex) != FRIEND_PENDING_IN:
            return False
        existing = self._storage.get_friend(identity_hash_hex) or {}
        self._storage.upsert_friend(
            identity_hash_hex,
            nickname or existing.get("nickname", ""),
            existing.get("note", ""),
            FRIEND_ACCEPTED,
        )
        with self._lock:
            self._friend_hashes.add(identity_hash_hex)
        if self._asked_us(identity_hash_hex):
            self._send(identity_hash_hex, MT_FRIEND_ACCEPT)
        self._file_held_messages(identity_hash_hex)
        self._fire_callbacks(identity_hash_hex)
        return True

    def hold_message_request(self, identity_hash_hex: str, body: str,
                             from_trenchchat: bool = False) -> bool:
        """Hold words from someone we have not accepted, for the user to judge.

        A client that speaks only plain LXMF cannot send MT_FRIEND_REQUEST, so
        without this it has no way to reach a stranger at all -- its message was
        dropped and nobody was ever told. Holding it grants nothing: the sender
        stays unaccepted, and only the user accepting changes that.

        False when there is nothing to hold: our own hash, a malformed one, a
        sender already accepted (their message is a real one), or one we have an
        outstanding request to -- their answer belongs to that request, not to a
        second queue.
        """
        if not self._is_valid_hash(identity_hash_hex) \
                or identity_hash_hex == self._self_hex:
            return False
        state = self._storage.get_friend_state(identity_hash_hex)
        if state in (FRIEND_ACCEPTED, FRIEND_PENDING_OUT):
            return False

        self._storage.prune_message_requests(time.time() - MESSAGE_REQUEST_TTL_SECS)
        if state != FRIEND_PENDING_IN:
            self._evict_oldest_pending()
            self._storage.upsert_friend(identity_hash_hex, "", "", FRIEND_PENDING_IN)

        self._storage.add_message_request(
            identity_hash_hex, (body or "")[:MAX_REQUEST_BODY_CHARS],
            from_trenchchat,
            max_per_sender=MAX_HELD_PER_SENDER, max_total=MAX_HELD_MESSAGES,
        )
        RNS.log(
            f"TrenchChat [friends]: holding a message from {identity_hash_hex[:12]}… "
            f"— not an accepted friend, waiting on the user",
            RNS.LOG_NOTICE,
        )
        self._fire_callbacks(identity_hash_hex)
        return True

    def set_message_filer(self, filer) -> None:
        """Attach what files a newly accepted peer's held messages.

        A plain callable taking the peer's hash, not a manager: filing needs
        Messaging, and friends.py must not depend on it. Optional -- without
        one, held messages simply stay held.
        """
        self._message_filer = filer

    def _file_held_messages(self, identity_hash_hex: str) -> None:
        if self._message_filer is None:
            return
        try:
            self._message_filer(identity_hash_hex)
        except Exception as e:
            RNS.log(f"TrenchChat [friends]: could not file held messages for "
                    f"{identity_hash_hex[:12]}…: {e}", RNS.LOG_ERROR)

    def take_message_requests(self, identity_hash_hex: str) -> list[dict]:
        """Held messages from a peer, oldest first, cleared as they are handed over.

        Called once a peer is accepted, so the words that asked for that
        acceptance end up in the conversation they were always meant for.
        """
        held = self._storage.get_message_requests(identity_hash_hex)
        if held:
            self._storage.clear_message_requests(identity_hash_hex)
        return held

    def _asked_us(self, identity_hash_hex: str) -> bool:
        """Whether this peer sent a handshake rather than only words.

        A peer holding messages it sent us, none of them from TrenchChat, has no
        handshake to answer -- and an empty control message is all its client
        would show. A TrenchChat peer could not have messaged us at all without
        already accepting us, so an accept back is at worst redundant.
        """
        held = self._storage.get_message_requests(identity_hash_hex)
        return not held or any(h["from_trenchchat"] for h in held)

    def decline_friend_request(self, identity_hash_hex: str) -> bool:
        """Refuse a request we received. False if there is no request from them."""
        if self._storage.get_friend_state(identity_hash_hex) != FRIEND_PENDING_IN:
            return False
        held = self._storage.get_message_requests(identity_hash_hex)
        self._storage.delete_friend(identity_hash_hex)
        self._storage.clear_message_requests(identity_hash_hex)
        # A peer that only ever sent words has no handshake to decline, and
        # telling it we refused would be the one thing it hears from us.
        if not held or any(h["from_trenchchat"] for h in held):
            self._send(identity_hash_hex, MT_FRIEND_DECLINE)
        self._fire_callbacks(identity_hash_hex)
        return True

    def cancel_friend_request(self, identity_hash_hex: str) -> bool:
        """Withdraw a request we sent. Local only, like remove_friend."""
        if self._storage.get_friend_state(identity_hash_hex) != FRIEND_PENDING_OUT:
            return False
        self._storage.delete_friend(identity_hash_hex)
        self._fire_callbacks(identity_hash_hex)
        return True

    def get_pending_requests(self) -> dict[str, list[dict]]:
        """Requests waiting on someone: 'incoming' on us, 'outgoing' on them.

        An incoming entry also carries any words the peer sent while unaccepted
        -- that is how a client with no handshake asks, so it is the same queue.
        """
        return {
            "incoming": [self._with_held_message(self._present(r))
                         for r in self._storage.get_friends_in_state(FRIEND_PENDING_IN)],
            "outgoing": [self._present(r)
                         for r in self._storage.get_friends_in_state(FRIEND_PENDING_OUT)],
        }

    def _with_held_message(self, entry: dict) -> dict:
        """Add the most recent held message, its count, and who sent it.

        The body is peer-written text shown before the user has agreed to
        anything, capped on the way in at MAX_REQUEST_BODY_CHARS; a client
        renders it as text and never as anything else.
        """
        held = self._storage.get_message_requests(entry["identity_hash"])
        entry["message"] = held[-1]["body"] if held else None
        entry["message_count"] = len(held)
        entry["from_trenchchat"] = bool(held and held[-1]["from_trenchchat"])
        return entry

    def flush_pending(self, dest_hex: str) -> int:
        """Re-send handshake messages held while this peer had no known path."""
        return self._retry.flush(dest_hex, self._send_raw)

    # --- presence bookkeeping ---

    def record_seen(self, peer_hex: str) -> None:
        """PresenceManager seen-callback. A no-op set lookup for non-friends;
        for a friend, a durable write throttled to once per
        FRIEND_SEEN_WRITE_INTERVAL_SECS."""
        with self._lock:
            if peer_hex not in self._friend_hashes:
                return
            now = time.time()
            self._last_seen[peer_hex] = now
            if now - self._last_write.get(peer_hex, 0.0) < FRIEND_SEEN_WRITE_INTERVAL_SECS:
                return
            self._last_write[peer_hex] = now
        self._storage.touch_friend_seen(peer_hex, now)

    def record_presence(self, peer_hex: str, is_online: bool) -> None:
        """PresenceManager presence-callback. On the offline transition, flush
        the last sighting to storage.

        Presence discards its entry when a peer goes offline -- via prune() or
        a graceful-shutdown goodbye -- so without this the reported last_seen
        falls back to the throttled write and jumps backwards by up to
        FRIEND_SEEN_WRITE_INTERVAL_SECS.
        """
        if is_online:
            return
        with self._lock:
            if peer_hex not in self._friend_hashes:
                return
            seen = self._last_seen.get(peer_hex, 0.0)
            if seen <= 0.0 or seen <= self._last_write.get(peer_hex, 0.0):
                return
            self._last_write[peer_hex] = seen
        self._storage.touch_friend_seen(peer_hex, seen)

    # --- callbacks ---

    def add_friends_callback(self, cb) -> None:
        """Register a callback invoked with (identity_hash_hex: str) on any
        change to a contact -- add, update, remove, or a handshake transition.

        Never fired for throttled last-seen writes.
        """
        self._callbacks.append(cb)

    def add_request_callback(self, cb) -> None:
        """Register a callback invoked with (identity_hash_hex, display_name,
        note) when a peer asks to be added."""
        self._request_callbacks.append(cb)

    # --- inbound handshake ---

    def _on_lxmf_message(self, message: LXMF.LXMessage) -> None:
        fields = getattr(message, "fields", None) or {}
        msg_type = fields.get(F_MSG_TYPE)
        if isinstance(msg_type, bytes):
            msg_type = msg_type.decode(errors="replace")
        if msg_type not in (MT_FRIEND_REQUEST, MT_FRIEND_ACCEPT, MT_FRIEND_DECLINE):
            return

        sender_hex = self._sender_hex(message)
        if not self._is_valid_hash(sender_hex) or sender_hex == self._self_hex:
            return

        if msg_type == MT_FRIEND_REQUEST:
            self._handle_request(sender_hex, fields)
        elif msg_type == MT_FRIEND_ACCEPT:
            self._handle_accept(sender_hex)
        else:
            self._handle_decline(sender_hex)

    def _handle_request(self, sender_hex: str, fields: dict) -> None:
        state = self._storage.get_friend_state(sender_hex)

        if state == FRIEND_ACCEPTED:
            # They asked again -- most likely they lost their contacts. Answer
            # so their side reaches accepted too, with nothing shown to us.
            self._send(sender_hex, MT_FRIEND_ACCEPT)
            return

        if state == FRIEND_PENDING_OUT:
            # Crossed requests: both sides asked, so both sides have agreed.
            self._storage.set_friend_state(sender_hex, FRIEND_ACCEPTED)
            with self._lock:
                self._friend_hashes.add(sender_hex)
            self._send(sender_hex, MT_FRIEND_ACCEPT)
            self._fire_callbacks(sender_hex)
            RNS.log(
                f"TrenchChat [friends]: {sender_hex[:12]}… asked while we were "
                f"asking them — now friends",
                RNS.LOG_NOTICE,
            )
            return

        if state == FRIEND_PENDING_IN:
            return

        self._evict_oldest_pending()
        note = self._text(fields.get(F_FRIEND_NOTE))[:MAX_FRIEND_NOTE_CHARS]
        display_name = self._text(fields.get(F_DISPLAY_NAME))
        self._storage.upsert_friend(sender_hex, "", note, FRIEND_PENDING_IN)
        RNS.log(f"TrenchChat [friends]: friend request from {sender_hex[:12]}…",
                RNS.LOG_NOTICE)
        self._fire_callbacks(sender_hex)
        self._fire_request_callbacks(sender_hex, display_name, note)

    def _handle_accept(self, sender_hex: str) -> None:
        # Only an answer to a request we actually sent. An accept from an
        # identity we never asked must never create a friendship, or the gate
        # would be one message away from anyone.
        if self._storage.get_friend_state(sender_hex) != FRIEND_PENDING_OUT:
            RNS.log(
                f"TrenchChat [friends]: ignoring unsolicited accept from "
                f"{sender_hex[:12]}…",
                RNS.LOG_WARNING,
            )
            return
        self._storage.set_friend_state(sender_hex, FRIEND_ACCEPTED)
        with self._lock:
            self._friend_hashes.add(sender_hex)
        RNS.log(f"TrenchChat [friends]: {sender_hex[:12]}… accepted our request",
                RNS.LOG_NOTICE)
        self._fire_callbacks(sender_hex)

    def _handle_decline(self, sender_hex: str) -> None:
        if self._storage.get_friend_state(sender_hex) != FRIEND_PENDING_OUT:
            return
        self._storage.delete_friend(sender_hex)
        self._fire_callbacks(sender_hex)

    def _evict_oldest_pending(self) -> None:
        while (self._storage.count_friends_in_state(FRIEND_PENDING_IN)
               >= MAX_PENDING_FRIEND_REQUESTS):
            oldest = self._storage.oldest_friend_in_state(FRIEND_PENDING_IN)
            if oldest is None:
                return
            self._storage.delete_friend(oldest)
            self._storage.clear_message_requests(oldest)
            RNS.log(
                f"TrenchChat [friends]: dropped the oldest pending request "
                f"({oldest[:12]}…) — queue full",
                RNS.LOG_WARNING,
            )

    # --- outbound handshake ---

    def _send(self, dest_hex: str, msg_type: str, note: str = "") -> bool:
        if self._router is None or self._identity is None:
            return False
        fields = {F_MSG_TYPE: msg_type,
                  F_DISPLAY_NAME: self._identity.display_name}
        if msg_type == MT_FRIEND_REQUEST and note:
            fields[F_FRIEND_NOTE] = note[:MAX_FRIEND_NOTE_CHARS]
        return self._send_raw(dest_hex, fields)

    def _send_raw(self, dest_hex: str, fields: dict) -> bool:
        """Send a handshake message. False if it had to be queued instead."""
        if self._router is None:
            return False
        try:
            identity_hash = bytes.fromhex(dest_hex)
            delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
            dest_identity = RNS.Identity.recall(delivery_dest_hash)
            if dest_identity is None:
                RNS.Transport.request_path(delivery_dest_hash)
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
            lxm.fields = fields
            self._router.send(lxm)
            return True
        except Exception as e:
            RNS.log(f"TrenchChat [friends]: handshake send error: {e}", RNS.LOG_WARNING)
            return False

    # --- private helpers ---

    def _present(self, row: dict) -> dict:
        identity_hash = row["identity_hash"]
        last_seen_at = row["last_seen_at"]
        is_online = False
        if self._presence_mgr is not None:
            is_online = self._presence_mgr.is_online(identity_hash)
            last_seen_at = max(last_seen_at,
                               self._presence_mgr.last_seen_at(identity_hash))
        with self._lock:
            last_seen_at = max(last_seen_at, self._last_seen.get(identity_hash, 0.0))
        return {
            "identity_hash": identity_hash,
            "nickname": row["nickname"],
            "note": row["note"],
            "display_name": resolve_display_name(
                identity_hash, self._self_hex, self._storage
            ),
            "added_at": row["added_at"],
            "last_seen_at": last_seen_at,
            "is_online": is_online,
            "state": row.get("state", FRIEND_ACCEPTED),
        }

    def _sender_hex(self, message: LXMF.LXMessage) -> str:
        sender_identity = (RNS.Identity.recall(message.source_hash)
                           if message.source_hash else None)
        if sender_identity is not None:
            return sender_identity.hash.hex()
        return message.source_hash.hex() if message.source_hash else ""

    @staticmethod
    def _text(value) -> str:
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        return str(value) if value else ""

    def _fire_callbacks(self, identity_hash_hex: str) -> None:
        for cb in self._callbacks:
            try:
                cb(identity_hash_hex)
            except Exception as e:
                RNS.log(f"TrenchChat [friends]: callback error: {e}", RNS.LOG_ERROR)

    def _fire_request_callbacks(self, identity_hash_hex: str, display_name: str,
                                note: str) -> None:
        for cb in self._request_callbacks:
            try:
                cb(identity_hash_hex, display_name, note)
            except Exception as e:
                RNS.log(f"TrenchChat [friends]: request callback error: {e}",
                        RNS.LOG_ERROR)

    @staticmethod
    def _is_valid_hash(value: str) -> bool:
        if len(value) != IDENTITY_HASH_HEX_LEN:
            return False
        try:
            bytes.fromhex(value)
        except ValueError:
            return False
        return True
