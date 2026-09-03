"""
File plane: RNS Link lifecycle for pulling shared file chunks from a holder.

A file is never pushed. A member that wants one asks a holder for a range of
chunks over a link, on TrenchChat's own "files" aspect, and the holder answers
with the bytes or with silence. One request path carries everything: the hash
of the file, the first chunk index and how many chunks, or a flag asking for
the chunk-hash list instead. A path per file, nomad's /file/<name> shape,
would need a register and a deregister on every store and every prune; the
hash in the request data needs neither, and is no less private, because RNS
hashes paths anyway. Chunk ranges are what makes a dropped link cost
one request rather than a whole transfer, since RNS response resources never
resume across links.

The dial ladder, the per-holder queue and the request plumbing are the shared
link client (network/link_client.py). What is specific here: the outbound
destination is derived from the holder's identity the way messaging derives a
delivery destination, every outbound link identifies (a holder must know who
is asking before it can check membership), and a request that stops making
progress fails on a stall timeout rather than a total deadline, because a slow
transfer is not an error.

A holder announces on this aspect, and only while it actually holds
something. Registering the destination is not enough on its own: a path
request for a destination that has never announced is answered by the node
that owns it, but a transport node in between neither knows the path nor
searches for one (`Interface.DISCOVER_PATHS_FOR` leaves out the ordinary
full mode), so on any mesh with a hop in it the request dies there and every
fetch fails as unreachable. Who announces and when is the core layer's
decision (files.py), because whether there is anything to serve is its
question; this module only sends the packet.

Authorisation is not done here. The serve callback is the core layer's
membership check (files.py), and this module hands it the requester's identity
hash, or None when the link carried no identity, so a refusal is that layer's
answer. This module never touches Storage or core managers, matching
node_transport.py's layering: callbacks up, tick() down.
"""

import time

import RNS

from trenchchat import APP_NAME, APP_ASPECT_FILES
from trenchchat.core.protocol import FILE_CHUNK_BYTES, MAX_SHARED_FILE_BYTES
from trenchchat.network.link_client import (
    FETCH_BAD_PATH, FETCH_BAD_RESPONSE, MAX_INBOUND_LINKS,
    MAX_SERVED_RESPONSE_BYTES, LinkClient, LinkClientBase, LinkFetch,
)

FILE_REQUEST_PATH = "/tc/file"

# Request keys, kept to one byte each: this dict rides every chunk request.
R_FILE_HASH = "h"
R_FIRST = "i"
R_COUNT = "n"
R_WANT_LIST = "l"

CHUNK_HASH_BYTES = 32
FILE_HASH_BYTES = 32

# Most chunks one request may ask for: 512 KB, inside the size RNS carries as
# one Resource segment. A ceiling for a fast link, never a target on a slow
# one, where the window never climbs near it.
FILE_REQUEST_MAX_CHUNKS = 16
MAX_CHUNK_INDEX = MAX_SHARED_FILE_BYTES // FILE_CHUNK_BYTES

# RNS measures a response against max_response_size as the msgpack envelope
# it puts on the wire ([request_id, payload]), not as the payload alone, so a
# full-size range needs headroom or the requester rejects its own answer.
RESPONSE_ENVELOPE_BYTES = 64

# The chunk list is one hash per chunk of the largest file, and it rides the
# same envelope, so its ceiling carries the same headroom.
MAX_CHUNK_LIST_BYTES = ((MAX_CHUNK_INDEX + 1) * CHUNK_HASH_BYTES
                        + RESPONSE_ENVELOPE_BYTES)

# How long a request may sit with no answer at all. RNS stops applying it once
# a response starts arriving, which is what leaves room for the stall timeout.
FILE_FETCH_TIMEOUT_SECS = 120.0
# No progress on an in-flight request for this long ends the request, not the
# download. There is deliberately no total deadline: a 5 MB transfer over LoRa
# takes hours and is not an error. Measured against one chunk of airtime on the
# slowest profile, which is why it moves only alongside FILE_CHUNK_BYTES:
# 32 KB is about 47 s at SF7, comfortably inside this sweep.
FILE_STALL_SECS = 120.0
# A download issues its requests back to back, so a link nobody has used for
# this long belongs to a download that has finished or moved to another holder.
FILE_LINK_IDLE_SECS = 120.0

MAX_INBOUND_FILE_LINKS = MAX_INBOUND_LINKS
# Airtime is shared: two downloads out at once, and the third requester is
# told nothing and comes back later. The slot is held per link rather than
# per request, because a download issues one request at a time: the next
# range on a link already serving is the same transfer continuing, and
# counting it as a third would refuse every download that got past its first
# chunk.
MAX_CONCURRENT_SERVES = 2
# RNS builds the response Resource itself, after the handler returns, and
# exposes no concluded callback for it, so a link's serve slot is counted as
# free when it has no outgoing resource left (RNS drops one from
# link.outgoing_resources when it concludes or is cancelled). The settle floor
# covers the gap between the handler returning and the Resource existing; the
# window is the backstop for a slot that never clears.
FILE_SERVE_SETTLE_SECS = 2.0
FILE_SERVE_WINDOW_SECS = 900.0

# Fetch failure reasons this plane adds to the shared ones.
FETCH_STALLED = "stalled"
FETCH_REFUSED = "refused"


def _request_value(data: dict, key: str):
    """One request field, whether msgpack delivered its key as str or bytes."""
    if key in data:
        return data[key]
    return data.get(key.encode("utf-8"))


def _is_index(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_file_request(data) -> tuple[str, int, int, bool] | None:
    """An inbound request as (file_hash_hex, first, count, want_list).

    None for anything that is not exactly the shape this plane speaks. The
    sender is a peer, so every field is bounded here rather than downstream.
    """
    if not isinstance(data, dict):
        return None
    file_hash = _request_value(data, R_FILE_HASH)
    if not isinstance(file_hash, (bytes, bytearray)):
        return None
    if len(file_hash) != FILE_HASH_BYTES:
        return None
    file_hash_hex = bytes(file_hash).hex()
    if _request_value(data, R_WANT_LIST):
        return file_hash_hex, 0, 0, True
    first = _request_value(data, R_FIRST)
    count = _request_value(data, R_COUNT)
    if not _is_index(first) or not _is_index(count):
        return None
    if first > MAX_CHUNK_INDEX:
        return None
    if count < 1 or count > FILE_REQUEST_MAX_CHUNKS:
        return None
    return file_hash_hex, first, count, False


def max_response_for(count: int) -> int:
    """The largest response a chunk request of this size may accept."""
    return count * FILE_CHUNK_BYTES + RESPONSE_ENVELOPE_BYTES


class FileTransportBase(LinkClientBase):
    """Injectable transport interface; see RNSFileTransport for semantics."""

    log_tag = "files"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._serve_cb = None

    def set_result_callback(self, cb) -> None:
        """cb(fetch_id, ok, payload: bytes | None, reason)"""
        self._result_cb = cb

    def set_progress_callback(self, cb) -> None:
        """cb(fetch_id, progress: float) with progress in 0.0-1.0"""
        self._progress_cb = cb

    def set_serve_callback(self, cb) -> None:
        """cb(remote_identity_hash_hex, file_hash_hex, first, count,
        want_list) -> bytes | None

        The core layer answers an inbound request here, and refuses one by
        returning None: this is where the membership check lives. The
        requester's identity hash is None when the link carried no identity.
        """
        self._serve_cb = cb

    def fetch_chunks(self, fetch_id: str, holder_hex: str,
                     file_hash_hex: str, first: int, count: int,
                     timeout: float = FILE_FETCH_TIMEOUT_SECS) -> None:
        raise NotImplementedError

    def fetch_chunk_list(self, fetch_id: str, holder_hex: str,
                         file_hash_hex: str,
                         timeout: float = FILE_FETCH_TIMEOUT_SECS) -> None:
        raise NotImplementedError

    def cancel(self, fetch_id: str) -> None:
        raise NotImplementedError

    def start_serving(self) -> None:
        raise NotImplementedError

    def stop_serving(self) -> None:
        raise NotImplementedError

    def announce(self) -> None:
        raise NotImplementedError

    def drop_link(self, holder_hex: str) -> bool:
        raise NotImplementedError

    def tick(self) -> None:
        raise NotImplementedError

    # shared helpers for subclasses

    def _call_serve(self, requester_hex: str | None, file_hash_hex: str,
                    first: int, count: int, want_list: bool) -> bytes | None:
        """Ask the core layer for a range, or for the chunk-hash list."""
        if self._serve_cb is None:
            return None
        try:
            payload = self._serve_cb(requester_hex, file_hash_hex, first,
                                     count, want_list)
        except Exception as e:
            RNS.log(f"TrenchChat [files]: serve callback error for "
                    f"{file_hash_hex[:12]}…: {e}", RNS.LOG_ERROR)
            return None
        if payload is None:
            return None
        if not isinstance(payload, (bytes, bytearray)):
            RNS.log(f"TrenchChat [files]: serve callback returned "
                    f"{type(payload).__name__} for {file_hash_hex[:12]}…",
                    RNS.LOG_ERROR)
            return None
        return bytes(payload)

    def _valid_fetch(self, fetch_id: str, file_hash_hex: str, first: int,
                     count: int) -> bytes | None:
        """The file hash as bytes, or None after failing the fetch."""
        try:
            file_hash = bytes.fromhex(file_hash_hex)
        except ValueError:
            file_hash = b""
        if len(file_hash) != FILE_HASH_BYTES:
            RNS.log(f"TrenchChat [files]: refusing to fetch a file hash of "
                    f"{len(file_hash)} bytes", RNS.LOG_WARNING)
            self._notify_result(fetch_id, False, None, FETCH_BAD_PATH)
            return None
        if not _is_index(first) or first > MAX_CHUNK_INDEX \
                or not _is_index(count) or count < 1 \
                or count > FILE_REQUEST_MAX_CHUNKS:
            RNS.log(f"TrenchChat [files]: refusing to fetch chunks "
                    f"{first}+{count} of {file_hash_hex[:12]}…",
                    RNS.LOG_WARNING)
            self._notify_result(fetch_id, False, None, FETCH_BAD_PATH)
            return None
        return file_hash


class RNSFileTransport(LinkClient, FileTransportBase):
    """Real RNS Link implementation of the file plane."""

    link_idle_secs = FILE_LINK_IDLE_SECS
    max_inbound_links = MAX_INBOUND_FILE_LINKS

    def __init__(self, identity):
        super().__init__(identity=identity)
        self._in_dest = None
        # link id -> when its latest response was handed to RNS.
        self._serves: dict[bytes, float] = {}

    # --- fetching ---

    def fetch_chunks(self, fetch_id: str, holder_hex: str,
                     file_hash_hex: str, first: int, count: int,
                     timeout: float = FILE_FETCH_TIMEOUT_SECS) -> None:
        """Ask a holder for chunks [first, first + count) of a file."""
        file_hash = self._valid_fetch(fetch_id, file_hash_hex, first, count)
        if file_hash is None:
            return
        data = {R_FILE_HASH: file_hash, R_FIRST: first, R_COUNT: count}
        self._enqueue(LinkFetch(fetch_id, holder_hex, FILE_REQUEST_PATH,
                                max_response_for(count), timeout, data))

    def fetch_chunk_list(self, fetch_id: str, holder_hex: str,
                         file_hash_hex: str,
                         timeout: float = FILE_FETCH_TIMEOUT_SECS) -> None:
        """Ask a holder for the concatenated chunk hashes of a file."""
        file_hash = self._valid_fetch(fetch_id, file_hash_hex, 0, 1)
        if file_hash is None:
            return
        data = {R_FILE_HASH: file_hash, R_WANT_LIST: 1}
        self._enqueue(LinkFetch(fetch_id, holder_hex, FILE_REQUEST_PATH,
                                MAX_CHUNK_LIST_BYTES, timeout, data))

    # --- link client hooks ---

    def _dial_destination(self, holder_hex: str):
        delivery_hash = RNS.Destination.hash(
            bytes.fromhex(holder_hex), "lxmf", "delivery")
        peer_identity = RNS.Identity.recall(delivery_hash)
        if peer_identity is None:
            RNS.Transport.request_path(delivery_hash)
            return None, None
        dest = RNS.Destination(
            peer_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            APP_NAME,
            APP_ASPECT_FILES,
        )
        if not RNS.Transport.has_path(dest.hash):
            RNS.Transport.request_path(dest.hash)
            return None, None
        return dest, None

    def _should_identify(self, holder_hex: str) -> bool:
        # Never optional here: the holder cannot check membership without
        # knowing who is asking, so an anonymous link gets nothing served.
        return True

    def _handle_response(self, fetch: LinkFetch, receipt) -> None:
        data = receipt.response
        if isinstance(data, bytearray):
            data = bytes(data)
        if data is None or data == b"":
            # A holder that refuses answers with silence, which surfaces as a
            # timeout; an empty answer is the explicit refusal.
            self._notify_result(fetch.fetch_id, False, None, FETCH_REFUSED)
            return
        if not isinstance(data, bytes):
            self._notify_result(fetch.fetch_id, False, None,
                                FETCH_BAD_RESPONSE)
            return
        self._notify_result(fetch.fetch_id, True, data, None)

    def _sweep_active(self, now: float) -> list[tuple[LinkFetch, str]]:
        return [(fetch, FETCH_STALLED) for fetch in self._active.values()
                if now - fetch.last_progress_at >= FILE_STALL_SECS]

    # --- housekeeping ---

    def tick(self) -> None:
        self._tick_links()
        with self._lock:
            self._release_finished_serves(time.time())

    # --- serving ---

    def start_serving(self) -> None:
        """Register the inbound destination and its one request path.

        The destination outlives a stop, so serving again reuses it rather
        than registering a second destination for the same aspect.
        """
        with self._lock:
            if self._in_dest is None:
                self._in_dest = RNS.Destination(
                    self._identity.rns_identity,
                    RNS.Destination.IN,
                    RNS.Destination.SINGLE,
                    APP_NAME,
                    APP_ASPECT_FILES,
                )
                self._in_dest.set_link_established_callback(
                    self._on_inbound_link)
            # ALLOW_ALL at the RNS level: its allowed_list is one static
            # identity list per handler, and membership is per channel and
            # changes, so the check belongs in the serve callback.
            self._in_dest.register_request_handler(
                FILE_REQUEST_PATH,
                response_generator=self._serve,
                allow=RNS.Destination.ALLOW_ALL,
                auto_compress=True,
            )

    def announce(self) -> None:
        """Say this destination exists, so a member can resolve a path to it.

        Carries no app data: the hash is the whole message, and what a peer
        does with it (ask for a file) needs nothing else. Silent before
        start_serving, since there is nothing to reach yet.
        """
        with self._lock:
            dest = self._in_dest
        if dest is None:
            return
        try:
            dest.announce()
        except Exception as e:
            RNS.log(f"TrenchChat [files]: could not announce: {e}",
                    RNS.LOG_WARNING)

    def stop_serving(self) -> None:
        with self._lock:
            if self._in_dest is None:
                return
            try:
                self._in_dest.deregister_request_handler(FILE_REQUEST_PATH)
            except Exception as e:
                RNS.log(f"TrenchChat [files]: could not deregister the "
                        f"request handler: {e}", RNS.LOG_DEBUG)
            self._serves = {}

    def _release_finished_serves(self, now: float) -> None:
        """Caller holds the lock. Drop the slots whose responses are done."""
        for link_id, started_at in list(self._serves.items()):
            if now - started_at < FILE_SERVE_SETTLE_SECS:
                continue
            link = self._inbound_link(link_id)
            outgoing = getattr(link, "outgoing_resources", None) \
                if link is not None else None
            if (link is None or not outgoing
                    or now - started_at >= FILE_SERVE_WINDOW_SECS):
                del self._serves[link_id]

    def _begin_serve(self, link_id: bytes, now: float) -> bool:
        """Caller holds the lock. Whether this link may be answered now."""
        self._release_finished_serves(now)
        if link_id not in self._serves \
                and len(self._serves) >= MAX_CONCURRENT_SERVES:
            return False
        self._serves[link_id] = now
        return True

    def _end_serve(self, link_id: bytes) -> None:
        """A request that was refused holds nothing, so its slot goes back."""
        with self._lock:
            self._serves.pop(link_id, None)

    def _serve(self, path, data, request_id, link_id, remote_identity,
               requested_at):
        now = time.time()
        requester = remote_identity.hash.hex() \
            if remote_identity is not None else None
        who = f"{requester[:12]}…" if requester else "an unidentified peer"
        key = bytes(link_id) if link_id is not None else b""

        with self._lock:
            if not self._allow_request(key, now):
                RNS.log(f"TrenchChat [files]: rate limit reached, refusing a "
                        f"request from {who}", RNS.LOG_WARNING)
                return None

        parsed = parse_file_request(data)
        if parsed is None:
            RNS.log(f"TrenchChat [files]: malformed request from {who}",
                    RNS.LOG_WARNING)
            return None
        file_hash_hex, first, count, want_list = parsed

        with self._lock:
            admitted = self._begin_serve(key, now)
        if not admitted:
            RNS.log(f"TrenchChat [files]: already serving "
                    f"{MAX_CONCURRENT_SERVES} downloads, refusing "
                    f"{file_hash_hex[:12]}… to {who}", RNS.LOG_WARNING)
            return None

        payload = self._call_serve(requester, file_hash_hex, first, count,
                                   want_list)
        if payload is None:
            self._end_serve(key)
            RNS.log(f"TrenchChat [files]: refusing {file_hash_hex[:12]}… "
                    f"to {who}", RNS.LOG_WARNING)
            return None
        if len(payload) > MAX_SERVED_RESPONSE_BYTES:
            self._end_serve(key)
            RNS.log(f"TrenchChat [files]: refusing to serve oversized "
                    f"{file_hash_hex[:12]}… ({len(payload)} bytes) to {who}",
                    RNS.LOG_WARNING)
            return None
        return payload
