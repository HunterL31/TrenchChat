"""
Who this node will hold a direct IP session with, and how one is opened.

A direct session discloses this node's addresses to the peer on the other end,
so it is offered to someone an admin vetted and invited and to nobody else: a
peer is eligible when this node and that peer are both current members of one
invite-only channel, or of one server, by the stored members table. Servers
are always invite-only. An open-join channel never qualifies whatever its
subscriber list says, because anyone can join one, and an accepted friendship
does not qualify either in this first cut.

is_eligible is the core enforcement layer of that gate, called by IPTransport
the moment an inbound HELLO proves an identity and before any frame of theirs
is read. UpgradeManager is the outbound guard over it (consider() refuses an
ineligible peer before a byte is sent) and the client gate under it
(config.upgrade_enabled off means no offers and no answers), plus the
handshake itself.

The handshake, between two peers that can already see each other on the mesh:

1. The smaller identity hash sends MT_UPGRADE_OFFER over LXMF, encrypted end to
   end like every control message: its candidates, a sixteen-byte nonce, its
   session certificate and the time it will start probing. The larger hash
   offers only after seeing the smaller one online for FALLBACK_OFFER_SECS
   without an offer, which covers a peer running an older build; it is the same
   fallback the voice plane uses for one-way reachability.
2. The receiver checks eligibility and every bound, answers MT_UPGRADE_ANSWER
   and starts probing at once. Probing first is the point of answering: its own
   outbound probes are what open its NAT, and Phase 0 found that a probe
   arriving before a NAT has made that mapping can take the very tuple the
   mapping wanted.
3. The offerer probes when the answer arrives, no earlier than the time it
   named. The first candidate pair seen both ways carries the QUIC handshake:
   the smaller hash dials it, the larger takes it on the punched socket.

Nothing here is periodic on the mesh. An offer is sent once per sighting of an
eligible peer under a backoff that doubles from thirty seconds to a day, and a
pair that cannot punch stays on Reticulum, which is where it already was.
"""

import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import RNS

from trenchchat.core.permissions import is_open_join, permissions_from_json
from trenchchat.core.protocol import (
    F_MSG_TYPE, F_UPGRADE_CANDIDATES, F_UPGRADE_CERT, F_UPGRADE_NONCE,
    F_UPGRADE_OBSERVED, F_UPGRADE_PUNCH_AT, MT_UPGRADE_ANSWER, MT_UPGRADE_OFFER,
    UPGRADE_NONCE_BYTES, upgrade_address, upgrade_candidates,
    upgrade_certificate, upgrade_nonce, upgrade_punch_at,
)
from trenchchat.core.storage import Storage
from trenchchat.network.base import SendState
from trenchchat.network.ip import candidates as candidate_gathering
from trenchchat.network.ip import punch as punching
from trenchchat.network.ip.portmap import PortMapper

# Why a pair has no session, as the diagnostics panel names it. A fixed set, so
# a client can say what to do about each one rather than print a sentence.
REASON_DISABLED = "disabled"
REASON_INELIGIBLE = "ineligible"
REASON_NO_ANSWER = "no_answer"
REASON_PUNCH_FAILED = "punch_failed"
REASON_HANDSHAKE_FAILED = "handshake_failed"
REASON_REFUSED = "refused"
REASON_BACKOFF = "backoff"

# Where the address kinds are remembered. 'peer' is where this node last saw
# that peer; 'self' is where that peer last saw this node.
ADDRESS_PEER = "peer"
ADDRESS_SELF = "self"

# A pair that failed waits this long before trying again, doubling to a day.
# Reset when either side's candidate set changes, because a new address is new
# evidence and the old failure says nothing about it.
BACKOFF_START_SECS = 30.0
BACKOFF_MAX_SECS = 24 * 3600.0

# How long the larger hash waits before offering, having seen the smaller one
# online and heard no offer. The voice plane's VOICE_DIAL_FALLBACK_SECS.
FALLBACK_OFFER_SECS = 10.0

# How far ahead an offer puts its punch time, so the answer has time to arrive
# and the answerer's own probes go first.
PUNCH_LEAD_SECS = 3.0

# How long an offer waits for its answer before the attempt is over.
OFFER_TIMEOUT_SECS = 30.0

# How long the QUIC handshake gets on the punched path.
SESSION_TIMEOUT_SECS = 15.0

# Attempts at once, which bounds probes in flight at eight candidates each.
MAX_CONCURRENT_ATTEMPTS = 4

# Peers tracked for sightings, failures and spent nonces. Identities are free
# to mint, so each of these is bounded rather than left to grow.
MAX_TRACKED_PEERS = 256
MAX_SPENT_NONCES = 512

# How often the router mapping is asked about. The mapper itself decides
# whether anything is due; this only keeps it off the tick's thread.
PORTMAP_INTERVAL_SECS = 60.0


def is_eligible(storage: Storage, self_hex: str, peer_hex: str) -> bool:
    """Whether this node and a peer share an invite-only channel or a server."""
    if not self_hex or not peer_hex or self_hex == peer_hex:
        return False
    shared = storage.member_scopes(self_hex) & storage.member_scopes(peer_hex)
    for scope_hash in shared:
        if storage.get_server(scope_hash) is not None:
            return True
        channel = storage.get_channel(scope_hash)
        if channel is None:
            continue
        if not is_open_join(permissions_from_json(channel["permissions"])):
            return True
    return False


class _Attempt:
    """One upgrade in flight with one peer."""

    def __init__(self, peer_hex: str, nonce: bytes, sock, offered: bool):
        self.peer_hex = peer_hex
        self.nonce = nonce
        self.sock = sock
        self.offered = offered
        self.started_at = time.time()
        self.punch_at = self.started_at + PUNCH_LEAD_SECS
        self.peer_candidates: list = []
        self.peer_cert = b""
        self.answered = False
        self.done = False
        self.probe_addresses: list = []


class UpgradeManager:
    """Offers, answers and opens direct sessions with eligible peers."""

    def __init__(self, identity, storage: Storage, router, presence_mgr,
                 config, transport=None):
        """
        identity: trenchchat.core.identity.Identity instance
        (passed in to avoid circular imports)
        transport: the IPTransport a session is opened on, or None for a node
        that holds none, which is what config.upgrade_enabled off gives it.
        """
        self._identity = identity
        self._self_hex = identity.hash_hex
        self._storage = storage
        self._router = router
        self._presence = presence_mgr
        self._config = config
        self._transport = transport

        self._lock = threading.Lock()
        self._attempts: dict[str, _Attempt] = {}
        self._failures: dict[str, dict] = {}
        self._backoff: dict[str, float] = {}
        self._next_attempt_at: dict[str, float] = {}
        self._first_seen: dict[str, float] = {}
        self._peer_candidate_sets: dict[str, tuple] = {}
        self._spent_nonces: deque = deque(maxlen=MAX_SPENT_NONCES)
        self._live_nonces: dict[bytes, str] = {}
        self._stopped = False
        self._last_portmap = 0.0

        self._pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_ATTEMPTS,
                                        thread_name_prefix="upgrade")
        self._mapper = (PortMapper(transport.listen_port)
                        if transport is not None and transport.listen_port
                        else None)
        if router is not None:
            router.add_delivery_callback(self._on_message)
        if transport is not None:
            transport.set_probe_responder(self._answer_probe)
            transport.set_observed_callback(self._note_observed)

    # --- the gate ---

    @property
    def enabled(self) -> bool:
        """Whether this node holds direct sessions at all."""
        return bool(self._config.upgrade_enabled) and self._transport is not None

    def set_enabled(self, enabled: bool) -> bool:
        """Turn this node's direct sessions on or off. Returns the new state.

        The client gate: off means no offer leaves and no offer is answered,
        and every session this node already holds is closed, because a switch
        that left them up would be a switch about nothing.
        """
        self._config.upgrade_enabled = bool(enabled)
        if not enabled and self._transport is not None:
            for entry in self._transport.sessions():
                peer_hex = entry.get("peer") or ""
                if peer_hex:
                    self._transport.close_session(
                        peer_hex, "direct connections turned off")
        RNS.log(f"TrenchChat [upgrade]: direct sessions are now "
                f"{'on' if enabled else 'off'}", RNS.LOG_NOTICE)
        return self.enabled

    def is_eligible(self, peer_hex: str) -> bool:
        """Whether a peer is one this node may hold a session with."""
        return is_eligible(self._storage, self._self_hex, peer_hex)

    def consider(self, peer_hex: str, now: float | None = None) -> str | None:
        """Why an offer to this peer would not be sent, or None to send one.

        The outbound guard: an ineligible peer is refused here, before an
        address of this node's has left it. Backoff is every "not now" there
        is, whether the pair already has a session, has an attempt in flight,
        is waiting out a failure, or is the larger hash before its fallback.
        The cheap in-memory answers come first so a peer that is waiting costs
        no database read.
        """
        now = time.time() if now is None else now
        if not self.enabled:
            return REASON_DISABLED
        if self._transport.can_reach(peer_hex):
            return REASON_BACKOFF
        with self._lock:
            if peer_hex in self._attempts:
                return REASON_BACKOFF
            waiting = now < self._next_attempt_at.get(peer_hex, 0.0)
            was_ineligible = (self._failures.get(peer_hex, {}).get("reason")
                              == REASON_INELIGIBLE)
            first_seen = self._first_seen.get(peer_hex)
        # A peer refused as ineligible is re-checked on every sighting rather
        # than waiting out a backoff: the answer changes the moment an admin
        # admits them, and the check is two reads of a table already in memory.
        if waiting and not was_ineligible:
            return REASON_BACKOFF
        if not self.is_eligible(peer_hex):
            return REASON_INELIGIBLE
        if was_ineligible:
            self._clear_failure(peer_hex)
        if self._self_hex > peer_hex:
            # The larger hash waits, so two peers do not both offer; the wait
            # ends anyway, so a peer that never offers is still upgraded.
            if first_seen is None or now - first_seen < FALLBACK_OFFER_SECS:
                return REASON_BACKOFF
        return None

    # --- events ---

    def on_peer_appeared(self, peer_hex: str) -> None:
        """A peer was heard from: note the sighting, and offer if it is ours to."""
        if not peer_hex or peer_hex == self._self_hex:
            return
        with self._lock:
            self._first_seen.setdefault(peer_hex, time.time())
            self._prune_tracked()
        if self._self_hex < peer_hex:
            self.offer(peer_hex)

    def tick(self, now: float | None = None) -> None:
        """Periodic housekeeping; call roughly once per second.

        Closes a session whose peer has stopped being eligible, gives up on an
        offer nothing answered, makes the larger hash's fallback offer, and
        keeps the router mapping alive.
        """
        now = time.time() if now is None else now
        self._sweep_sessions()
        self._expire_attempts(now)
        self._fallback_offers(now)
        self._refresh_mapping(now)

    def _sweep_sessions(self) -> None:
        """Drop any session whose peer is no longer a member. The core re-check."""
        if self._transport is None:
            return
        for entry in self._transport.sessions():
            peer_hex = entry.get("peer") or ""
            if not peer_hex or self.is_eligible(peer_hex):
                continue
            RNS.log(f"TrenchChat [upgrade]: closing the session with "
                    f"{peer_hex[:12]}…: no longer a member of a shared "
                    f"invite-only channel", RNS.LOG_WARNING)
            self._transport.close_session(peer_hex, "no longer eligible")
            self._record_failure(peer_hex, REASON_INELIGIBLE)

    def _expire_attempts(self, now: float) -> None:
        with self._lock:
            stale = [attempt for attempt in self._attempts.values()
                     if not attempt.answered
                     and now - attempt.started_at > OFFER_TIMEOUT_SECS]
        for attempt in stale:
            self._finish(attempt, REASON_NO_ANSWER)

    def _fallback_offers(self, now: float) -> None:
        if not self.enabled:
            return
        with self._lock:
            waiting = [peer_hex for peer_hex, seen in self._first_seen.items()
                       if self._self_hex > peer_hex
                       and now - seen >= FALLBACK_OFFER_SECS]
        for peer_hex in waiting:
            if self._presence is not None and not self._presence.is_online(peer_hex):
                continue
            self.offer(peer_hex, now=now)

    def _refresh_mapping(self, now: float) -> None:
        if self._mapper is None or now - self._last_portmap < PORTMAP_INTERVAL_SECS:
            return
        self._last_portmap = now
        self._submit(self._mapper.refresh)

    # --- offering ---

    def offer(self, peer_hex: str, *, ignore_backoff: bool = False,
              now: float | None = None) -> str | None:
        """Start an upgrade with a peer. Returns why it did not, or None.

        ignore_backoff is what a person pressing "Try now" gets: the gate still
        holds, the waiting does not. A backoff is never recorded as a failure,
        because it is not one: it would overwrite the reason the pair is
        actually waiting on and double the wait for asking.
        """
        refusal = self.consider(peer_hex, now)
        if refusal is None:
            self._submit(self._run_offer, peer_hex)
            return None
        if refusal == REASON_BACKOFF:
            if ignore_backoff and self._free_to_try(peer_hex):
                self._submit(self._run_offer, peer_hex)
                return None
            return refusal
        self._record_failure(peer_hex, refusal)
        return refusal

    def _free_to_try(self, peer_hex: str) -> bool:
        """Whether nothing but the waiting stands in the way of an attempt."""
        if not self.enabled or self._transport.can_reach(peer_hex):
            return False
        if not self.is_eligible(peer_hex):
            return False
        with self._lock:
            return peer_hex not in self._attempts

    def _run_offer(self, peer_hex: str) -> None:
        """Bind a socket, name this node's candidates, and send the offer."""
        attempt = self._begin(peer_hex, os.urandom(UPGRADE_NONCE_BYTES),
                              offered=True)
        if attempt is None:
            return
        own = self._own_candidates(attempt)
        if not own:
            RNS.log(f"TrenchChat [upgrade]: not offering {peer_hex[:12]}… a "
                    f"session: this node has no address to name", RNS.LOG_DEBUG)
            self._finish(attempt, REASON_PUNCH_FAILED)
            return
        fields = {
            F_MSG_TYPE: MT_UPGRADE_OFFER,
            F_UPGRADE_CANDIDATES: own,
            F_UPGRADE_NONCE: attempt.nonce,
            F_UPGRADE_CERT: self._transport.certificate_der,
            F_UPGRADE_PUNCH_AT: attempt.punch_at,
        }
        self._add_observed(fields, peer_hex)
        if self._router.send(peer_hex, fields) is SendState.NO_PATH:
            RNS.log(f"TrenchChat [upgrade]: no path to offer {peer_hex[:12]}… "
                    f"a direct session", RNS.LOG_DEBUG)
            self._finish(attempt, REASON_NO_ANSWER)
            return
        RNS.log(f"TrenchChat [upgrade]: offered {peer_hex[:12]}… a direct "
                f"session on {len(fields[F_UPGRADE_CANDIDATES])} candidates",
                RNS.LOG_NOTICE)

    # --- inbound ---

    def _on_message(self, message) -> None:
        """Every inbound message; only an offer or an answer is ours."""
        fields = message.fields or {}
        msg_type = fields.get(F_MSG_TYPE)
        if isinstance(msg_type, bytes):
            msg_type = msg_type.decode(errors="replace")
        if msg_type == MT_UPGRADE_OFFER:
            self._handle_offer(message.source_hex, fields)
        elif msg_type == MT_UPGRADE_ANSWER:
            self._handle_answer(message.source_hex, fields)

    def _handle_offer(self, peer_hex: str, fields: dict) -> None:
        """Check an offer against the gate and every bound, then answer it."""
        if not self.enabled:
            RNS.log(f"TrenchChat [upgrade]: ignoring an offer from "
                    f"{peer_hex[:12]}…: direct sessions are off", RNS.LOG_DEBUG)
            return
        if not peer_hex or peer_hex == self._self_hex:
            return
        if not self.is_eligible(peer_hex):
            RNS.log(f"TrenchChat [upgrade]: refused an offer from "
                    f"{peer_hex[:12]}…: not a member of a shared invite-only "
                    f"channel", RNS.LOG_WARNING)
            self._record_failure(peer_hex, REASON_INELIGIBLE)
            return
        parsed = self._read_offer(peer_hex, fields)
        if parsed is None:
            return
        nonce, peer_candidates, peer_cert, punch_at = parsed
        if self._transport.can_reach(peer_hex):
            return
        with self._lock:
            mine = self._attempts.get(peer_hex)
        if mine is not None:
            # Both sides offered at once, which the tie-break makes rare rather
            # than impossible. The smaller hash's offer wins, so the two never
            # sit waiting for answers neither will send.
            if peer_hex < self._self_hex and mine.offered and not mine.answered:
                self._finish(mine, None)
            else:
                RNS.log(f"TrenchChat [upgrade]: ignoring an offer from "
                        f"{peer_hex[:12]}…: one is already in flight",
                        RNS.LOG_DEBUG)
                return
        self._note_candidate_set(peer_hex, peer_candidates)
        self._submit(self._run_answer, peer_hex, nonce, peer_candidates,
                     peer_cert, punch_at)

    def _read_offer(self, peer_hex: str, fields: dict):
        """An offer's four values, all bounded, or None with the refusal logged."""
        nonce = upgrade_nonce(fields.get(F_UPGRADE_NONCE))
        peer_candidates = upgrade_candidates(fields.get(F_UPGRADE_CANDIDATES))
        peer_cert = upgrade_certificate(fields.get(F_UPGRADE_CERT))
        punch_at = upgrade_punch_at(fields.get(F_UPGRADE_PUNCH_AT))
        refusal = ""
        if nonce is None:
            refusal = "the nonce is not sixteen bytes"
        elif not peer_candidates:
            refusal = "it names no usable candidate"
        elif peer_cert is None:
            refusal = "the certificate is missing or over its cap"
        elif punch_at is None:
            refusal = "the punch time is not soon"
        elif nonce in self._spent_nonces:
            refusal = "the nonce has been used before"
        if refusal:
            RNS.log(f"TrenchChat [upgrade]: refused an offer from "
                    f"{peer_hex[:12]}…: {refusal}", RNS.LOG_WARNING)
            self._record_failure(peer_hex, REASON_REFUSED)
            return None
        self._remember_self_address(peer_hex, fields)
        return nonce, peer_candidates, peer_cert, punch_at

    def _run_answer(self, peer_hex: str, nonce: bytes, peer_candidates: list,
                    peer_cert: bytes, punch_at: float) -> None:
        """Answer an offer and start probing, which opens this node's mapping."""
        attempt = self._begin(peer_hex, nonce, offered=False)
        if attempt is None:
            return
        attempt.peer_candidates = peer_candidates
        attempt.peer_cert = peer_cert
        attempt.punch_at = punch_at
        own = self._own_candidates(attempt)
        if not own:
            RNS.log(f"TrenchChat [upgrade]: not answering {peer_hex[:12]}…: "
                    f"this node has no address to name", RNS.LOG_DEBUG)
            self._finish(attempt, REASON_PUNCH_FAILED)
            return
        fields = {
            F_MSG_TYPE: MT_UPGRADE_ANSWER,
            F_UPGRADE_CANDIDATES: own,
            F_UPGRADE_NONCE: nonce,
            F_UPGRADE_CERT: self._transport.certificate_der,
            F_UPGRADE_PUNCH_AT: time.time(),
        }
        self._add_observed(fields, peer_hex)
        if self._router.send(peer_hex, fields) is SendState.NO_PATH:
            self._finish(attempt, REASON_NO_ANSWER)
            return
        attempt.answered = True
        self._punch(attempt, start_at=None)

    def _handle_answer(self, peer_hex: str, fields: dict) -> None:
        """Match an answer to the offer it belongs to, then probe."""
        if not self.enabled or not peer_hex:
            return
        if not self.is_eligible(peer_hex):
            self._record_failure(peer_hex, REASON_INELIGIBLE)
            return
        nonce = upgrade_nonce(fields.get(F_UPGRADE_NONCE))
        with self._lock:
            attempt = self._attempts.get(peer_hex)
            matched = (attempt is not None and nonce is not None
                       and attempt.offered and not attempt.answered
                       and attempt.nonce == nonce)
        if not matched:
            RNS.log(f"TrenchChat [upgrade]: dropped an answer from "
                    f"{peer_hex[:12]}…: it matches no offer of ours",
                    RNS.LOG_WARNING)
            self._record_failure(peer_hex, REASON_REFUSED)
            return
        peer_candidates = upgrade_candidates(fields.get(F_UPGRADE_CANDIDATES))
        peer_cert = upgrade_certificate(fields.get(F_UPGRADE_CERT))
        if not peer_candidates or peer_cert is None:
            RNS.log(f"TrenchChat [upgrade]: dropped an answer from "
                    f"{peer_hex[:12]}…: it names no candidate or certificate",
                    RNS.LOG_WARNING)
            self._finish(attempt, REASON_REFUSED)
            return
        self._remember_self_address(peer_hex, fields)
        self._note_candidate_set(peer_hex, peer_candidates)
        attempt.peer_candidates = peer_candidates
        attempt.peer_cert = peer_cert
        attempt.answered = True
        self._submit(self._punch, attempt, attempt.punch_at)

    # --- the punch and the session ---

    def _punch(self, attempt: _Attempt, start_at: float | None) -> None:
        """Probe the peer's candidates, then carry the handshake on the winner."""
        targets = list(attempt.peer_candidates)
        remembered = self._storage.get_upgrade_address(attempt.peer_hex,
                                                       ADDRESS_PEER)
        if remembered is not None:
            targets.append((remembered[0], remembered[1]))
        result = punching.punch(
            attempt.sock, targets, attempt.nonce, start_at=start_at,
            on_probe=lambda source: attempt.probe_addresses.append(source),
        )
        for source in list(attempt.probe_addresses):
            self._storage.record_upgrade_address(
                attempt.peer_hex, ADDRESS_PEER, source[0], source[1])
        if not result.punched:
            RNS.log(f"TrenchChat [upgrade]: no path punched to "
                    f"{attempt.peer_hex[:12]}… in {result.seconds:.1f}s over "
                    f"{len(targets)} candidates", RNS.LOG_WARNING)
            self._finish(attempt, REASON_PUNCH_FAILED)
            return
        host, port = result.remote
        sock = attempt.sock
        attempt.sock = None
        if self._self_hex < attempt.peer_hex:
            opened = self._transport.open_session(
                attempt.peer_hex, host, port, attempt.peer_cert, sock=sock,
                timeout=SESSION_TIMEOUT_SECS)
        else:
            opened = self._transport.accept_on(
                sock, attempt.peer_hex, attempt.peer_cert,
                timeout=SESSION_TIMEOUT_SECS)
        if not opened:
            self._finish(attempt, REASON_HANDSHAKE_FAILED)
            return
        RNS.log(f"TrenchChat [upgrade]: direct session with "
                f"{attempt.peer_hex[:12]}… over {host}:{port}", RNS.LOG_NOTICE)
        self._finish(attempt, None)

    # --- attempts ---

    def _begin(self, peer_hex: str, nonce: bytes, offered: bool) -> _Attempt | None:
        """Take the slot for this peer and bind the socket the session runs on."""
        try:
            sock = punching.bind_socket()
        except OSError as e:
            RNS.log(f"TrenchChat [upgrade]: could not bind a socket to punch "
                    f"with {peer_hex[:12]}…: {e}", RNS.LOG_WARNING)
            self._record_failure(peer_hex, REASON_HANDSHAKE_FAILED)
            return None
        attempt = _Attempt(peer_hex, nonce, sock, offered)
        with self._lock:
            if peer_hex in self._attempts or self._stopped:
                sock.close()
                return None
            self._attempts[peer_hex] = attempt
            self._live_nonces[nonce] = peer_hex
            self._spent_nonces.append(nonce)
        return attempt

    def _finish(self, attempt: _Attempt, reason: str | None) -> None:
        """End an attempt once, recording what it cost and when to try again."""
        with self._lock:
            if attempt.done:
                return
            attempt.done = True
            if self._attempts.get(attempt.peer_hex) is attempt:
                del self._attempts[attempt.peer_hex]
            self._live_nonces.pop(attempt.nonce, None)
            sock = attempt.sock
            attempt.sock = None
        if sock is not None:
            sock.close()
        if reason is None:
            self._clear_failure(attempt.peer_hex)
        else:
            self._record_failure(attempt.peer_hex, reason)

    def _own_candidates(self, attempt: _Attempt) -> list:
        """This node's candidates for one attempt, as the wire carries them."""
        mapped = self._mapper.address() if self._mapper is not None else None
        observed = self._storage.get_upgrade_addresses(ADDRESS_SELF, limit=2)
        gathered = candidate_gathering.gather(
            attempt.sock.getsockname()[1], mapped=mapped, observed=observed)
        return [[host, port, kind] for host, port, kind in gathered]

    def _add_observed(self, fields: dict, peer_hex: str) -> None:
        """Tell a peer where its probes last arrived from, if they ever did.

        The Phase 0 harness showed this is the only recovery from a NAT that
        remapped a port: the peer's own candidate is wrong and nothing but an
        observation from outside can say so.
        """
        seen = self._storage.get_upgrade_address(peer_hex, ADDRESS_PEER)
        if seen is not None:
            fields[F_UPGRADE_OBSERVED] = [seen[0], seen[1]]

    def _note_observed(self, peer_hex: str, host: str, port: int) -> None:
        """Record where a peer's session saw this node arrive from.

        The accepting side of every session says so in its hello, so one member
        this node can already reach teaches it its own translated address, and
        that address is then a candidate to offer anybody else. Nothing about
        it is trusted: it is a place to aim a probe, and a session still
        authenticates from nothing.
        """
        self._storage.record_upgrade_address(peer_hex, ADDRESS_SELF, host, port)
        RNS.log(f"TrenchChat [upgrade]: {peer_hex[:12]}… saw this node at "
                f"{host}:{port}", RNS.LOG_DEBUG)

    def _remember_self_address(self, peer_hex: str, fields: dict) -> None:
        """Record where a peer says it saw this node, as a candidate for later."""
        observed = upgrade_address(fields.get(F_UPGRADE_OBSERVED))
        if observed is not None:
            self._storage.record_upgrade_address(peer_hex, ADDRESS_SELF,
                                                 observed[0], observed[1])

    def _note_candidate_set(self, peer_hex: str, peer_candidates: list) -> None:
        """A peer that moved gets a clean slate: the old failure is about an
        address it no longer has."""
        current = tuple(sorted((host, port) for host, port, _kind in peer_candidates))
        with self._lock:
            previous = self._peer_candidate_sets.get(peer_hex)
            self._peer_candidate_sets[peer_hex] = current
            changed = previous is not None and previous != current
        if changed:
            self._clear_failure(peer_hex)

    def _answer_probe(self, data: bytes, source, send) -> bool:
        """Answer a probe that arrived on the listening socket.

        A probe aimed at a mapped or observed candidate lands there rather than
        on an attempt's own socket. Runs on the transport's loop, so it records
        the address in memory and leaves the writing to the attempt.
        """
        for nonce, peer_hex in list(self._live_nonces.items()):
            if not punching.answer_probe(data, source, send, nonce):
                continue
            with self._lock:
                attempt = self._attempts.get(peer_hex)
            if attempt is not None and source not in attempt.probe_addresses:
                attempt.probe_addresses.append(source)
            return True
        return False

    # --- failures ---

    def _record_failure(self, peer_hex: str, reason: str,
                        now: float | None = None) -> None:
        """Record why a pair has no session, and when to try again.

        The wait doubles per attempt, not per ask: the same reason recorded
        again while the pair is still waiting it out changes nothing, so a peer
        that announces every ten seconds cannot push its own next attempt into
        next week.
        """
        now = time.time() if now is None else now
        with self._lock:
            standing = self._failures.get(peer_hex)
            if (standing is not None and standing["reason"] == reason
                    and now < standing["next_attempt"]):
                return
            previous = self._backoff.get(peer_hex, 0.0)
            wait = min(previous * 2 if previous else BACKOFF_START_SECS,
                       BACKOFF_MAX_SECS)
            self._backoff[peer_hex] = wait
            self._next_attempt_at[peer_hex] = now + wait
            self._failures[peer_hex] = {"reason": reason, "at": now,
                                        "next_attempt": now + wait}
            self._prune_tracked()
        RNS.log(f"TrenchChat [upgrade]: {peer_hex[:12]}… has no direct session "
                f"({reason}); next attempt in {wait:.0f}s", RNS.LOG_DEBUG)

    def _clear_failure(self, peer_hex: str) -> None:
        with self._lock:
            self._failures.pop(peer_hex, None)
            self._backoff.pop(peer_hex, None)
            self._next_attempt_at.pop(peer_hex, None)

    def _prune_tracked(self) -> None:
        """Keep the per-peer books bounded. Called under the lock."""
        for book in (self._failures, self._backoff, self._next_attempt_at,
                     self._first_seen, self._peer_candidate_sets):
            while len(book) > MAX_TRACKED_PEERS:
                book.pop(next(iter(book)))

    def failures(self) -> dict:
        """Why each peer with no session has none, for the diagnostics panel."""
        with self._lock:
            return {peer_hex: dict(entry)
                    for peer_hex, entry in self._failures.items()}

    def attempt_count(self) -> int:
        """How many upgrades are in flight."""
        with self._lock:
            return len(self._attempts)

    # --- lifecycle ---

    def _submit(self, fn, *args) -> None:
        """Run one piece of work off whatever thread called in.

        Every path here blocks (a punch takes seconds, a handshake can take
        more), and callers are RNS callback threads and the ticker.
        """
        if self._stopped:
            return
        try:
            self._pool.submit(self._guard, fn, *args)
        except RuntimeError:
            pass

    @staticmethod
    def _guard(fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:
            RNS.log(f"TrenchChat [upgrade]: attempt failed: {e}", RNS.LOG_ERROR)

    def stop(self) -> None:
        """Drop every attempt in flight and give the router mapping back."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            attempts = list(self._attempts.values())
            self._attempts.clear()
            self._live_nonces.clear()
        for attempt in attempts:
            if attempt.sock is not None:
                attempt.sock.close()
                attempt.sock = None
        if self._router is not None:
            self._router.remove_delivery_callback(self._on_message)
        if self._transport is not None:
            self._transport.set_probe_responder(None)
        self._pool.shutdown(wait=False)
        if self._mapper is not None:
            self._mapper.release()
