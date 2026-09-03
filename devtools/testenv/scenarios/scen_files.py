"""
Family files -- shared files in invite-only channels.

A file message carries a manifest and never the bytes. The bytes move only
when a member asks for them, over the file plane's own RNS Links on the
"files" aspect, chunk range by chunk range, each one verified against the
signed chunk root before it is stored. That is three things pytest's
FakeFileTransport cannot reach: path resolution to a destination nothing
ever announces, a holder that dies with a transfer in flight, and what a
window that doubles on success and halves on failure actually costs on a
radio.

The property the whole design rests on is check 1: nobody's absence breaks a
download once one other member holds the file. files2, files3 and files6
each take the sender away and watch the download finish anyway.

See docs/testenv-scenarios.md for the matrix these implement.
"""

import hashlib
import os
import random
from pathlib import Path

from asserts import hold_for, settle, wait_until, ScenarioFailure
from flows import (
    invite_only_channel, go_offline, go_online, set_link_profile,
    BACKFILL_TIMEOUT, CUSTOM, DISCOVERY_TIMEOUT, LORA_FAST, LOSSY,
    NEGATIVE_HOLD_SECS,
)
from scenario import PROBE, scenario

SIZE_2MB = 2 * 1024 * 1024
SIZE_512KB = 512 * 1024
SIZE_200KB = 200 * 1024
SIZE_20KB = 20 * 1024

# A download is a sequence of link requests, so it is slower than a message
# by more than its size: a dial, an identify, a chunk-hash list, then the
# ranges themselves.
FETCH_TIMEOUT = 240.0
# One 200 KB file is 298s of pure airtime at SF7 before any overhead, and
# measured at twice that when nothing stalls.
LORA_FETCH_TIMEOUT = 1200.0
LOSSY_FETCH_TIMEOUT = 600.0

# A probe is a bare request with no download behind it. Its own timeout only
# bounds the dial: RNS answers a refusal with nothing at all, and a request
# packet that is proven and never answered has no failure callback, so what
# ends a refused request is the plane's own stall sweep at FILE_STALL_SECS.
PROBE_REQUEST_TIMEOUT = 45.0
PROBE_WAIT = 240.0

CHUNK_BYTES = 64 * 1024
CHUNK_HASH_BYTES = 32

# Slow enough to interrupt a transfer on purpose rather than by luck, and
# fast enough that the link is not what the scenario ends up measuring: a
# 2 MB file takes about half a minute. Shaped on bandwidth alone, because a
# request-and-response transfer over a long round trip measures the round
# trip instead (satellite's 800 ms managed 1.9 KB/s here, and timed out).
SLOW_HOLDER = {"bitrate_bps": 512_000, "latency_ms": 10.0, "jitter_ms": 2.0,
               "loss_pct": 0.0}
# Chunks a download must have verified before it is worth interrupting: past
# the first window, so what survives the interruption is several requests'
# work rather than one.
PARTIAL_FLOOR = 4

# The harness announces every 10s, which is 1000x a real client and, at SF7,
# a large fraction of the link: four testers announcing three destinations
# each costs more airtime than the file does. A radio measurement has to be
# about the transfer, so files5 slows every tester down to something closer
# to a real cadence and puts the default back afterwards.
QUIET_HEARTBEAT_SECS = 60.0
DEFAULT_HEARTBEAT_SECS = 10.0


def _payload(size: int, seed: int) -> bytes:
    """Deterministic, incompressible bytes, so the wire carries the whole size."""
    return random.Random(seed).randbytes(size)


def _chunks(size: int) -> int:
    return (size + CHUNK_BYTES - 1) // CHUNK_BYTES


def _share(sender, channel_hash: str, name: str, data: bytes,
           content: str) -> tuple[str, str]:
    """Share a file and return (message_id, file_hash)."""
    result = sender.share_file(channel_hash, name, data, content)
    if not result.get("ok"):
        raise ScenarioFailure(f"{sender.tag} could not share {name}: {result}")
    return result["message_id"], result["file_hash"]


def _await_manifest(peer, channel_hash: str, content: str, message_id: str,
                    timeout: float = DISCOVERY_TIMEOUT) -> float:
    """Wait for the message naming a file, and check it arrived as a manifest."""
    elapsed = wait_until(
        lambda: peer.message_by_content(channel_hash, content) is not None,
        f"{peer.tag} to receive the file message", timeout)
    row = peer.message_by_content(channel_hash, content)
    if row["message_id"] != message_id:
        raise ScenarioFailure(
            f"{peer.tag} holds {row['message_id']} for {content!r}, "
            f"not the sender's {message_id}")
    if row.get("file_stripped"):
        raise ScenarioFailure(f"{peer.tag} stripped the manifest off {content!r}")
    if row.get("file") is None:
        raise ScenarioFailure(f"{peer.tag} received {content!r} with no file card")
    return elapsed


def _status(peer, channel_hash: str, file_hash: str) -> dict:
    return peer.file_fetch_status(channel_hash, file_hash) or {}


def _await_done(peer, channel_hash: str, file_hash: str,
                timeout: float = FETCH_TIMEOUT) -> float:
    """Wait for a download to finish, reporting where it stopped if it does not."""
    try:
        return wait_until(
            lambda: _status(peer, channel_hash, file_hash).get("state") == "done",
            f"{peer.tag} to finish downloading {file_hash[:12]}", timeout)
    except ScenarioFailure as e:
        raise ScenarioFailure(
            f"{e} | {peer.tag} ({peer.hash}) last status: "
            f"{_status(peer, channel_hash, file_hash)}")


def _download(peer, channel_hash: str, file_hash: str, message_id: str,
              timeout: float = FETCH_TIMEOUT) -> float:
    started = peer.start_file_fetch(channel_hash, file_hash, message_id)
    if not started.get("ok"):
        raise ScenarioFailure(f"{peer.tag} was refused the download: {started}")
    return _await_done(peer, channel_hash, file_hash, timeout)


def _verify(peer, channel_hash: str, file_hash: str, data: bytes) -> None:
    """The bytes this peer serves back are the bytes that were shared."""
    got = peer.file_bytes(channel_hash, file_hash)
    if got is None:
        raise ScenarioFailure(f"{peer.tag} reports done but serves no bytes")
    if hashlib.sha256(got).digest() != hashlib.sha256(data).digest():
        raise ScenarioFailure(
            f"{peer.tag} holds {len(got)} bytes that do not match the "
            f"{len(data)} shared")


def _await_probe(peer, probe_id: str, timeout: float = PROBE_WAIT) -> dict:
    """Wait for a bare file-plane request to settle. Reading it ticks it."""
    elapsed = wait_until(lambda: peer.probe_result(probe_id).get("done"),
                         f"{peer.tag}'s file-plane probe to settle", timeout)
    return {**peer.probe_result(probe_id), "secs": round(elapsed, 1)}


class _LogTail:
    """Whatever the testers have logged since this was made.

    A refusal is silence on the wire by design, so the holder's log is the
    only place it exists. Only available when the run captured one
    (--tester-log); scenarios record that they could not look rather than
    asserting on nothing.
    """

    def __init__(self, path: Path):
        self._path = path
        self._offset = path.stat().st_size

    def _text(self) -> str:
        with self._path.open("r", errors="replace") as fh:
            fh.seek(self._offset)
            return fh.read()

    def lines_with(self, *needles: str) -> list[str]:
        return [line for line in self._text().splitlines()
                if all(needle in line for needle in needles)]

    def count(self, needle: str) -> int:
        return self._text().count(needle)


def _log_tail() -> _LogTail | None:
    path = os.environ.get("TC_TESTER_LOG")
    if not path or not Path(path).exists():
        return None
    return _LogTail(Path(path))


@scenario("files1", "A 2 MB file reaches both members byte for byte", peers="ABC")
def h1(env):
    """The honest path, and the one every other row here is a variation of.

    Both members ask at once, which is also the concurrent-serve cap exactly
    filled: a third asker would be refused and would come back later.
    """
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "h1-private")

    data = _payload(SIZE_2MB, 1)
    message_id, file_hash = _share(a, ch, "h1-2mb.bin", data, "h1-file")
    if a.file_state(ch, message_id) != "done":
        raise ScenarioFailure(
            f"the sender does not hold its own share: "
            f"{a.file_card(ch, message_id)}")

    for peer in (b, c):
        _await_manifest(peer, ch, "h1-file", message_id)
        state = peer.file_state(ch, message_id)
        if state != "available":
            raise ScenarioFailure(
                f"{peer.tag} reads {state!r} before asking for anything")
        if peer.file_bytes_status(ch, file_hash) != 404:
            raise ScenarioFailure(f"{peer.tag} holds bytes it never asked for")
        started = peer.start_file_fetch(ch, file_hash, message_id)
        if not started.get("ok"):
            raise ScenarioFailure(f"{peer.tag} was refused the download: {started}")

    secs = {}
    for peer in (b, c):
        secs[peer.tag] = round(_await_done(peer, ch, file_hash), 1)
        _verify(peer, ch, file_hash, data)
    return {"file_bytes": len(data), "chunks": _chunks(len(data)),
            "fetch_secs": secs}


@scenario("files2", "A dead sender's file still downloads from another member",
          peers="ABC")
def h2(env):
    """Check 1, on the file plane: whose absence breaks a download?

    C is away for the share and A's process is gone before C returns, so
    both halves have to come from B: the manifest by sync, then the bytes by
    a link to a member that was never the author.
    """
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "h2-private")

    go_offline(c)
    data = _payload(SIZE_2MB, 2)
    message_id, file_hash = _share(a, ch, "h2-2mb.bin", data, "h2-file")
    _await_manifest(b, ch, "h2-file", message_id)
    b_secs = _download(b, ch, file_hash, message_id)
    _verify(b, ch, file_hash, data)

    env.orch.kill(a.tag)
    wait_until(lambda: not a.alive(), "A's process to go away", 60.0)
    go_online(c)

    manifest_secs = _await_manifest(c, ch, "h2-file", message_id,
                                    BACKFILL_TIMEOUT)
    if c.file_state(ch, message_id) != "available":
        raise ScenarioFailure(
            f"C's backfilled manifest reads "
            f"{c.file_card(ch, message_id)} rather than an unasked-for file")
    c_secs = _download(c, ch, file_hash, message_id)
    _verify(c, ch, file_hash, data)
    return {"file_bytes": len(data), "b_fetch_secs": round(b_secs, 1),
            "manifest_by_sync_secs": round(manifest_secs, 1),
            "c_fetch_secs": round(c_secs, 1)}


@scenario("files3", "A small file still crosses as a manifest and nothing else",
          peers="ABC")
def h3(env):
    """20 KB is small enough that pushing it would have been cheap.

    It is not pushed: C backfills the message with no bytes behind it, holds
    none while it sits there, and only then does asking produce a download.
    An inline tier for small files was the rejected alternative this row is
    the guard for.
    """
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "h3-private")

    go_offline(c)
    data = _payload(SIZE_20KB, 3)
    message_id, file_hash = _share(a, ch, "h3-20kb.bin", data, "h3-file")
    _await_manifest(b, ch, "h3-file", message_id)
    _download(b, ch, file_hash, message_id)
    _verify(b, ch, file_hash, data)

    env.orch.kill(a.tag)
    wait_until(lambda: not a.alive(), "A's process to go away", 60.0)
    go_online(c)

    manifest_secs = _await_manifest(c, ch, "h3-file", message_id,
                                    BACKFILL_TIMEOUT)
    usage = c.file_usage()["usage"]
    if any(usage.values()):
        raise ScenarioFailure(f"C's file store is not empty after a sync: {usage}")
    hold_for(
        lambda: (c.file_bytes_status(ch, file_hash) == 404
                 and c.file_state(ch, message_id) == "available"),
        "C to hold no bytes of a file it has not asked for",
        NEGATIVE_HOLD_SECS,
    )

    c_secs = _download(c, ch, file_hash, message_id)
    _verify(c, ch, file_hash, data)
    held = c.file_usage()["usage"]
    if held["received"] != len(data):
        raise ScenarioFailure(
            f"C holds {held} after downloading {len(data)} bytes")
    return {"file_bytes": len(data),
            "manifest_by_sync_secs": round(manifest_secs, 1),
            "c_fetch_secs": round(c_secs, 1),
            "bytes_held_before_asking": 0}


@scenario("files4", "A non-member gets nothing from the file plane", peers="ABD")
def h4(env):
    """The core enforcement layer, reached the only way a client cannot.

    D is in no channel this file was shared in and asks A's file plane for it
    directly, with the real hash. B, who is a member, makes the same request,
    because a refusal only means something if the identical request works for
    somebody entitled to it.
    """
    a, b, d = env.peers("A", "B", "D")
    ch = invite_only_channel(a, [b], "h4-private")

    data = _payload(SIZE_20KB, 4)
    message_id, file_hash = _share(a, ch, "h4-20kb.bin", data, "h4-file")
    _await_manifest(b, ch, "h4-file", message_id)

    tail = _log_tail()
    member = _await_probe(b, b.probe_file(a.hash, file_hash, want_list=True,
                                          timeout=PROBE_REQUEST_TIMEOUT))
    if not member["ok"] or member["bytes"] != _chunks(len(data)) * CHUNK_HASH_BYTES:
        raise ScenarioFailure(
            f"a member's own request for the chunk list was not served: {member}")

    outsider = {}
    for label, kwargs in (("chunk_list", {"want_list": True}),
                          ("chunk_range", {"first": 0, "count": 1})):
        result = _await_probe(
            d, d.probe_file(a.hash, file_hash,
                            timeout=PROBE_REQUEST_TIMEOUT, **kwargs))
        outsider[label] = f"{result['reason']} after {result['secs']}s"
        if result["ok"] or result["bytes"]:
            raise ScenarioFailure(
                f"a non-member was served {result} for {label}")
    if d.file_bytes_status(ch, file_hash) != 404:
        raise ScenarioFailure("a non-member ended up holding the file")

    notes = {"member_secs": member["secs"],
             "member_list_bytes": member["bytes"], "outsider": outsider}
    if tail is None:
        notes["refusal_logged"] = "no tester log captured"
        return notes
    logged, _ = settle(lambda: bool(tail.lines_with("[files]", "refusing",
                                                    d.hash[:12])),
                       "A's log to name the peer it refused", 60.0)
    notes["refusal_logged"] = logged
    if not logged:
        raise ScenarioFailure(
            f"nothing in the log names D ({d.hash}) as refused; a refusal "
            f"nobody can see is indistinguishable from a lost packet")
    return notes


@scenario("files5", "A 200 KB file over a LoRa SF7 link", peers="ABC",
          kind=PROBE)
def h5(env):
    """The run the chunk size and the ceilings are read against.

    A probe because it measures a link rather than claiming a behaviour, and
    because the measurement is not one number: the same transfer takes about
    eleven minutes when nothing stalls and does not finish inside twenty when
    something does. Both outcomes are the result; neither is a regression.

    Only B downloads: two members pulling the same file share the holder's
    one uplink, so a second downloader doubles the airtime and measures the
    same thing twice. The channel is built before the link is shaped, so what
    is measured is the transfer and not the membership handshake, and the
    testers are slowed to a real announce cadence first, because at SF7 the
    harness's own announces are otherwise most of what the link carries.
    """
    a, b, c = env.peers("A", "B", "C")
    for peer in (a, b, c):
        env.orch.set_heartbeat(peer.tag, QUIET_HEARTBEAT_SECS)
        env.wait_alive(peer)
    try:
        return _lora_transfer(env, a, b, c)
    finally:
        for peer in (a, b, c):
            env.orch.set_heartbeat(peer.tag, DEFAULT_HEARTBEAT_SECS)
            env.wait_alive(peer)


def _lora_transfer(env, a, b, c) -> dict:
    ch = invite_only_channel(a, [b, c], "h5-private")

    shaping = {p.tag: set_link_profile(env, p, LORA_FAST) for p in (a, b, c)}
    data = _payload(SIZE_200KB, 5)
    tail = _log_tail()
    message_id, file_hash = _share(a, ch, "h5-200kb.bin", data, "h5-file")
    manifest_secs = _await_manifest(b, ch, "h5-file", message_id,
                                    LORA_FETCH_TIMEOUT)

    started = b.start_file_fetch(ch, file_hash, message_id)
    if not started.get("ok"):
        raise ScenarioFailure(f"B was refused the download: {started}")
    arrived, secs = settle(
        lambda: _status(b, ch, file_hash).get("state") == "done",
        "B to finish the file over a radio", LORA_FETCH_TIMEOUT,
    )

    status = _status(b, ch, file_hash)
    notes = {
        "shaping": shaping,
        "heartbeat_secs": QUIET_HEARTBEAT_SECS,
        "file_bytes": len(data),
        "chunks": _chunks(len(data)),
        "manifest_secs": round(manifest_secs, 1),
        "arrived": arrived,
        "fetch_secs": round(secs, 1),
        "bytes_per_sec": round(len(data) / secs, 1) if arrived and secs else None,
        "chunks_held": status.get("chunks_held"),
    }
    if tail is not None:
        notes["requests_served"] = tail.count("for: /tc/file")
        notes["requests_failed"] = len(tail.lines_with("[files]", "failed:"))
    if not arrived:
        notes["surprise"] = (
            f"200 KB did not finish over SF7 in {LORA_FETCH_TIMEOUT:.0f}s, "
            f"stopping at {status.get('chunks_held')} of "
            f"{status.get('chunk_count')} chunks")
        return notes
    _verify(b, ch, file_hash, data)
    return notes


@scenario("files6", "A holder that dies mid-transfer is replaced, keeping every "
                    "verified chunk", peers="ABC")
def h6(env):
    """Resume from a second holder, at the request boundary rather than from zero.

    C downloads first, so the file has two holders when A dies with a request
    in flight. What B loses is that one request; what it keeps is every chunk
    already verified, which is why the progress bar never walks backwards.
    A's link is shaped so the transfer is long enough to interrupt on purpose
    rather than by luck.

    The claim is that the rest of the file arrives with the sender dead, and
    that is what is asserted: which holder served it is a note, because a
    download names the holder it is asking at that instant and a poll can
    miss the switch entirely on a fast link.
    """
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "h6-private")
    shaping = set_link_profile(env, a, CUSTOM, **SLOW_HOLDER)

    data = _payload(SIZE_2MB, 6)
    total = _chunks(len(data))
    message_id, file_hash = _share(a, ch, "h6-2mb.bin", data, "h6-file")

    _await_manifest(c, ch, "h6-file", message_id)
    _download(c, ch, file_hash, message_id)
    _verify(c, ch, file_hash, data)

    _await_manifest(b, ch, "h6-file", message_id)
    started = b.start_file_fetch(ch, file_hash, message_id)
    if not started.get("ok"):
        raise ScenarioFailure(f"B was refused the download: {started}")
    wait_until(
        lambda: PARTIAL_FLOOR <= _status(b, ch, file_hash).get("chunks_held", 0)
        < total, "B to be part way through the file", FETCH_TIMEOUT,
        interval=0.2)
    held_at_kill = _status(b, ch, file_hash)["chunks_held"]

    env.orch.kill(a.tag)
    wait_until(lambda: not a.alive(), "A's process to go away", 60.0)

    samples: list[tuple[int, str | None]] = []

    def _finished() -> bool:
        status = _status(b, ch, file_hash)
        samples.append((status.get("chunks_held", 0), status.get("holder")))
        return status.get("state") == "done"

    secs = wait_until(_finished, "B to finish from the remaining holder",
                      FETCH_TIMEOUT, interval=0.5)
    _verify(b, ch, file_hash, data)

    held = [count for count, _ in samples]
    dropped = [(held[i], held[i + 1]) for i in range(len(held) - 1)
               if held[i + 1] < held[i]]
    holders = {holder for _, holder in samples if holder}
    notes = {
        "shaping": shaping,
        "chunks": total,
        "chunks_held_at_kill": held_at_kill,
        "chunks_after_the_sender_died": total - held_at_kill,
        "finish_secs": round(secs, 1),
        # A note rather than an assertion: the holder a download names is
        # whichever one it is asking right now, so the switch is only visible
        # to a poll that lands inside it.
        "holders_seen_after_kill": sorted(
            {"C" if h == c.hash else h[:8] for h in holders}),
    }
    if dropped:
        raise ScenarioFailure(
            f"B's verified chunk count went backwards {dropped}, so work "
            f"already paid for was re-fetched: {notes}")
    if held_at_kill and min(held) < held_at_kill:
        raise ScenarioFailure(
            f"B resumed below the {held_at_kill} chunks it held when the "
            f"sender died: {notes}")
    if held_at_kill >= total:
        raise ScenarioFailure(
            f"B had the whole file before the sender died, so nothing was "
            f"served by the second holder: {notes}")
    if a.alive():
        raise ScenarioFailure("A came back to life before B finished")
    return notes


@scenario("files7", "A restarted downloader resumes from its stored chunks",
          peers="AB")
def h7(env):
    """A process kill mid-download, which is the case a link drop cannot make.

    Verified chunks are rows, not memory, so what a restart costs is the
    request that was in flight. Nothing re-asks: the rebuilt download waits
    for a holder to announce, the same trigger every other catch-up path uses.
    """
    a, b = env.peers("A", "B")
    ch = invite_only_channel(a, [b], "h7-private")
    shaping = set_link_profile(env, a, CUSTOM, **SLOW_HOLDER)

    data = _payload(SIZE_2MB, 7)
    total = _chunks(len(data))
    message_id, file_hash = _share(a, ch, "h7-2mb.bin", data, "h7-file")
    _await_manifest(b, ch, "h7-file", message_id)

    started = b.start_file_fetch(ch, file_hash, message_id)
    if not started.get("ok"):
        raise ScenarioFailure(f"B was refused the download: {started}")
    wait_until(
        lambda: PARTIAL_FLOOR <= _status(b, ch, file_hash).get("chunks_held", 0)
        < total, "B to be part way through the file", FETCH_TIMEOUT,
        interval=0.2)
    held_before = _status(b, ch, file_hash)["chunks_held"]

    env.orch.kill(b.tag)
    wait_until(lambda: not b.alive(), "B's process to die", 60.0)
    env.orch.start(b.tag)
    env.wait_alive(b)

    restored = _status(b, ch, file_hash)
    if restored.get("chunks_held", 0) < held_before:
        raise ScenarioFailure(
            f"B came back holding {restored.get('chunks_held')} of the "
            f"{held_before} chunks it had verified: {restored}")
    resume_secs = wait_until(
        lambda: _status(b, ch, file_hash).get("chunks_held", 0) > held_before,
        "B to resume without being asked again", FETCH_TIMEOUT)
    secs = _await_done(b, ch, file_hash)
    _verify(b, ch, file_hash, data)
    return {"shaping": shaping, "chunks": total,
            "chunks_held_before_kill": held_before,
            "chunks_held_after_restart": restored.get("chunks_held"),
            "resume_secs": round(resume_secs, 1),
            "finish_secs": round(secs, 1)}


@scenario("files8", "A file downloaded over a 15% loss link", peers="AB",
          kind=PROBE)
def h8(env):
    """What the window halving costs when the link drops one frame in seven.

    RNS's own part retries are what carry a transfer through loss; the chunk
    scheme only decides how much work one dead request throws away. This
    records both, so the two are not confused for each other. 512 KB rather
    than files1's 2 MB: at 62.5 kbps the larger file measures the shaper.
    """
    a, b = env.peers("A", "B")
    ch = invite_only_channel(a, [b], "h8-private")
    shaping = set_link_profile(env, a, LOSSY)

    data = _payload(SIZE_512KB, 8)
    tail = _log_tail()
    message_id, file_hash = _share(a, ch, "h8-512kb.bin", data, "h8-file")
    _await_manifest(b, ch, "h8-file", message_id, LOSSY_FETCH_TIMEOUT)

    started = b.start_file_fetch(ch, file_hash, message_id)
    if not started.get("ok"):
        raise ScenarioFailure(f"B was refused the download: {started}")
    arrived, secs = settle(
        lambda: _status(b, ch, file_hash).get("state") == "done",
        "B to finish over a lossy link", LOSSY_FETCH_TIMEOUT,
    )

    notes = {
        "shaping": shaping,
        "file_bytes": len(data),
        "chunks": _chunks(len(data)),
        "arrived": arrived,
        "fetch_secs": round(secs, 1),
    }
    if tail is not None:
        notes["requests_served"] = tail.count("for: /tc/file")
        notes["requests_failed"] = len(tail.lines_with("[files]", "failed:"))
    if not arrived:
        notes["surprise"] = (
            f"a 512 KB file did not arrive over a 15% loss link in "
            f"{LOSSY_FETCH_TIMEOUT:.0f}s: {_status(b, ch, file_hash)}")
        return notes
    _verify(b, ch, file_hash, data)
    return notes
