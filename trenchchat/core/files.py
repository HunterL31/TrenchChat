"""
Shared files: the download engine, holder choice, and serving members.

A file is content addressed. The message carries a manifest and never the
bytes, so a download is this node asking a member who holds the file for
ranges of it and verifying every chunk before it is stored. Nothing is
pushed, and nothing says which files it holds: a holder is found from the
member list and asked, so a miss costs one link handshake instead of every
member a control message. What a holder does announce is that it is one at
all, because that announce is what a path to its file plane is made of.

Requests go out one at a time, one download at a time, because airtime is
shared and two transfers over one slow link finish later than the same two in
sequence. The request window starts at one chunk and doubles on each success,
halving on any failure, which measures the link rather than detecting it.
Progress is chunks verified over chunks total and is persisted as they land,
so a dropped link, a restart or a PIN lock costs nothing already held and the
bar never goes backwards.

Verification is what makes resume from a second holder safe: the chunk-hash
list is checked against the manifest's signed chunk root before anything a
holder says is trusted, every chunk is checked against that list on arrival,
and the assembled file is checked against the manifest hash before it counts
as held. A holder that serves a bad chunk is skipped for this file and the
next holder is asked from the same index.

The serve callback is the layer that holds against a peer calling in
directly: the requester must have identified on the link and must be in the
stored member list of an invite-only channel this file was shared in. A
refusal is silence on the wire and a warning in the log.
"""

import hashlib
import os
import threading
import time

import RNS

from trenchchat.core.fileutils import clean_filename
from trenchchat.core.permissions import is_open_join, permissions_from_json
from trenchchat.core.protocol import (
    FILE_CHUNK_BYTES, chunk_hashes, chunk_root, file_manifest,
)
from trenchchat.network.file_transport import (
    CHUNK_HASH_BYTES, FETCH_REFUSED, FILE_REQUEST_MAX_CHUNKS,
    FileTransportBase,
)

# Holders tried per round before a download parks and waits instead.
MAX_HOLDER_ATTEMPTS = 4
# How long a parked download waits before asking again on its own, doubling
# up to the ceiling and reset by any sign of a member. Hearing a peer is the
# cheap trigger and stays the first one, but it cannot be the only one: a
# transport node damps repeat announces and the liveness beacon informs only
# its receiver, so a download whose one attempt failed could sit parked for
# ever with the holder up the whole time.
DOWNLOAD_RETRY_SECS = 120.0
MAX_DOWNLOAD_RETRY_SECS = 3600.0
CACHE_PRUNE_INTERVAL_SECS = 300.0
# How often a node that holds files says so. It is also the floor between
# announces, so becoming a holder several times over costs one packet, not one
# per file. Shorter than a hosted node's 900s because this announce is what a
# path to the file plane is made of: on a lossy link the one sent when the
# file arrived is the one that goes missing, and until the next one every
# download from this holder fails as unreachable.
FILE_ANNOUNCE_INTERVAL_SECS = 300.0

DL_QUEUED = "queued"
DL_FETCHING = "fetching"
DL_DONE = "done"
DL_UNAVAILABLE = "unavailable"
DL_FAILED = "failed"

REASON_STORAGE = "storage"
REASON_NO_HOLDER = "no_holder"
REASON_REFUSED = "refused"
REASON_CORRUPT = "corrupt"


def build_manifest(name: str, data: bytes) -> dict | None:
    """The manifest for a file about to be shared, or None if it cannot be one.

    The name is cleaned to the shape a receiver accepts, so what the author
    signs is what holds up at the other end, and the two digests are what bind
    the message to bytes nobody has sent yet: the file hash addresses it
    everywhere, the chunk root covers each chunk of it.
    """
    cleaned = clean_filename(name)
    if cleaned is None or not data:
        return None
    return file_manifest(cleaned, len(data), hashlib.sha256(data).digest(),
                         chunk_root(chunk_hashes(data)))


def chunk_count_for(size: int) -> int:
    """How many chunks a file of this size is stored and served as."""
    return (size + FILE_CHUNK_BYTES - 1) // FILE_CHUNK_BYTES


def chunk_size_at(size: int, idx: int) -> int:
    """The length of one chunk of a file, the last one being the short one."""
    count = chunk_count_for(size)
    if idx < count - 1:
        return FILE_CHUNK_BYTES
    return size - (count - 1) * FILE_CHUNK_BYTES


class _Download:
    """One file being pulled, and everything the engine remembers about it."""

    def __init__(self, manifest: dict):
        self.manifest = manifest
        self.file_hash_hex: str = manifest["hash"].hex()
        self.size: int = manifest["size"]
        self.chunk_count: int = chunk_count_for(manifest["size"])
        self.state: str = DL_QUEUED
        self.reason: str | None = None
        self.progress: float = 0.0
        self.holder: str | None = None
        self.message_ids: list[str] = []
        self.channels: set[str] = set()
        self.senders: list[str] = []
        self.held: set[int] = set()
        self.chunk_list: list[bytes] | None = None
        self.window: int = 1
        self.attempts: int = 0
        # Holders passed over for the rest of this round, and for the life of
        # the download: a stall is worth retrying later, bad bytes are not.
        self.skipped: set[str] = set()
        self.suspect: set[str] = set()
        self.refused: set[str] = set()
        self.contributors: set[str] = set()
        self.admitted: bool = False
        self.fetch_id: str | None = None
        self.wants_list: bool = False
        self.next_retry_at: float = 0.0
        self.retry_backoff: float = DOWNLOAD_RETRY_SECS

    def note_progress(self) -> None:
        if self.chunk_count:
            self.progress = max(self.progress,
                                len(self.held) / self.chunk_count)

    def next_index(self) -> int | None:
        for idx in range(self.chunk_count):
            if idx not in self.held:
                return idx
        return None

    def snapshot(self) -> dict:
        return {
            "file_hash": self.file_hash_hex,
            "name": self.manifest["name"],
            "size": self.size,
            "state": self.state,
            "progress": self.progress,
            "reason": self.reason,
            "holder": self.holder,
            "message_ids": list(self.message_ids),
            "channels": sorted(self.channels),
            "chunks_held": len(self.held),
            "chunk_count": self.chunk_count,
        }


class FileManager:
    """Shares files, downloads them from members, and serves what it holds."""

    def __init__(self, identity, storage, presence_mgr,
                 transport: FileTransportBase | None = None):
        self._identity = identity
        self._storage = storage
        self._presence = presence_mgr
        self._transport = transport if transport is not None \
            else FileTransportBase()
        # The base class does no link work, so a node built without a
        # transport neither serves nor fetches; nothing to start or tick.
        self._has_transport = transport is not None
        self._lock = threading.RLock()
        self._downloads: dict[str, _Download] = {}
        self._order: list[str] = []
        self._by_fetch: dict[str, str] = {}
        self._active: str | None = None
        self._last_holder: dict[str, str] = {}
        self._chunk_lists: dict[str, bytes] = {}
        self._callbacks: list = []
        self._last_prune = time.time()
        self._last_announce = 0.0

        self._transport.set_serve_callback(self._serve)
        self._transport.set_result_callback(self._on_result)
        self._transport.set_progress_callback(self._on_progress)
        if self._has_transport:
            self._transport.start_serving()
        self._restore_downloads()

    # --- callbacks ---

    def add_download_callback(self, cb) -> None:
        """cb(file_hash_hex, state, progress, reason, message_ids)"""
        self._callbacks.append(cb)

    def _notify(self, snap: dict) -> None:
        """Caller must not hold the lock."""
        for cb in self._callbacks:
            try:
                cb(snap["file_hash"], snap["state"], snap["progress"],
                   snap["reason"], list(snap["message_ids"]))
            except Exception as e:
                RNS.log(f"TrenchChat [files]: download callback error: {e}",
                        RNS.LOG_ERROR)

    def _notify_all(self, events: list[dict]) -> None:
        for snap in events:
            self._notify(snap)

    # --- sharing ---

    def share(self, channel_hash_hex: str, name: str,
              data: bytes) -> dict | None:
        """Store a file as our own and return the manifest naming it.

        The manifest is what the caller hands to the send action; the bytes
        stay here until a member asks for them. None means the file could not
        be shared: an unusable name or size, or an own store that is full,
        which is refused rather than paid for by evicting somebody else's
        file.
        """
        manifest = build_manifest(name, data)
        if manifest is None:
            RNS.log(f"TrenchChat [files]: refusing to share {name!r} on "
                    f"{channel_hash_hex[:12]}…", RNS.LOG_WARNING)
            return None
        hash_hex = manifest["hash"].hex()
        existing = self._storage.get_file(hash_hex)
        if existing is not None and existing["complete"]:
            self._storage.begin_file(hash_hex, len(data), own=True)
            self._storage.mark_file_complete(hash_hex)
            self._storage.touch_file(hash_hex)
            return manifest
        if not self._storage.admit_file(hash_hex, len(data), own=True):
            RNS.log(f"TrenchChat [files]: the own file store has no room for "
                    f"{manifest['name']} ({len(data)} bytes)", RNS.LOG_WARNING)
            return None
        self._storage.begin_file(hash_hex, len(data), own=True)
        for idx in range(chunk_count_for(len(data))):
            chunk = data[idx * FILE_CHUNK_BYTES:(idx + 1) * FILE_CHUNK_BYTES]
            if not self._storage.put_file_chunk(hash_hex, idx, chunk):
                self._storage.delete_file(hash_hex)
                return None
        self._storage.mark_file_complete(hash_hex)
        self._storage.touch_file(hash_hex)
        with self._lock:
            self._chunk_lists.pop(hash_hex, None)
        self.announce()
        return manifest

    # --- downloads ---

    def request_download(self, channel_hash_hex: str,
                         message_id: str) -> dict | None:
        """Start or join the download of the file a message names.

        None when there is nothing to download: no such message, no manifest
        on it, one that was stripped on arrival, or a channel this node is not
        a subscribed member of.
        """
        row = self._storage.get_message(channel_hash_hex, message_id)
        if row is None or row["file_stripped"] or not row["file_hash"]:
            return None
        if not self._storage.is_subscribed(channel_hash_hex):
            return None
        if not self._storage.is_member(channel_hash_hex,
                                       self._identity.hash_hex):
            return None
        manifest = self._manifest_from_row(row)
        if manifest is None:
            return None
        hash_hex = manifest["hash"].hex()

        held = self._storage.get_file(hash_hex)
        if held is not None and held["complete"]:
            self._storage.touch_file(hash_hex)
        events: list[dict] = []
        with self._lock:
            dl = self._downloads.get(hash_hex)
            if dl is None:
                dl = _Download(manifest)
                self._downloads[hash_hex] = dl
                self._order.append(hash_hex)
            self._attach_message(dl, row)
            if held is not None and held["complete"]:
                dl.held = set(range(dl.chunk_count))
                dl.note_progress()
                if dl.state != DL_DONE:
                    dl.state = DL_DONE
                    dl.reason = None
                    events.append(dl.snapshot())
                snap = dl.snapshot()
            else:
                if dl.state in (DL_UNAVAILABLE, DL_FAILED):
                    dl.state = DL_QUEUED
                    dl.reason = None
                    dl.skipped.clear()
                    dl.attempts = 0
                if not dl.admitted:
                    self._admit(dl, events)
                snap = dl.snapshot()
        self._notify_all(events)
        self._pump()
        return snap

    def download_status(self, file_hash_hex: str) -> dict | None:
        """How a download is doing, or how it ended. None once forgotten."""
        with self._lock:
            dl = self._downloads.get(file_hash_hex)
            return dl.snapshot() if dl is not None else None

    def list_downloads(self) -> list[dict]:
        """Every download this node is tracking, in the order they were asked for."""
        with self._lock:
            return [self._downloads[h].snapshot() for h in self._order
                    if h in self._downloads]

    def file_bytes(self, file_hash_hex: str) -> bytes | None:
        """The whole file, or None while this node does not hold all of it."""
        data = self._storage.get_file_bytes(file_hash_hex)
        if data is not None:
            self._storage.touch_file(file_hash_hex)
        return data

    def on_peer_appeared(self, peer_hex: str) -> None:
        """Re-run the downloads this peer might be able to answer.

        The only retry there is: a download with no reachable holder waits for
        a member to announce rather than for a timer to fire.
        """
        if peer_hex == self._identity.hash_hex:
            return
        events: list[dict] = []
        pruned = False
        with self._lock:
            for hash_hex in list(self._order):
                dl = self._downloads.get(hash_hex)
                if dl is None or dl.state not in (DL_UNAVAILABLE, DL_QUEUED):
                    continue
                if not self._peer_may_hold(dl, peer_hex):
                    continue
                dl.skipped.discard(peer_hex)
                dl.refused.discard(peer_hex)
                dl.attempts = 0
                dl.next_retry_at = 0.0
                dl.retry_backoff = DOWNLOAD_RETRY_SECS
                if dl.state == DL_UNAVAILABLE:
                    dl.state = DL_QUEUED
                    dl.reason = None
                    events.append(dl.snapshot())
                elif dl.reason == REASON_STORAGE and not pruned:
                    pruned = True
                    self._prune_locked(time.time(), events)
        self._notify_all(events)
        self._pump()

    # --- serving ---

    def _serve(self, requester_hex: str | None, file_hash_hex: str,
               first: int, count: int, want_list: bool) -> bytes | None:
        """Answer a member's request for a range of a file, or refuse it.

        The core enforcement layer: it holds whoever calls in, so it checks
        the stored member list rather than anything the request claims. A
        file this node does not hold and a requester with no right to it are
        both answered the same way, so a stranger learns nothing about what
        exists here.
        """
        if requester_hex is None:
            RNS.log(f"TrenchChat [files]: refusing {file_hash_hex[:12]}… to "
                    f"an unidentified peer", RNS.LOG_WARNING)
            return None
        who = requester_hex[:12]
        row = self._storage.get_file(file_hash_hex)
        if row is None or not row["complete"]:
            RNS.log(f"TrenchChat [files]: refusing {file_hash_hex[:12]}… to "
                    f"{who}…: not held here", RNS.LOG_WARNING)
            return None
        if not self._may_serve(file_hash_hex, requester_hex):
            RNS.log(f"TrenchChat [files]: refusing {file_hash_hex[:12]}… to "
                    f"{who}…: not a member of a channel it was shared in",
                    RNS.LOG_WARNING)
            return None
        if want_list:
            payload = self._served_chunk_list(file_hash_hex, row["chunk_count"])
        else:
            payload = self._served_chunks(file_hash_hex, row["chunk_count"],
                                          first, count)
        if payload is None:
            RNS.log(f"TrenchChat [files]: refusing chunks {first}+{count} of "
                    f"{file_hash_hex[:12]}… to {who}…: out of range",
                    RNS.LOG_WARNING)
            return None
        self._storage.touch_file(file_hash_hex)
        return payload

    def _may_serve(self, file_hash_hex: str, requester_hex: str) -> bool:
        """Whether the requester is a member of a channel holding this file."""
        for channel_hash_hex in self._storage.file_channels(file_hash_hex):
            channel = self._storage.get_channel(channel_hash_hex)
            if channel is None:
                continue
            if is_open_join(permissions_from_json(channel["permissions"])):
                continue
            if self._storage.is_member(channel_hash_hex, requester_hex):
                return True
        return False

    def _served_chunk_list(self, file_hash_hex: str,
                           chunk_count: int) -> bytes | None:
        """The concatenated chunk hashes, computed once per file and kept."""
        with self._lock:
            cached = self._chunk_lists.get(file_hash_hex)
        if cached is not None:
            return cached
        chunks = self._storage.get_file_chunks(file_hash_hex, 0, chunk_count)
        if len(chunks) != chunk_count:
            return None
        payload = b"".join(hashlib.sha256(c).digest() for c in chunks)
        with self._lock:
            self._chunk_lists[file_hash_hex] = payload
        return payload

    def _served_chunks(self, file_hash_hex: str, chunk_count: int,
                       first: int, count: int) -> bytes | None:
        if first >= chunk_count:
            return None
        count = min(count, chunk_count - first)
        chunks = self._storage.get_file_chunks(file_hash_hex, first, count)
        if not chunks:
            return None
        return b"".join(chunks)

    # --- housekeeping ---

    def announce(self) -> None:
        """Tell the mesh this node holds files, if it holds any.

        A member cannot ask for a file without a path to the file plane, and
        nothing else on the mesh can supply one: a path request for a
        destination that has never announced dies at the first transport node
        (see network/file_transport.py). So a node says it is a holder the
        moment it becomes one and repeats it on a slow cadence, and a node
        holding nothing stays quiet, because nobody has any reason to dial it.
        """
        if not self._has_transport:
            return
        now = time.time()
        with self._lock:
            if now - self._last_announce < FILE_ANNOUNCE_INTERVAL_SECS:
                return
        usage = self._storage.file_store_usage()
        if not usage["own"] and not usage["received"]:
            return
        with self._lock:
            self._last_announce = now
        self._transport.announce()

    def tick(self, now: float | None = None) -> None:
        if self._has_transport:
            self._transport.tick()
        self.announce()
        now = time.time() if now is None else now
        events: list[dict] = []
        if now - self._last_prune >= CACHE_PRUNE_INTERVAL_SECS:
            self._last_prune = now
            with self._lock:
                self._prune_locked(now, events)
        with self._lock:
            self._retry_parked(now)
        self._notify_all(events)
        self._pump()

    def prune(self, now: float | None = None) -> int:
        """Drop what the store budgets no longer cover, and forget its downloads."""
        events: list[dict] = []
        with self._lock:
            deleted = self._prune_locked(
                time.time() if now is None else now, events)
        self._notify_all(events)
        return deleted

    def stop(self) -> None:
        """Stop serving. Held files stay held; a download resumes on restart."""
        if self._has_transport:
            self._transport.stop_serving()

    # --- internals ---

    def _prune_locked(self, now: float, events: list[dict]) -> int:
        deleted = self._storage.prune_files(now)
        for hash_hex in list(self._order):
            dl = self._downloads.get(hash_hex)
            if dl is None or not dl.admitted:
                continue
            if self._storage.get_file(hash_hex) is not None:
                continue
            self._drop_locked(hash_hex)
            self._chunk_lists.pop(hash_hex, None)
        return deleted

    def _retry_parked(self, now: float) -> None:
        """Caller holds the lock. Re-queue the downloads whose wait is up."""
        for hash_hex in list(self._order):
            dl = self._downloads.get(hash_hex)
            if dl is None or dl.state != DL_UNAVAILABLE:
                continue
            if now < dl.next_retry_at:
                continue
            dl.retry_backoff = min(dl.retry_backoff * 2,
                                   MAX_DOWNLOAD_RETRY_SECS)
            dl.next_retry_at = now + dl.retry_backoff
            dl.skipped.clear()
            dl.attempts = 0
            dl.state = DL_QUEUED
            dl.reason = None

    def _drop_locked(self, hash_hex: str) -> None:
        self._downloads.pop(hash_hex, None)
        if hash_hex in self._order:
            self._order.remove(hash_hex)
        if self._active == hash_hex:
            self._active = None

    def _manifest_from_row(self, row) -> dict | None:
        try:
            file_hash = bytes.fromhex(row["file_hash"])
            root = bytes.fromhex(row["file_chunk_root"] or "")
        except (TypeError, ValueError):
            return None
        return file_manifest(row["file_name"], row["file_size"], file_hash,
                             root)

    def _attach_message(self, dl: _Download, row) -> None:
        """Caller holds the lock. Record a message that names this file."""
        if row["message_id"] not in dl.message_ids:
            dl.message_ids.append(row["message_id"])
        dl.channels.add(row["channel_hash"])
        sender = row["sender_hash"]
        if sender and sender != self._identity.hash_hex \
                and sender not in dl.senders:
            dl.senders.append(sender)

    def _admit(self, dl: _Download, events: list[dict]) -> None:
        """Caller holds the lock. Make room for a download, or leave it queued."""
        if self._storage.admit_file(dl.file_hash_hex, dl.size):
            self._storage.begin_file(dl.file_hash_hex, dl.size)
            dl.admitted = True
            dl.held = set(self._storage.file_chunk_indices(dl.file_hash_hex))
            dl.note_progress()
            if dl.reason == REASON_STORAGE:
                dl.reason = None
                events.append(dl.snapshot())
            return
        if dl.state != DL_QUEUED or dl.reason != REASON_STORAGE:
            dl.state = DL_QUEUED
            dl.reason = REASON_STORAGE
            events.append(dl.snapshot())

    def _restore_downloads(self) -> None:
        """Rebuild the unfinished downloads a previous run left behind.

        Their chunks are already verified and stored, so what is rebuilt is
        the bookkeeping around them. Nothing is fetched until a holder is
        known: they wait for an announce like any other download with nobody
        to ask.
        """
        for row in self._storage.list_files(complete=False, own=False):
            hash_hex = row["hash"]
            messages = self._storage.messages_for_file(hash_hex)
            manifest = None
            for message in messages:
                manifest = self._manifest_from_row(message)
                if manifest is not None:
                    break
            if manifest is None:
                continue
            dl = _Download(manifest)
            dl.admitted = True
            dl.state = DL_UNAVAILABLE
            dl.reason = REASON_NO_HOLDER
            dl.held = set(self._storage.file_chunk_indices(hash_hex))
            dl.note_progress()
            for message in messages:
                self._attach_message(dl, message)
            self._downloads[hash_hex] = dl
            self._order.append(hash_hex)

    def _peer_may_hold(self, dl: _Download, peer_hex: str) -> bool:
        """Caller holds the lock. Whether a peer is in this file's member set."""
        if peer_hex in dl.senders:
            return True
        for channel_hash_hex in self._file_channels(dl):
            if self._storage.is_member(channel_hash_hex, peer_hex):
                return True
        return False

    def _file_channels(self, dl: _Download) -> set[str]:
        return set(self._storage.file_channels(dl.file_hash_hex)) | dl.channels

    def _candidates(self, dl: _Download) -> list[str]:
        """Caller holds the lock. Every peer that might hold this file, in order.

        The peer that last served it, then whoever sent the message, then the
        other members, most recently seen first.
        """
        ordered: list[str] = []
        seen: set[str] = set()

        def add(peer: str | None) -> None:
            if not peer or peer == self._identity.hash_hex or peer in seen:
                return
            seen.add(peer)
            ordered.append(peer)

        add(self._last_holder.get(dl.file_hash_hex))
        for sender in dl.senders:
            add(sender)
        members: list[str] = []
        for channel_hash_hex in self._file_channels(dl):
            for row in self._storage.get_members(channel_hash_hex):
                members.append(row["identity_hash"])
        members.sort(key=self._presence.last_seen_at, reverse=True)
        for member in members:
            add(member)
        return ordered

    def _next_holder(self, dl: _Download) -> str | None:
        """Caller holds the lock. The holder to ask next, or None this round.

        Presence orders the candidates rather than gating them. A member we
        have not heard from lately is not a member we know to be gone: a
        transport node damps repeat announces, and the liveness beacon is
        evidence for whoever receives it and not for whoever sends it, so a
        peer one link away can read as offline here for minutes at a time.
        Asking a quiet member costs one dial that fails and parks the
        download; not asking costs the download.
        """
        if dl.attempts >= MAX_HOLDER_ATTEMPTS:
            return None
        quiet: str | None = None
        for peer in self._candidates(dl):
            if peer in dl.suspect or peer in dl.skipped:
                continue
            if self._presence.is_online(peer):
                return peer
            if quiet is None:
                quiet = peer
        return quiet

    def _all_refused(self, dl: _Download) -> bool:
        """Caller holds the lock. Whether every known holder has said no."""
        candidates = self._candidates(dl)
        if not candidates or not dl.refused:
            return False
        return all(peer in dl.refused or peer in dl.suspect
                   for peer in candidates)

    def _pump(self) -> None:
        """Issue the next request, starting the next download if none is live."""
        events: list[dict] = []
        with self._lock:
            issue = self._prepare(events)
        self._notify_all(events)
        if issue is None:
            return
        fetch_id, holder, hash_hex, first, count, want_list = issue
        if want_list:
            self._transport.fetch_chunk_list(fetch_id, holder, hash_hex)
        else:
            self._transport.fetch_chunks(fetch_id, holder, hash_hex, first,
                                         count)

    def _prepare(self, events: list[dict]) -> tuple | None:
        """Caller holds the lock. The one request to issue now, or None."""
        while True:
            dl = self._downloads.get(self._active) if self._active else None
            if dl is None:
                dl = self._activate_next(events)
            if dl is None:
                return None
            if dl.fetch_id is not None:
                return None
            holder = self._next_holder(dl)
            if holder is None:
                self._stall(dl, events)
                continue
            fetch_id = os.urandom(8).hex()
            dl.fetch_id = fetch_id
            dl.holder = holder
            dl.attempts += 1
            dl.wants_list = dl.chunk_list is None
            self._by_fetch[fetch_id] = dl.file_hash_hex
            if dl.state != DL_FETCHING:
                dl.state = DL_FETCHING
                dl.reason = None
                events.append(dl.snapshot())
            if dl.wants_list:
                return fetch_id, holder, dl.file_hash_hex, 0, 0, True
            first = dl.next_index()
            count = 1
            while (count < dl.window and first + count < dl.chunk_count
                   and (first + count) not in dl.held):
                count += 1
            return fetch_id, holder, dl.file_hash_hex, first, count, False

    def _activate_next(self, events: list[dict]) -> "_Download | None":
        """Caller holds the lock. The next queued download that has room."""
        self._active = None
        for hash_hex in list(self._order):
            dl = self._downloads.get(hash_hex)
            if dl is None or dl.state != DL_QUEUED:
                continue
            if not dl.admitted:
                self._admit(dl, events)
                if not dl.admitted:
                    continue
            self._active = hash_hex
            return dl
        return None

    def _stall(self, dl: _Download, events: list[dict]) -> None:
        """Caller holds the lock. Park a download nobody reachable can answer."""
        dl.holder = None
        if self._all_refused(dl):
            dl.state = DL_FAILED
            dl.reason = REASON_REFUSED
        else:
            dl.state = DL_UNAVAILABLE
            dl.reason = REASON_NO_HOLDER
            # The wait runs from the attempt that just failed, and grows only
            # when one of these waits is actually spent (_retry_parked).
            dl.next_retry_at = time.time() + dl.retry_backoff
        events.append(dl.snapshot())
        self._active = None

    def _finish(self, dl: _Download, state: str, reason: str | None,
                events: list[dict]) -> None:
        """Caller holds the lock. Settle a download and free the in-flight slot."""
        dl.state = state
        dl.reason = reason
        dl.holder = None
        events.append(dl.snapshot())
        if self._active == dl.file_hash_hex:
            self._active = None

    # --- transport callbacks ---

    def _on_progress(self, fetch_id: str, progress: float) -> None:
        """Deliberately ignored: progress is chunks verified, not bytes moving.

        A request that stalls halfway leaves nothing behind, so counting its
        bytes would walk the bar backwards when the next holder starts the
        same chunk again.
        """

    def _on_result(self, fetch_id: str, ok: bool, payload: bytes | None,
                   reason: str | None) -> None:
        events: list[dict] = []
        with self._lock:
            hash_hex = self._by_fetch.pop(fetch_id, None)
            dl = self._downloads.get(hash_hex) if hash_hex else None
            if dl is None or dl.fetch_id != fetch_id:
                return
            dl.fetch_id = None
            holder = dl.holder or ""
            if not ok:
                self._on_failed_request(dl, holder, reason, events)
            elif dl.wants_list:
                self._on_chunk_list(dl, holder, payload, events)
            else:
                self._on_chunks(dl, holder, payload, events)
        self._notify_all(events)
        # A finished download makes this node a holder, and a holder nobody
        # can find a path to is no holder at all.
        self.announce()
        self._pump()

    def _on_failed_request(self, dl: _Download, holder: str,
                           reason: str | None, events: list[dict]) -> None:
        """Caller holds the lock. One request failed, the download did not."""
        dl.window = max(1, dl.window // 2)
        if holder:
            dl.skipped.add(holder)
            if reason == FETCH_REFUSED:
                dl.refused.add(holder)
        RNS.log(f"TrenchChat [files]: {dl.file_hash_hex[:12]}… from "
                f"{holder[:12]}… failed: {reason}", RNS.LOG_WARNING)

    def _on_chunk_list(self, dl: _Download, holder: str,
                       payload: bytes | None, events: list[dict]) -> None:
        """Caller holds the lock. Check a chunk list against the signed root."""
        data = payload or b""
        expected = dl.chunk_count * CHUNK_HASH_BYTES
        hashes = [data[i:i + CHUNK_HASH_BYTES]
                  for i in range(0, len(data), CHUNK_HASH_BYTES)]
        if len(data) != expected or chunk_root(hashes) != \
                dl.manifest["chunk_root"]:
            RNS.log(f"TrenchChat [files]: {holder[:12]}… served a chunk list "
                    f"that does not match the root of "
                    f"{dl.file_hash_hex[:12]}…", RNS.LOG_WARNING)
            dl.suspect.add(holder)
            dl.window = 1
            return
        dl.chunk_list = hashes
        self._round_reset(dl, holder)

    def _on_chunks(self, dl: _Download, holder: str, payload: bytes | None,
                   events: list[dict]) -> None:
        """Caller holds the lock. Verify and store what a holder just served."""
        data = payload or b""
        first = dl.next_index()
        if first is None or dl.chunk_list is None:
            return
        offset = 0
        stored = 0
        idx = first
        while offset < len(data) and idx < dl.chunk_count:
            length = chunk_size_at(dl.size, idx)
            chunk = data[offset:offset + length]
            if len(chunk) != length:
                break
            if hashlib.sha256(chunk).digest() != dl.chunk_list[idx]:
                RNS.log(f"TrenchChat [files]: {holder[:12]}… served a bad "
                        f"chunk {idx} of {dl.file_hash_hex[:12]}…",
                        RNS.LOG_WARNING)
                dl.suspect.add(holder)
                dl.window = 1
                break
            if not self._storage.put_file_chunk(dl.file_hash_hex, idx, chunk):
                self._finish(dl, DL_FAILED, REASON_STORAGE, events)
                return
            dl.held.add(idx)
            stored += 1
            offset += length
            idx += 1
        if stored == 0:
            if holder not in dl.suspect:
                dl.skipped.add(holder)
                dl.window = max(1, dl.window // 2)
            return
        dl.contributors.add(holder)
        dl.note_progress()
        events.append(dl.snapshot())
        if holder not in dl.suspect:
            dl.window = min(dl.window * 2, FILE_REQUEST_MAX_CHUNKS)
            self._round_reset(dl, holder)
        if len(dl.held) >= dl.chunk_count:
            self._complete(dl, events)

    def _round_reset(self, dl: _Download, holder: str) -> None:
        """Caller holds the lock. A holder answered, so the round starts over."""
        dl.attempts = 0
        dl.skipped.clear()
        dl.refused.discard(holder)
        self._last_holder[dl.file_hash_hex] = holder

    def _complete(self, dl: _Download, events: list[dict]) -> None:
        """Caller holds the lock. Check the whole file, then hold it.

        Every chunk already matched the list the signed root covers, so a
        mismatch here means the manifest itself never described these bytes.
        The chunks are dropped, everyone who served one is suspect, and the
        download starts again on a holder that has not been asked yet.
        """
        chunks = self._storage.get_file_chunks(dl.file_hash_hex, 0,
                                               dl.chunk_count)
        data = b"".join(chunks)
        if len(chunks) == dl.chunk_count and \
                hashlib.sha256(data).digest() == dl.manifest["hash"]:
            self._storage.mark_file_complete(dl.file_hash_hex)
            self._storage.touch_file(dl.file_hash_hex)
            self._finish(dl, DL_DONE, None, events)
            return
        RNS.log(f"TrenchChat [files]: {dl.file_hash_hex[:12]}… did not match "
                f"its manifest; starting again", RNS.LOG_WARNING)
        self._storage.delete_file(dl.file_hash_hex)
        dl.suspect |= dl.contributors
        dl.contributors.clear()
        dl.held.clear()
        dl.chunk_list = None
        dl.progress = 0.0
        dl.window = 1
        dl.admitted = False
        if not [p for p in self._candidates(dl) if p not in dl.suspect]:
            self._finish(dl, DL_FAILED, REASON_CORRUPT, events)
            return
        self._storage.begin_file(dl.file_hash_hex, dl.size)
        dl.admitted = True
        dl.state = DL_QUEUED
        dl.reason = None
        events.append(dl.snapshot())
        self._active = None
