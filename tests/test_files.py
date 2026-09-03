"""
FileManager: sharing, the download engine, holder choice and the store.

Two peers share one FakeFileRegistry, so a fetch resolves against the other
peer's real serve callback on a thread, the way a link would. What is checked
here is the engine around that: chunks verified before they are stored,
progress that only ever goes up, a request window that measures the link, a
holder replaced at the request boundary, and a download that survives a
dropped link, a restart and a full store.
"""

import random
import time

from tests.helpers import wait_for, wait_for_member
from trenchchat.core import actions
from trenchchat.core import storage as storage_module
from trenchchat.core.files import (
    DL_DONE, DL_QUEUED, DL_UNAVAILABLE, DOWNLOAD_RETRY_SECS, FileManager,
    REASON_NO_HOLDER, REASON_STORAGE, chunk_count_for, chunk_size_at,
)
from trenchchat.core.permissions import PRESET_PRIVATE, ROLE_MEMBER, ROLE_OWNER
from trenchchat.core.protocol import FILE_CHUNK_BYTES, chunk_hashes
from tests.fake_file_transport import FakeFileTransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def blob(chunks: int, extra: int = 1000, seed: int = 7) -> bytes:
    """A file of *chunks* chunks, the last one short."""
    size = (chunks - 1) * FILE_CHUNK_BYTES + extra
    return random.Random(seed).randbytes(size)


def file_channel(peer_factory, *names):
    """An invite-only channel whose members are all mirrored on each peer.

    Returns (peers, channel_hash). Every peer sees every other as online, so
    holder choice is exercised rather than presence.
    """
    owner = peer_factory(names[0])
    peers = [owner]
    perms = dict(PRESET_PRIVATE)
    ch_hash = owner.channel_mgr.create_channel("files-ch", "", permissions=perms)

    for name in names[1:]:
        member = peer_factory(name)
        peers.append(member)
        owner.invite_mgr.publish_member_list(
            ch_hash, add_members=[member.identity.hash])
        assert wait_for_member(owner.storage, ch_hash, member.identity.hash_hex)

    for peer in peers[1:]:
        peer.storage.upsert_channel(ch_hash, "files-ch", "",
                                    owner.identity.hash_hex, perms, time.time())
        peer.storage.subscribe(ch_hash)
        peer.storage.set_channel_permissions(ch_hash, perms)
        for other in peers:
            peer.storage.upsert_member(
                ch_hash, other.identity.hash_hex, other.name.capitalize(),
                role=ROLE_OWNER if other is owner else ROLE_MEMBER)

    mark_online(peers)
    return peers, ch_hash


def mark_online(peers) -> None:
    """Every peer has heard from every other one just now."""
    for peer in peers:
        for other in peers:
            if other is not peer:
                peer.presence_mgr.record_seen(other.identity.hash_hex)


def share(sender, ch_hash: str, name: str, data: bytes,
          content: str = "here") -> dict:
    result = actions.share_file(sender.file_mgr, sender.storage,
                                sender.subscription_mgr, sender.messaging,
                                ch_hash, sender.identity.hash_hex, name, data,
                                content)
    assert result["shared"] and result["sent"], result
    return result["manifest"]


def wait_for_file_message(peer, ch_hash: str, file_hash_hex: str,
                          timeout: float = 10.0) -> str:
    """The id of the message naming this file, once it has arrived."""
    found: dict = {}

    def seen() -> bool:
        rows = [m for m in peer.storage.get_messages(ch_hash)
                if m["file_hash"] == file_hash_hex]
        if rows:
            found["id"] = rows[0]["message_id"]
        return bool(rows)

    assert wait_for(seen, timeout=timeout), "the file message never arrived"
    return found["id"]


def wait_for_state(peer, file_hash_hex: str, state: str,
                   timeout: float = 20.0) -> dict:
    assert wait_for(
        lambda: (peer.file_mgr.download_status(file_hash_hex) or {}).get(
            "state") == state,
        timeout=timeout,
    ), (f"{file_hash_hex[:12]} never reached {state}: "
        f"{peer.file_mgr.download_status(file_hash_hex)}")
    return peer.file_mgr.download_status(file_hash_hex)


def download(peer, ch_hash: str, file_hash_hex: str, state: str = DL_DONE,
             timeout: float = 20.0) -> dict:
    msg_id = wait_for_file_message(peer, ch_hash, file_hash_hex)
    assert peer.file_mgr.request_download(ch_hash, msg_id) is not None
    return wait_for_state(peer, file_hash_hex, state, timeout=timeout)


def chunk_fetches(registry, requester_hex: str, file_hash_hex: str,
                  since: int = 0):
    """The chunk-range requests one peer made for one file, in order."""
    with registry.lock:
        entries = list(registry.fetch_log[since:])
    return [(holder, first, count)
            for who, holder, file_hash, first, count, want_list in entries
            if who == requester_hex and file_hash == file_hash_hex
            and not want_list]


def list_fetches(registry, requester_hex: str, file_hash_hex: str,
                 since: int = 0):
    with registry.lock:
        entries = list(registry.fetch_log[since:])
    return [holder
            for who, holder, file_hash, _first, _count, want_list in entries
            if who == requester_hex and file_hash == file_hash_hex
            and want_list]


def hostile_serve(data: bytes, *, bad_list: bool = False):
    """A holder that answers with the right shape and the wrong bytes."""
    total = chunk_count_for(len(data))

    def serve(requester_hex, file_hash_hex, first, count, want_list):
        if want_list:
            if bad_list:
                return b"\x00" * (total * 32)
            return b"".join(chunk_hashes(data))
        count = min(count, total - first)
        if count < 1:
            return None
        return b"\x00" * sum(chunk_size_at(len(data), i)
                             for i in range(first, first + count))

    return serve


# ---------------------------------------------------------------------------
# Sharing and a plain download
# ---------------------------------------------------------------------------

def test_a_shared_file_is_held_complete_by_its_sender(peer_factory):
    (alice, _bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    data = blob(2)

    manifest = share(alice, ch_hash, "survey.bin", data)

    row = alice.storage.get_file(manifest["hash"].hex())
    assert row["complete"] and row["own"]
    assert alice.file_mgr.file_bytes(manifest["hash"].hex()) == data


def test_a_member_downloads_the_file_a_message_names(peer_factory):
    (alice, bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    data = blob(3)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()

    seen: list[float] = []
    bob.file_mgr.add_download_callback(
        lambda _h, _s, progress, _r, _m: seen.append(progress))

    status = download(bob, ch_hash, file_hash)

    assert status["progress"] == 1.0
    assert bob.file_mgr.file_bytes(file_hash) == data
    assert seen == sorted(seen), "progress must never go backwards"
    assert bob.storage.get_file(file_hash)["complete"]


def test_a_download_of_an_unknown_message_is_refused(peer_factory):
    (_alice, bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    assert bob.file_mgr.request_download(ch_hash, "no-such-message") is None


def test_a_message_with_no_file_has_nothing_to_download(peer_factory):
    (alice, bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    actions.send_message(alice.storage, alice.subscription_mgr, alice.messaging,
                         ch_hash, alice.identity.hash_hex, "just words")
    assert wait_for(lambda: len(bob.storage.get_messages(ch_hash)) == 1)
    msg_id = bob.storage.get_messages(ch_hash)[0]["message_id"]

    assert bob.file_mgr.request_download(ch_hash, msg_id) is None


def test_a_second_request_joins_the_download_already_running(peer_factory):
    (alice, bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    data = blob(2)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()
    msg_id = wait_for_file_message(bob, ch_hash, file_hash)

    first = bob.file_mgr.request_download(ch_hash, msg_id)
    second = bob.file_mgr.request_download(ch_hash, msg_id)

    assert first["file_hash"] == second["file_hash"]
    assert len(bob.file_mgr.list_downloads()) == 1
    wait_for_state(bob, file_hash, DL_DONE)


# ---------------------------------------------------------------------------
# Holders
# ---------------------------------------------------------------------------

def test_a_third_member_downloads_from_the_second_holder(peer_factory):
    (alice, bob, carol), ch_hash = file_channel(peer_factory, "alice", "bob",
                                                "carol")
    data = blob(3)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()
    download(bob, ch_hash, file_hash)

    carol.file_transport.unreachable.add(alice.identity.hash_hex)
    download(carol, ch_hash, file_hash)

    assert carol.file_mgr.file_bytes(file_hash) == data
    holders = {holder for holder, _f, _c
               in chunk_fetches(carol.file_transport.registry,
                                carol.identity.hash_hex, file_hash)}
    assert bob.identity.hash_hex in holders


def test_a_stalled_holder_is_replaced_at_the_request_boundary(peer_factory):
    (alice, bob, carol), ch_hash = file_channel(peer_factory, "alice", "bob",
                                                "carol")
    data = blob(4)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()
    download(carol, ch_hash, file_hash)

    # Alice answers the chunk list and the first chunk, then stops answering.
    bob.file_transport.stall_chunks.update(
        {(alice.identity.hash_hex, idx) for idx in range(1, 4)})
    download(bob, ch_hash, file_hash)

    assert bob.file_mgr.file_bytes(file_hash) == data
    holders = [holder for holder, _f, _c
               in chunk_fetches(bob.file_transport.registry,
                                bob.identity.hash_hex, file_hash)]
    assert carol.identity.hash_hex in holders, holders


def test_a_download_nobody_can_answer_waits_for_an_announce(peer_factory):
    """A download with nothing to ask parks and waits rather than failing.

    Being unreachable is what settles that, not presence: this used to test
    the same concern with a peer merely recorded offline, which the files
    scenarios showed is a different thing entirely (see
    test_a_quiet_member_is_asked_anyway).
    """
    (alice, bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    data = blob(2)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()
    msg_id = wait_for_file_message(bob, ch_hash, file_hash)
    bob.file_transport.unreachable.add(alice.identity.hash_hex)
    bob.presence_mgr.record_offline(alice.identity.hash_hex)

    bob.file_mgr.request_download(ch_hash, msg_id)
    status = wait_for_state(bob, file_hash, DL_UNAVAILABLE)
    assert status["reason"] == REASON_NO_HOLDER

    bob.file_transport.unreachable.discard(alice.identity.hash_hex)
    bob.presence_mgr.record_seen(alice.identity.hash_hex)
    bob.file_mgr.on_peer_appeared(alice.identity.hash_hex)

    wait_for_state(bob, file_hash, DL_DONE)
    assert bob.file_mgr.file_bytes(file_hash) == data


def test_a_quiet_member_is_asked_anyway(peer_factory):
    """Regression guard for the defect the files scenarios found.

    Presence is evidence of having heard from a peer, not of the peer being
    gone: announces are damped behind a transport node, and the liveness
    beacon only tells whoever receives it. A returning member that beacons a
    holder and is never answered had the holder read as offline for minutes,
    and its download waited for an announce with the bytes one link away.
    """
    (alice, bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    data = blob(2)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()
    msg_id = wait_for_file_message(bob, ch_hash, file_hash)
    bob.presence_mgr.record_offline(alice.identity.hash_hex)

    bob.file_mgr.request_download(ch_hash, msg_id)

    wait_for_state(bob, file_hash, DL_DONE)
    assert bob.file_mgr.file_bytes(file_hash) == data


def test_a_member_with_no_path_is_asked_after_a_holder_with_one(peer_factory):
    """Regression guard for what the files5 radio runs spent their time on.

    A node announces on the files aspect only while it holds something, so a
    member holding nothing has no path to dial, and the dial ladder cannot
    tell that from a holder that is merely slow: asking it spends the whole
    ladder and ends as unreachable. Presence cannot see the difference, and
    here it says the member holding nothing is the livelier of the two.
    """
    (alice, bob, carol), ch_hash = file_channel(peer_factory, "alice", "bob",
                                                "carol")
    data = blob(2)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()
    msg_id = wait_for_file_message(bob, ch_hash, file_hash)

    bob.file_transport.unreachable.add(carol.identity.hash_hex)
    bob.presence_mgr.record_offline(alice.identity.hash_hex)

    registry = bob.file_transport.registry
    with registry.lock:
        since = len(registry.fetch_log)
    bob.file_mgr.request_download(ch_hash, msg_id)
    wait_for_state(bob, file_hash, DL_DONE)

    bob_hex = bob.identity.hash_hex
    assert list_fetches(registry, bob_hex, file_hash, since) == [
        alice.identity.hash_hex]
    asked = {holder for holder, _first, _count
             in chunk_fetches(registry, bob_hex, file_hash, since)}
    assert asked == {alice.identity.hash_hex}, asked


# ---------------------------------------------------------------------------
# Interrupted links, resume and the request window
# ---------------------------------------------------------------------------

def test_a_dropped_link_keeps_the_chunks_already_verified(peer_factory):
    (alice, bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    data = blob(4)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()
    msg_id = wait_for_file_message(bob, ch_hash, file_hash)
    # Slow enough that the link can be dropped while a request rides it.
    bob.file_transport.holder_delays[alice.identity.hash_hex] = 0.4

    bob.file_mgr.request_download(ch_hash, msg_id)
    assert wait_for(
        lambda: (bob.file_mgr.download_status(file_hash) or {})["progress"] > 0,
        timeout=20.0)
    held = bob.file_mgr.download_status(file_hash)["chunks_held"]
    assert bob.file_transport.drop_link(alice.identity.hash_hex)

    wait_for_state(bob, file_hash, DL_UNAVAILABLE)
    assert bob.file_mgr.download_status(file_hash)["chunks_held"] == held

    bob.file_transport.holder_delays.clear()
    bob.file_mgr.on_peer_appeared(alice.identity.hash_hex)
    wait_for_state(bob, file_hash, DL_DONE)

    assert bob.file_mgr.file_bytes(file_hash) == data
    firsts = [first for _h, first, _c
              in chunk_fetches(bob.file_transport.registry,
                               bob.identity.hash_hex, file_hash)]
    assert firsts.count(0) == 1, \
        f"chunk 0 was verified and stored; it must not be asked for twice: {firsts}"


def test_a_restarted_manager_resumes_at_the_same_index(peer_factory):
    (alice, bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    data = blob(4)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()
    msg_id = wait_for_file_message(bob, ch_hash, file_hash)
    bob.file_transport.stall_chunks.add((alice.identity.hash_hex, 1))

    bob.file_mgr.request_download(ch_hash, msg_id)
    wait_for_state(bob, file_hash, DL_UNAVAILABLE)
    held = bob.file_mgr.download_status(file_hash)["chunks_held"]
    assert held == 1
    bob.file_mgr.stop()

    registry = bob.file_transport.registry
    with registry.lock:
        mark = len(registry.fetch_log)
    resumed_transport = FakeFileTransport(bob.identity.hash_hex, registry)
    resumed = FileManager(bob.identity, bob.storage, bob.presence_mgr,
                          transport=resumed_transport)
    try:
        status = resumed.download_status(file_hash)
        assert status["state"] == DL_UNAVAILABLE
        assert status["chunks_held"] == held

        resumed.on_peer_appeared(alice.identity.hash_hex)
        assert wait_for(
            lambda: (resumed.download_status(file_hash) or {})["state"]
            == DL_DONE, timeout=20.0)

        assert resumed.file_bytes(file_hash) == data
        firsts = [first for _h, first, _c
                  in chunk_fetches(registry, bob.identity.hash_hex, file_hash,
                                   since=mark)]
        assert firsts and firsts[0] == held, \
            f"a resumed download starts at the first index it lacks: {firsts}"
    finally:
        resumed.stop()
        resumed_transport.join_threads()


def test_the_request_window_doubles_after_two_successes(peer_factory):
    (alice, bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    data = blob(15)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()

    download(bob, ch_hash, file_hash, timeout=40.0)

    counts = [count for _h, _f, count
              in chunk_fetches(bob.file_transport.registry,
                               bob.identity.hash_hex, file_hash)]
    # Every size is asked for twice before the next one, and the last request
    # asks for the one chunk that is left rather than the window.
    assert counts == [1, 1, 2, 2, 4, 4, 1], counts


def test_the_window_halves_after_a_failed_request(peer_factory):
    (alice, bob, carol), ch_hash = file_channel(peer_factory, "alice", "bob",
                                                "carol")
    data = blob(8)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()
    download(carol, ch_hash, file_hash, timeout=40.0)
    # (0,1) and (1,1) land and the window reaches two, then (2,2) fails: the
    # next request asks for one, from the other holder.
    bob.file_transport.stall_chunks.add((alice.identity.hash_hex, 3))

    download(bob, ch_hash, file_hash, timeout=40.0)

    fetches = chunk_fetches(bob.file_transport.registry,
                            bob.identity.hash_hex, file_hash)
    counts = [count for _h, _f, count in fetches]
    assert counts[:3] == [1, 1, 2], counts
    assert counts[3] == 1, counts
    assert bob.file_mgr.file_bytes(file_hash) == data


def test_a_failure_between_two_successes_does_not_double_the_window(
        peer_factory):
    """The pair the window climbs on has to be consecutive.

    One success either side of a failed request is not evidence that the link
    can carry twice as much, and finding out that it cannot costs a stall
    timeout on a radio. The count starts again, so three ranges of one chunk
    run before the window reaches two.
    """
    (alice, bob, carol), ch_hash = file_channel(peer_factory, "alice", "bob",
                                                "carol")
    data = blob(6)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()
    download(carol, ch_hash, file_hash, timeout=40.0)
    bob.file_transport.stall_chunks.add((alice.identity.hash_hex, 1))

    download(bob, ch_hash, file_hash, timeout=40.0)

    fetches = chunk_fetches(bob.file_transport.registry,
                            bob.identity.hash_hex, file_hash)
    counts = [count for _h, _f, count in fetches]
    assert counts[:5] == [1, 1, 1, 1, 2], counts
    assert fetches[1][0] == alice.identity.hash_hex, fetches
    assert fetches[2][0] == carol.identity.hash_hex, fetches
    assert bob.file_mgr.file_bytes(file_hash) == data


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def test_a_share_past_the_own_budget_is_refused(peer_factory, monkeypatch):
    (alice, _bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    data = blob(2)
    monkeypatch.setattr(storage_module, "OWN_FILE_STORE_MAX_BYTES", 1024)

    assert alice.file_mgr.share(ch_hash, "survey.bin", data) is None
    assert alice.storage.list_files() == []


def test_a_shared_name_is_cleaned_before_it_is_signed(peer_factory):
    """What the author signs is the name a receiver would accept."""
    (alice, _bob), ch_hash = file_channel(peer_factory, "alice", "bob")

    manifest = alice.file_mgr.share(ch_hash, "../../etc/passwd", b"x")

    assert manifest["name"] == "passwd"


def test_an_unshareable_file_is_refused(peer_factory):
    (alice, _bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    assert alice.file_mgr.share(ch_hash, "empty.bin", b"") is None
    assert alice.file_mgr.share(ch_hash, "...", b"x") is None


def test_a_refused_admission_leaves_the_download_queued(peer_factory,
                                                        monkeypatch):
    (alice, bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    data = blob(2)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()
    msg_id = wait_for_file_message(bob, ch_hash, file_hash)
    monkeypatch.setattr(storage_module, "PARTIAL_STORE_MAX_BYTES", 1024)

    status = bob.file_mgr.request_download(ch_hash, msg_id)
    assert status["state"] == DL_QUEUED
    assert status["reason"] == REASON_STORAGE
    assert bob.storage.get_file(file_hash) is None

    monkeypatch.setattr(storage_module, "PARTIAL_STORE_MAX_BYTES",
                        20 * 1024 * 1024)
    bob.file_mgr.tick()

    wait_for_state(bob, file_hash, DL_DONE)
    assert bob.file_mgr.file_bytes(file_hash) == data


def test_prune_drops_an_expired_partial_and_forgets_it(peer_factory):
    (alice, bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    data = blob(3)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()
    msg_id = wait_for_file_message(bob, ch_hash, file_hash)
    bob.file_transport.stall_chunks.add((alice.identity.hash_hex, 1))

    bob.file_mgr.request_download(ch_hash, msg_id)
    wait_for_state(bob, file_hash, DL_UNAVAILABLE)
    assert bob.storage.get_file(file_hash) is not None

    later = time.time() + storage_module.PARTIAL_FILE_TTL_SECS + 10
    assert bob.file_mgr.prune(later) == 1

    assert bob.storage.get_file(file_hash) is None
    assert bob.file_mgr.download_status(file_hash) is None


def test_an_own_file_survives_a_prune(peer_factory):
    (alice, _bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    manifest = share(alice, ch_hash, "survey.bin", blob(2))

    later = time.time() + storage_module.PARTIAL_FILE_TTL_SECS + 10
    alice.file_mgr.prune(later)

    assert alice.storage.get_file(manifest["hash"].hex()) is not None


def test_a_file_already_held_downloads_without_a_request(peer_factory):
    (alice, _bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    data = blob(2)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()
    msg_id = wait_for_file_message(alice, ch_hash, file_hash)

    status = alice.file_mgr.request_download(ch_hash, msg_id)

    assert status["state"] == DL_DONE
    assert not chunk_fetches(alice.file_transport.registry,
                             alice.identity.hash_hex, file_hash)


# ---------------------------------------------------------------------------
# Announcing that this node holds files
# ---------------------------------------------------------------------------

def test_a_node_holding_nothing_says_nothing(peer_factory):
    (alice, _bob), _ch_hash = file_channel(peer_factory, "alice", "bob")

    alice.file_mgr.tick()

    assert alice.file_transport.announces == 0


def test_becoming_a_holder_announces_the_file_plane(peer_factory):
    """Regression guard for the defect the files scenarios found.

    Registering the destination is not enough to be reachable: a path request
    for a destination that has never announced is answered only by a node
    that already knows it, and a transport node in between does not search
    for one, so every fetch failed as unreachable on a mesh with a hop in it.
    A node says it is a holder the moment it becomes one, by sharing or by
    finishing a download.
    """
    (alice, bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    data = blob(2)

    manifest = share(alice, ch_hash, "survey.bin", data)
    assert alice.file_transport.announces == 1

    download(bob, ch_hash, manifest["hash"].hex())
    assert bob.file_transport.announces == 1


def test_a_holder_announces_once_however_many_files_it_takes_on(peer_factory):
    """The floor between announces: airtime is per node, not per file."""
    (alice, _bob), ch_hash = file_channel(peer_factory, "alice", "bob")

    for index in range(3):
        share(alice, ch_hash, f"survey-{index}.bin", blob(2, seed=index))
    alice.file_mgr.tick()

    assert alice.file_transport.announces == 1


def test_a_parked_download_asks_again_without_hearing_anyone(peer_factory):
    """Regression guard for the defect the files scenarios found.

    Hearing a peer used to be the only trigger to try again, and a transport
    node damps repeat announces while the liveness beacon informs only its
    receiver, so a download whose one attempt failed sat parked for the whole
    run with the holder up the entire time. It now asks again on its own,
    after a wait that doubles.
    """
    (alice, bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    data = blob(2)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()
    msg_id = wait_for_file_message(bob, ch_hash, file_hash)
    bob.file_transport.unreachable.add(alice.identity.hash_hex)

    bob.file_mgr.request_download(ch_hash, msg_id)
    wait_for_state(bob, file_hash, DL_UNAVAILABLE)
    bob.file_transport.unreachable.discard(alice.identity.hash_hex)

    bob.file_mgr.tick(now=time.time() + DOWNLOAD_RETRY_SECS + 1.0)

    wait_for_state(bob, file_hash, DL_DONE)
    assert bob.file_mgr.file_bytes(file_hash) == data


def test_a_served_range_puts_the_retry_wait_back_to_the_floor(peer_factory):
    """Regression guard for what the files8 loss runs spent their time on.

    The wait doubles every time one is spent, and hearing a peer is what puts
    it back. A served range says the same thing and used to say it to nobody,
    so a download that lost one request early asked twice in ten minutes on a
    link that was answering in between.
    """
    (alice, bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    data = blob(3)
    manifest = share(alice, ch_hash, "survey.bin", data)
    file_hash = manifest["hash"].hex()
    msg_id = wait_for_file_message(bob, ch_hash, file_hash)
    alice_hex = alice.identity.hash_hex

    bob.file_transport.unreachable.add(alice_hex)
    bob.file_mgr.request_download(ch_hash, msg_id)
    wait_for_state(bob, file_hash, DL_UNAVAILABLE)

    # One wait spent with nothing served doubles it; the range that lands
    # after it is what puts it back to the floor.
    bob.file_transport.unreachable.discard(alice_hex)
    bob.file_transport.stall_chunks.add((alice_hex, 1))
    bob.file_mgr.tick(now=time.time() + DOWNLOAD_RETRY_SECS + 1.0)
    assert wait_for(
        lambda: (bob.file_mgr.download_status(file_hash)["state"]
                 == DL_UNAVAILABLE
                 and bob.file_mgr.download_status(file_hash)["chunks_held"]
                 == 1),
        timeout=10.0), bob.file_mgr.download_status(file_hash)

    bob.file_transport.stall_chunks.clear()
    bob.file_mgr.tick(now=time.time() + DOWNLOAD_RETRY_SECS + 1.0)

    wait_for_state(bob, file_hash, DL_DONE)
    assert bob.file_mgr.file_bytes(file_hash) == data


def test_a_parked_download_waits_out_its_backoff(peer_factory):
    """The other half: the retry is a slow drip, not a poll."""
    (alice, bob), ch_hash = file_channel(peer_factory, "alice", "bob")
    manifest = share(alice, ch_hash, "survey.bin", blob(2))
    file_hash = manifest["hash"].hex()
    msg_id = wait_for_file_message(bob, ch_hash, file_hash)
    bob.file_transport.unreachable.add(alice.identity.hash_hex)

    bob.file_mgr.request_download(ch_hash, msg_id)
    wait_for_state(bob, file_hash, DL_UNAVAILABLE)
    bob.file_transport.unreachable.discard(alice.identity.hash_hex)

    bob.file_mgr.tick()

    assert wait_for(
        lambda: bob.file_mgr.download_status(file_hash)["state"] == DL_DONE,
        timeout=2.0) is False
