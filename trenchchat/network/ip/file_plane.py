"""
File plane over a direct session: chunk ranges as REQ/RESP on their own streams.

The same exchange as the RNS plane and the same serve callback: a member asks a
holder for a range of chunks or for a file's chunk-hash list, and the holder
answers with the bytes or refuses. What changes is the path and what it affords.
A request rides a stream of the session that is already up, so there is no dial,
no path request and no link to lose; a range is 256 chunks rather than 16,
because eight megabytes over a punched UDP path costs what half a megabyte costs
over a radio.

What does not change is who may have the bytes. The serve callback is the core
layer's membership check (core/files.py), and the identity handed to it is the
one the session's HELLO proved, so a peer can no more read this plane into
serving it something than it could the other one.

Two bounds hold the serving side: how many ranges one session may have in
flight, and how large an answer may be, both read from the path's own
TransportLimits rather than compiled in here.
"""

import threading
import time

import RNS

from trenchchat.network.base import TransportLimits, direct_limits
from trenchchat.network.file_transport import (
    FETCH_REFUSED, FETCH_STALLED, FILE_FETCH_TIMEOUT_SECS, FileTransportBase,
    R_FILE_HASH, R_FIRST, R_COUNT, R_WANT_LIST, max_response_for,
    parse_file_request,
)
from trenchchat.network.ip.frames import MAX_FRAME_BYTES
from trenchchat.network.link_client import (
    FETCH_LINK_CLOSED, FETCH_TOO_LARGE, FETCH_UNREACHABLE,
)

# The one operation this plane speaks, named on every REQ.
FILE_OP = "file"

# Where the bytes sit in a response.
R_DATA = "d"

# Ranges one session may have in flight here at once. A download issues one at
# a time and waits for it, so this bounds a peer that is not waiting rather
# than a peer that is downloading.
MAX_CONCURRENT_SERVES_PER_SESSION = 4


class IPFileTransport(FileTransportBase):
    """The file plane carried by direct sessions."""

    def __init__(self, transport, limits: TransportLimits | None = None):
        """
        transport: the IPTransport whose sessions carry the requests.
        limits: the path's budgets, for a test that wants narrower ones.
        """
        super().__init__()
        self._transport = transport
        self._limits = limits or direct_limits()
        self._lock = threading.RLock()
        # fetch id -> (peer_hex, request id, deadline)
        self._pending: dict[str, tuple[str, int, float]] = {}
        self._serving = False
        self._serves: dict[str, int] = {}

    @property
    def max_request_chunks(self) -> int:
        """How many chunks one request here may ask for."""
        return self._limits.file_request_max_chunks

    # --- fetching ---

    def fetch_chunks(self, fetch_id: str, holder_hex: str,
                     file_hash_hex: str, first: int, count: int,
                     timeout: float = FILE_FETCH_TIMEOUT_SECS) -> None:
        """Ask a holder for chunks [first, first + count) over its session."""
        file_hash = self._valid_fetch(fetch_id, file_hash_hex, first, count)
        if file_hash is None:
            return
        self._request(fetch_id, holder_hex,
                      {R_FILE_HASH: file_hash, R_FIRST: first, R_COUNT: count},
                      max_response_for(count), timeout)

    def fetch_chunk_list(self, fetch_id: str, holder_hex: str,
                         file_hash_hex: str,
                         timeout: float = FILE_FETCH_TIMEOUT_SECS) -> None:
        """Ask a holder for the concatenated chunk hashes of a file."""
        file_hash = self._valid_fetch(fetch_id, file_hash_hex, 0, 1)
        if file_hash is None:
            return
        self._request(fetch_id, holder_hex,
                      {R_FILE_HASH: file_hash, R_WANT_LIST: 1},
                      MAX_FRAME_BYTES, timeout)

    def _request(self, fetch_id: str, holder_hex: str, payload: dict,
                 max_response: int, timeout: float) -> None:
        """Put one request on the holder's session, or fail the fetch now."""
        request_id = self._transport.send_request(
            holder_hex, FILE_OP, payload,
            lambda ok, body, peer=holder_hex, fid=fetch_id, cap=max_response:
                self._on_response(fid, ok, body, cap),
        )
        if request_id is None:
            self._notify_result(fetch_id, False, None, FETCH_UNREACHABLE)
            return
        with self._lock:
            self._pending[fetch_id] = (holder_hex, request_id,
                                       time.time() + timeout)

    def _on_response(self, fetch_id: str, ok: bool, body: dict,
                     max_response: int) -> None:
        """One answer, checked against what was asked for before it is passed on."""
        with self._lock:
            if self._pending.pop(fetch_id, None) is None:
                return
        if not ok:
            self._notify_result(fetch_id, False, None, FETCH_REFUSED)
            return
        data = body.get(R_DATA)
        if not isinstance(data, (bytes, bytearray)) or not data:
            self._notify_result(fetch_id, False, None, FETCH_REFUSED)
            return
        if len(data) > max_response:
            self._notify_result(fetch_id, False, None, FETCH_TOO_LARGE)
            return
        self._notify_progress(fetch_id, 1.0)
        self._notify_result(fetch_id, True, bytes(data), None)

    def cancel(self, fetch_id: str) -> None:
        """Forget a fetch, so an answer that arrives later is dropped."""
        with self._lock:
            self._pending.pop(fetch_id, None)

    def can_reach(self, holder_hex: str) -> bool:
        """Whether a session with this holder is up."""
        return self._transport.can_reach(holder_hex)

    def drop_link(self, holder_hex: str) -> bool:
        """Fail whatever this holder is serving right now.

        The session itself is left alone: it carries this node's messages as
        well, and a file request that went nowhere is no reason to take those
        down with it.
        """
        with self._lock:
            stranded = [fetch_id for fetch_id, (peer, _id, _at)
                        in self._pending.items() if peer == holder_hex]
            for fetch_id in stranded:
                del self._pending[fetch_id]
        for fetch_id in stranded:
            self._notify_result(fetch_id, False, None, FETCH_LINK_CLOSED)
        return bool(stranded)

    def tick(self) -> None:
        """Fail the requests whose answer never came."""
        now = time.time()
        with self._lock:
            overdue = [fetch_id for fetch_id, (_peer, _id, deadline)
                       in self._pending.items() if deadline <= now]
            for fetch_id in overdue:
                del self._pending[fetch_id]
        for fetch_id in overdue:
            self._notify_result(fetch_id, False, None, FETCH_STALLED)

    # --- serving ---

    def start_serving(self) -> None:
        """Answer file requests arriving on any session."""
        self._serving = True
        self._transport.set_request_handler(FILE_OP, self._serve)

    def stop_serving(self) -> None:
        """Stop answering, leaving the sessions themselves up."""
        self._serving = False
        self._transport.set_request_handler(FILE_OP, None)
        with self._lock:
            self._serves.clear()

    def announce(self) -> None:
        """Nothing to announce: a session is already the path.

        On the mesh an announce is what a path to a holder's file plane is made
        of. Here the path is the session, opened because the two are members of
        the same invite-only channel, and a peer that has one needs nothing
        else to ask over it.
        """

    def _serve(self, peer_hex: str, payload: dict) -> tuple[bool, dict]:
        """Answer one peer's request, or refuse it. Runs on a worker thread."""
        if not self._serving:
            return False, {}
        parsed = parse_file_request(payload, max_chunks=self.max_request_chunks)
        if parsed is None:
            RNS.log(f"TrenchChat [files]: malformed direct request from "
                    f"{peer_hex[:12]}…", RNS.LOG_WARNING)
            return False, {}
        file_hash_hex, first, count, want_list = parsed
        if not self._begin_serve(peer_hex):
            RNS.log(f"TrenchChat [files]: already serving "
                    f"{MAX_CONCURRENT_SERVES_PER_SESSION} ranges to "
                    f"{peer_hex[:12]}…, refusing another", RNS.LOG_WARNING)
            return False, {}
        try:
            data = self._call_serve(peer_hex, file_hash_hex, first, count,
                                    want_list)
        finally:
            self._end_serve(peer_hex)
        if data is None:
            RNS.log(f"TrenchChat [files]: refusing {file_hash_hex[:12]}… to "
                    f"{peer_hex[:12]}…", RNS.LOG_WARNING)
            return False, {}
        ceiling = (MAX_FRAME_BYTES if want_list
                   else max_response_for(self.max_request_chunks))
        if len(data) > ceiling:
            RNS.log(f"TrenchChat [files]: refusing to serve oversized "
                    f"{file_hash_hex[:12]}… ({len(data)} bytes) to "
                    f"{peer_hex[:12]}…", RNS.LOG_WARNING)
            return False, {}
        return True, {R_DATA: data}

    def _begin_serve(self, peer_hex: str) -> bool:
        """Take one of this session's serving slots, if it has one free."""
        with self._lock:
            live = self._serves.get(peer_hex, 0)
            if live >= MAX_CONCURRENT_SERVES_PER_SESSION:
                return False
            self._serves[peer_hex] = live + 1
        return True

    def _end_serve(self, peer_hex: str) -> None:
        """Give a serving slot back."""
        with self._lock:
            live = self._serves.get(peer_hex, 0) - 1
            if live > 0:
                self._serves[peer_hex] = live
            else:
                self._serves.pop(peer_hex, None)
