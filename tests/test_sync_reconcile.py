"""
Integration tests for range-based sync reconciliation.

A watermark says "since when"; these tests are about the cases where the
answer that matters is "which". Two peers that both wrote through a partition
each hold rows behind the other's watermark, and only a description of the
sets themselves closes that gap.
"""

import time

import pytest

from tests.helpers import sign_as, wait_for, wait_for_member, wait_for_message
from trenchchat.core import sync_ranges
from trenchchat.core.messaging import _compute_message_id
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MSG_TYPE, F_SYNC_MESSAGES, F_SYNC_NEED, F_SYNC_RANGES,
    F_SYNC_WINDOW_START, MT_SYNC_REQUEST, MT_SYNC_RESPONSE,
    RANGE_FINGERPRINT, RANGE_IDLIST, message_id_from_wire, unpack_wire,
)
from trenchchat.core.sync import MAX_RESPONSE_MESSAGES, SYNC_WINDOW_SECS
from trenchchat.core.sync_status import SyncState


def _seed_channel_on_peer(peer, ch_hash, channel_name, creator_hash,
                          access_mode="public"):
    """Give a peer knowledge of a channel and subscribe them to it."""
    peer.storage.upsert_channel(ch_hash, channel_name, "", creator_hash,
                                access_mode, time.time())
    peer.storage.subscribe(ch_hash)


def _insert_message(storage, ch_hash, sender_hex, content, ts):
    """Insert a message directly into storage and return its message_id.

    Content, sender and timestamp decide the id, so inserting the same three
    on two peers models the same message being held by both.
    """
    msg_id = _compute_message_id(content, sender_hex, ts)
    storage.insert_message(
        channel_hash=ch_hash,
        sender_hash=sender_hex,
        sender_name="Test",
        content=content,
        timestamp=ts,
        message_id=msg_id,
        reply_to=None,
        last_seen_id=None,
        received_at=ts,
        author_sig=sign_as(sender_hex, ch_hash, msg_id, ts, content),
    )
    return msg_id


def _capture(peer):
    """Hold everything a peer sends instead of delivering it."""
    sent = []
    peer.sync_mgr._send_raw = lambda dest_hex, fields: sent.append(fields) or True
    return sent


def _served_ids(fields) -> set[str]:
    return {message_id_from_wire(m["message_id"])
            for m in unpack_wire(fields[F_SYNC_MESSAGES])}


class TestWorkedExample:
    def test_two_peers_converge_in_one_round_trip(self, peer_factory):
        """A holds {1,3,4}, B holds {1,2,4,5}: both end holding 1 to 5.

        The exact exchange, because the shape of it is the design. A holds no
        more than the newest bucket, so its fresh request names its rows by
        id. B diffs that in one step: it serves the two rows A lacks and asks
        for the one row it lacks itself, in the same answer. A stores them and
        answers the ask. Nothing is ever pushed that was not asked for.
        """
        author = peer_factory("author")
        a = peer_factory("a")
        b = peer_factory("b")
        author_hex = author.identity.hash_hex

        ch_hash = author.channel_mgr.create_channel("worked-example", "", "public")
        _seed_channel_on_peer(a, ch_hash, "worked-example", author_hex)
        _seed_channel_on_peer(b, ch_hash, "worked-example", author_hex)

        base = time.time() - 60
        ids = {}
        for n, holders in {1: (a, b), 2: (b,), 3: (a,), 4: (a, b), 5: (b,)}.items():
            for holder in holders:
                ids[n] = _insert_message(holder.storage, ch_hash, author_hex,
                                         f"message {n}", base + n)

        a_sent = _capture(a)
        b_sent = _capture(b)

        # 1. A asks, naming the three rows it holds.
        a.sync_mgr._send_sync_request(b.identity.hash_hex, ch_hash, base)
        assert len(a_sent) == 1, "A sent more than the one request"
        first_request = sync_ranges.unpack_ranges(a_sent[0][F_SYNC_RANGES])
        assert [mode for _lo, _hi, mode, _payload in first_request] == [RANGE_IDLIST]
        assert set(first_request[0][3]) == {sync_ranges.id_prefix(ids[n]) for n in (1, 3, 4)}

        # 2. B serves exactly what A lacks and asks for the one row it lacks.
        b.sync_mgr._handle_sync_request(a_sent[0], ch_hash, a.identity.hash_hex)
        assert len(b_sent) == 1, "B answered with more than one message"
        assert _served_ids(b_sent[0]) == {ids[2], ids[5]}, \
            "B served something other than exactly what A was missing"
        assert F_SYNC_RANGES not in b_sent[0], "B described a range it had already resolved"
        b_needs = sync_ranges.unpack_needs(b_sent[0][F_SYNC_NEED])
        assert [prefix for _lo, _hi, prefix in b_needs] == \
            [sync_ranges.id_prefix(ids[3])], \
            "B did not ask for the one message it was missing"

        # 3. A stores them and answers B's ask. That is the last message.
        a.sync_mgr._handle_sync_response(b_sent[0], ch_hash, b.identity.hash_hex)
        assert {n for n in ids if a.storage.message_exists(ids[n])} == {1, 2, 3, 4, 5}
        assert len(a_sent) == 2, "A sent something beyond the answer to B's need"
        answer = a_sent[1]
        assert answer[F_MSG_TYPE] == MT_SYNC_RESPONSE
        assert _served_ids(answer) == {ids[3]}, \
            "A's answer carried something other than the row B asked for"

        b.sync_mgr._handle_sync_response(answer, ch_hash, a.identity.hash_hex)
        assert {n for n in ids if b.storage.message_exists(ids[n])} == {1, 2, 3, 4, 5}
        assert len(b_sent) == 1, "B pushed a message nobody asked for"

    def test_a_recent_gap_is_filled_by_the_first_answer(self, peer_factory):
        """A peer that fell behind recently gets its rows in one round trip.

        The ladder spells out the newest bucket by id, so a responder holding
        newer rows in that span can send them at once instead of describing
        the range back and waiting to be asked. The older buckets match and
        are not mentioned again.
        """
        author = peer_factory("author")
        a = peer_factory("a")
        b = peer_factory("b")
        author_hex = author.identity.hash_hex

        ch_hash = author.channel_mgr.create_channel("recent-gap", "", "public")
        _seed_channel_on_peer(a, ch_hash, "recent-gap", author_hex)
        _seed_channel_on_peer(b, ch_hash, "recent-gap", author_hex)

        base = time.time() - 600
        shared = [_insert_message(b.storage, ch_hash, author_hex, f"row {i}", base + i)
                  for i in range(60)]
        for i in range(58):
            _insert_message(a.storage, ch_hash, author_hex, f"row {i}", base + i)
        missing = set(shared[58:])

        a_sent = _capture(a)
        b_sent = _capture(b)
        a.sync_mgr._send_sync_request(b.identity.hash_hex, ch_hash, base)
        ranges = sync_ranges.unpack_ranges(a_sent[0][F_SYNC_RANGES])
        assert len(ranges) > 1 and ranges[-1][2] == RANGE_IDLIST, \
            "a fresh request over a busy window did not ladder"

        b.sync_mgr._handle_sync_request(a_sent[0], ch_hash, a.identity.hash_hex)
        assert len(b_sent) == 1
        assert _served_ids(b_sent[0]) == missing, \
            "the first answer did not carry exactly the rows A lacked"
        assert F_SYNC_RANGES not in b_sent[0], \
            "B described ranges that already matched"
        assert F_SYNC_NEED not in b_sent[0], "B asked for rows it already held"

        a.sync_mgr._handle_sync_response(b_sent[0], ch_hash, b.identity.hash_hex)
        assert all(a.storage.message_exists(mid) for mid in shared)
        assert len(a_sent) == 1, "A followed up when nothing was left to ask"


class TestGapBehindTheWatermark:
    def test_a_message_older_than_our_progress_still_arrives(self, peer_factory):
        """The sync11 shape: a gap the watermark has already run past.

        Both peers kept writing while the link was down, so each holds
        something older than where the other's progress with them now sits.
        Under a timestamp watermark alone that message can never be asked for
        again; the description of the set is what finds it.
        """
        author = peer_factory("author")
        a = peer_factory("a")
        b = peer_factory("b")
        author_hex = author.identity.hash_hex

        ch_hash = author.channel_mgr.create_channel("behind-watermark", "", "public")
        _seed_channel_on_peer(a, ch_hash, "behind-watermark", author_hex)
        _seed_channel_on_peer(b, ch_hash, "behind-watermark", author_hex)
        a.subscription_mgr._subscribers[ch_hash] = {b.identity.hash_hex}

        base = time.time() - 600
        shared = _insert_message(a.storage, ch_hash, author_hex, "both hold this", base)
        _insert_message(b.storage, ch_hash, author_hex, "both hold this", base)
        missed = _insert_message(b.storage, ch_hash, author_hex,
                                 "written while A was away", base + 10)

        # A's progress with B is already well past the message it never got.
        a.storage.advance_peer_sync_progress(ch_hash, b.identity.hash_hex, base + 500)
        a.storage.update_last_sync(ch_hash, base + 500)

        a.sync_mgr._request_sync_for_channel(ch_hash)

        assert wait_for_message(a.storage, ch_hash, missed, timeout=5), \
            "a message older than A's own progress with B was never recovered"
        assert a.storage.message_exists(shared)


class TestBackfill:
    def test_a_fresh_join_backfills_through_continuations(self, peer_factory):
        """Nothing held, a full window on the other side, one trigger."""
        author = peer_factory("author")
        a = peer_factory("a")
        b = peer_factory("b")
        author_hex = author.identity.hash_hex

        ch_hash = author.channel_mgr.create_channel("fresh-join", "", "public")
        _seed_channel_on_peer(a, ch_hash, "fresh-join", author_hex)
        _seed_channel_on_peer(b, ch_hash, "fresh-join", author_hex)
        a.subscription_mgr._subscribers[ch_hash] = {b.identity.hash_hex}

        base = time.time() - 400
        total = 200
        for i in range(total):
            _insert_message(b.storage, ch_hash, author_hex, f"history {i}", base + i)

        a.sync_mgr._request_sync_for_channel(ch_hash, 0.0)

        assert wait_for(lambda: len(a.storage.get_messages(ch_hash, limit=500)) == total,
                        timeout=30), (
            f"backfill stalled at {len(a.storage.get_messages(ch_hash, limit=500))} "
            f"of {total} messages"
        )
        assert wait_for(
            lambda: a.sync_mgr.status.get_state(ch_hash) == SyncState.SYNCED,
            timeout=10,
        ), f"channel never settled, got {a.sync_mgr.status.get_state(ch_hash)}"

    def test_truncation_inside_one_range_completes_on_continuation(self, peer_factory):
        """A batch capped inside a single mismatched range still finishes.

        A holds enough rows to be described as split sub-ranges, and every row
        it lacks sits inside one of them, so the continuation has to narrow to
        that range rather than resume from a timestamp.
        """
        author = peer_factory("author")
        a = peer_factory("a")
        b = peer_factory("b")
        author_hex = author.identity.hash_hex

        ch_hash = author.channel_mgr.create_channel("range-truncation", "", "public")
        _seed_channel_on_peer(a, ch_hash, "range-truncation", author_hex)
        _seed_channel_on_peer(b, ch_hash, "range-truncation", author_hex)
        a.subscription_mgr._subscribers[ch_hash] = {b.identity.hash_hex}

        base = time.time() - 300
        shared = []
        for i in range(40):
            content = f"shared {i}"
            ts = base + i * 2
            shared.append(_insert_message(a.storage, ch_hash, author_hex, content, ts))
            _insert_message(b.storage, ch_hash, author_hex, content, ts)

        buried = [
            _insert_message(b.storage, ch_hash, author_hex, f"buried {i}",
                            base + 20 + i * 0.01)
            for i in range(MAX_RESPONSE_MESSAGES + 10)
        ]

        a.sync_mgr._request_sync_for_channel(ch_hash)

        assert wait_for(lambda: all(a.storage.message_exists(mid) for mid in buried),
                        timeout=20), (
            "rows buried inside one mismatched range were never fully served: "
            f"{sum(1 for m in buried if a.storage.message_exists(m))} of {len(buried)}"
        )
        assert all(a.storage.message_exists(mid) for mid in shared)


class TestWithheldHistorySettles:
    def test_a_range_we_may_not_see_settles_without_looping(self, peer_factory):
        """Pre-join history stays withheld, and the exchange still ends.

        The responder's description omits what it will not serve, so that
        range can never match: what has to be bounded is the narrowing, not
        the difference. The last answer is an empty one.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("withheld-settles", "", "invite")
        time.sleep(0.02)
        pre_join = [
            _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                            f"before bob {i}", time.time())
            for i in range(3)
        ]
        time.sleep(0.02)

        alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex, timeout=5)
        assert wait_for(lambda: bob.storage.get_pending_member_doc(ch_hash) is not None,
                        timeout=5)
        assert bob.invite_mgr.accept_pending_membership(ch_hash)
        assert wait_for(lambda: bob.storage.is_member(ch_hash, bob.identity.hash_hex),
                        timeout=5)

        time.sleep(0.02)
        after_join = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                     "after bob joined", time.time())

        answers = []
        original = alice.sync_mgr._send_raw

        def record(dest_hex, fields):
            if fields.get(F_MSG_TYPE) == MT_SYNC_RESPONSE:
                answers.append(fields)
            return original(dest_hex, fields)

        alice.sync_mgr._send_raw = record

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, time.time())
        assert wait_for_message(bob.storage, ch_hash, after_join, timeout=5)
        for msg_id in pre_join:
            assert not bob.storage.message_exists(msg_id), \
                "pre-join history was served to a member without full_sync"

        # Asked again, the range Bob may not see still differs and still
        # cannot be filled. The answer is empty and nothing follows it.
        answers.clear()
        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, time.time())
        assert wait_for(lambda: len(answers) == 1, timeout=5), \
            "the second request went unanswered"
        assert _served_ids(answers[0]) == set(), \
            "the withheld range was served on the second ask"
        time.sleep(1.0)
        assert len(answers) == 1, \
            "the withheld range kept the two peers asking each other forever"


class TestLegacyRequests:
    def test_a_request_without_ranges_still_gets_the_sweep(self, peer_factory):
        """A peer that predates ranges is answered exactly as it was before."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("legacy-sweep", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "legacy-sweep", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "legacy-sweep", alice.identity.hash_hex)

        window_start = time.time() - 100
        old = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                              "before the window", window_start - 500)
        new = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                              "after the window", window_start + 50)

        answers = _capture(carol)
        carol.sync_mgr._handle_sync_request(
            {
                F_MSG_TYPE:          MT_SYNC_REQUEST,
                F_CHANNEL_HASH:      bytes.fromhex(ch_hash),
                F_SYNC_WINDOW_START: window_start,
            },
            ch_hash, bob.identity.hash_hex,
        )

        assert len(answers) == 1
        assert _served_ids(answers[0]) == {new}, \
            "a legacy request was not answered by the timestamp sweep"
        assert old not in _served_ids(answers[0])
        assert F_SYNC_RANGES not in answers[0], \
            "a peer that sent no ranges was answered with ranges"


class TestDeepReconcileThrottle:
    def _channel(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        ch_hash = alice.channel_mgr.create_channel("deep-ranges", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "deep-ranges", alice.identity.hash_hex)
        return alice, bob, ch_hash

    def test_a_range_request_reaching_past_the_window_is_throttled(self, peer_factory):
        alice, bob, ch_hash = self._channel(peer_factory)
        old_ts = time.time() - SYNC_WINDOW_SECS - 3600
        first = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                "ancient one", old_ts)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, 0.0)
        assert wait_for_message(bob.storage, ch_hash, first, timeout=5), \
            "the first deep reconcile was not served"

        second = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                 "ancient two", old_ts + 1)
        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, 0.0)
        time.sleep(0.5)

        assert not bob.storage.message_exists(second), \
            "a repeated deep reconcile within the cooldown was served anyway"

    def test_an_in_window_range_request_is_never_deep(self, peer_factory):
        """Two peers' clocks never agree exactly, and a window start a
        fraction of a second either side of ours must not read as a deep
        backfill: that would throttle every routine sync after the first."""
        alice, bob, ch_hash = self._channel(peer_factory)
        base = time.time() - 60
        first = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                "recent one", base)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, base)
        assert wait_for_message(bob.storage, ch_hash, first, timeout=5)

        second = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                 "recent two", base + 1)
        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, base)

        assert wait_for_message(bob.storage, ch_hash, second, timeout=5), \
            "an in-window reconcile was throttled as a deep backfill"

    def test_a_need_is_answered_during_the_cooldown(self, peer_factory):
        """A need names rows outright, so it costs nothing a flood of them
        could turn into repeated full sweeps."""
        alice, bob, ch_hash = self._channel(peer_factory)
        old_ts = time.time() - SYNC_WINDOW_SECS - 3600
        wanted = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                 "named directly", old_ts)

        # Arm the cooldown exactly as serving one deep request would.
        assert alice.sync_mgr._deep_sync_allowed(ch_hash, bob.identity.hash_hex)

        answers = _capture(alice)
        deep_ranges = sync_ranges.pack(
            sync_ranges.describe([], 0.0, time.time() + 300))
        alice.sync_mgr._handle_sync_request(
            {
                F_MSG_TYPE:     MT_SYNC_REQUEST,
                F_CHANNEL_HASH: bytes.fromhex(ch_hash),
                F_SYNC_RANGES:  deep_ranges,
            },
            ch_hash, bob.identity.hash_hex,
        )
        assert answers == [], "a deep reconcile inside the cooldown was answered"

        alice.sync_mgr._handle_sync_request(
            {
                F_MSG_TYPE:     MT_SYNC_REQUEST,
                F_CHANNEL_HASH: bytes.fromhex(ch_hash),
                F_SYNC_NEED:    sync_ranges.pack(
                    [[old_ts - 1, old_ts + 1, sync_ranges.id_prefix(wanted)]]),
            },
            ch_hash, bob.identity.hash_hex,
        )
        assert len(answers) == 1, "a need-only request went unanswered"
        assert _served_ids(answers[0]) == {wanted}, \
            "a need-only request was throttled by the deep-sync cooldown"


class TestRangesAreOnTheWire:
    def test_a_fresh_request_summarises_the_window(self, peer_factory):
        """A routine re-check costs a ladder of fingerprints with the newest
        few rows by id, never a list of everything held."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        ch_hash = alice.channel_mgr.create_channel("describe", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "describe", alice.identity.hash_hex)

        base = time.time() - 300
        held = [_insert_message(bob.storage, ch_hash, alice.identity.hash_hex,
                                f"held {i}", base + i) for i in range(100)]

        sent = _capture(bob)
        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, base)

        ranges = sync_ranges.unpack_ranges(sent[0][F_SYNC_RANGES])
        assert ranges is not None, "the request's ranges did not validate"
        assert [mode for _lo, _hi, mode, _payload in ranges] == \
            [RANGE_FINGERPRINT] * (len(ranges) - 1) + [RANGE_IDLIST]
        assert set(ranges[-1][3]) == \
            {sync_ranges.id_prefix(mid) for mid in held[-sync_ranges.SYNC_SUMMARY_LADDER[0]:]}
        assert sum(payload[0] for _lo, _hi, mode, payload in ranges[:-1]) == \
            len(held) - sync_ranges.SYNC_SUMMARY_LADDER[0]
        assert len(sent[0][F_SYNC_RANGES]) <= sync_ranges.SYNC_DESCRIPTION_BUDGET_BYTES

    def test_a_continuation_describes_what_we_already_hold(self, peer_factory):
        """Only once a peer says the window differs is a list worth sending."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        ch_hash = alice.channel_mgr.create_channel("narrow", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "narrow", alice.identity.hash_hex)

        base = time.time() - 30
        held = [_insert_message(bob.storage, ch_hash, alice.identity.hash_hex,
                                f"held {i}", base + i) for i in range(3)]

        sent = _capture(bob)
        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, base,
                                        continuation=True)

        ranges = sync_ranges.unpack_ranges(sent[0][F_SYNC_RANGES])
        assert ranges is not None, "the continuation's ranges did not validate"
        named = {prefix for _lo, _hi, _mode, payload in ranges for prefix in payload}
        assert named == {sync_ranges.id_prefix(mid) for mid in held}

    def test_an_unsigned_row_is_not_claimed(self, peer_factory):
        """An unsigned row cannot be relayed to anyone, so claiming to hold it
        would stop peers offering their own verifiable copy."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        ch_hash = alice.channel_mgr.create_channel("unsigned", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "unsigned", alice.identity.hash_hex)

        ts = time.time() - 30
        bob.storage.insert_message(
            channel_hash=ch_hash, sender_hash=alice.identity.hash_hex,
            sender_name="Alice", content="legacy", timestamp=ts,
            message_id=_compute_message_id("legacy", alice.identity.hash_hex, ts),
            reply_to=None, last_seen_id=None, received_at=ts,
        )

        sent = _capture(bob)
        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, ts)

        ranges = sync_ranges.unpack_ranges(sent[0][F_SYNC_RANGES])
        assert [payload for _lo, _hi, _mode, payload in ranges] == [()], \
            "an unsigned row was named as something we hold"


@pytest.mark.parametrize("field", [F_SYNC_RANGES, F_SYNC_NEED])
def test_a_malformed_reconcile_field_is_refused_whole(peer_factory, field):
    """Half a description is worse than none: it would have us answer against
    a set the peer never actually described."""
    alice = peer_factory("alice")
    bob = peer_factory("bob")
    ch_hash = alice.channel_mgr.create_channel("malformed", "", "public")
    _seed_channel_on_peer(bob, ch_hash, "malformed", alice.identity.hash_hex)
    _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                    "would have been served", time.time() - 10)

    answers = _capture(alice)
    alice.sync_mgr._handle_sync_request(
        {
            F_MSG_TYPE:     MT_SYNC_REQUEST,
            F_CHANNEL_HASH: bytes.fromhex(ch_hash),
            field:          b"not a packed description",
        },
        ch_hash, bob.identity.hash_hex,
    )

    assert answers == [], "a malformed request was answered anyway"


def _tap(peer):
    """Record everything a peer sends while still delivering it."""
    sent = []
    original = peer.sync_mgr._send_raw

    def tapped(dest_hex, fields):
        sent.append(fields)
        return original(dest_hex, fields)

    peer.sync_mgr._send_raw = tapped
    return sent


def _requests(sent) -> list:
    return [f for f in sent if f.get(F_MSG_TYPE) == MT_SYNC_REQUEST]


def _rows_served(sent) -> list[str]:
    served: list[str] = []
    for f in sent:
        if f.get(F_MSG_TYPE) == MT_SYNC_RESPONSE and f.get(F_SYNC_MESSAGES):
            served.extend(_served_ids(f))
    return served


class TestExtendedAbsence:
    """A peer that was away long enough to miss far more than one answer holds.

    An absence is measured here in what it missed, not in wall-clock time: the
    rows are inserted with the timestamps the absence would have produced.
    """

    def _pair(self, peer_factory, name):
        """A creates the channel and B joins it, so each has exactly one peer
        and every request in the exchange is between the two of them."""
        a = peer_factory("a")
        b = peer_factory("b")
        author_hex = a.identity.hash_hex
        ch_hash = a.channel_mgr.create_channel(name, "", "public")
        _seed_channel_on_peer(b, ch_hash, name, author_hex)
        a.subscription_mgr._subscribers[ch_hash] = {b.identity.hash_hex}
        return a, b, author_hex, ch_hash

    def test_a_long_absence_backfills_the_whole_run(self, peer_factory):
        """Away across 300 rows: everything arrives, once, in bounded rounds.

        The newest bucket of A's ladder spans everything written after it
        left, so the first answer already carries a capped batch, and each
        continuation narrows to what is still missing rather than resuming
        from a timestamp. No row is served twice.
        """
        a, b, author_hex, ch_hash = self._pair(peer_factory, "long-absence")
        base = time.time() - 3600
        held = 40
        missed = 300
        for i in range(held):
            _insert_message(a.storage, ch_hash, author_hex, f"row {i}", base + i * 5)
        for i in range(held + missed):
            _insert_message(b.storage, ch_hash, author_hex, f"row {i}", base + i * 5)

        a_sent = _tap(a)
        b_sent = _tap(b)
        a.sync_mgr._request_sync_for_channel(ch_hash)

        assert wait_for(
            lambda: len(a.storage.get_messages(ch_hash, limit=1000)) == held + missed,
            timeout=30,
        ), (f"backfill stalled at {len(a.storage.get_messages(ch_hash, limit=1000))} "
            f"of {held + missed}")
        assert wait_for(
            lambda: a.sync_mgr.status.get_state(ch_hash) == SyncState.SYNCED, timeout=10
        ), f"channel never settled, got {a.sync_mgr.status.get_state(ch_hash)}"

        served = _rows_served(b_sent)
        assert len(served) == len(set(served)) == missed, \
            f"{len(served)} rows served for a gap of {missed}"
        batches = -(-missed // MAX_RESPONSE_MESSAGES)
        assert len(_requests(a_sent)) <= batches + 2, \
            f"{len(_requests(a_sent))} requests to recover {batches} batches"

    def test_a_gap_in_the_middle_of_a_long_history(self, peer_factory):
        """Away for a stretch that the newest bucket no longer covers.

        The missing rows sit behind hundreds A did receive afterwards, so the
        ladder's id list does not reach them and the fingerprint buckets they
        fall in have to be narrowed. They are still all found, and only they
        are served.
        """
        a, b, author_hex, ch_hash = self._pair(peer_factory, "middle-gap")
        base = time.time() - 3600
        total = 300
        gap = range(100, 160)
        for i in range(total):
            _insert_message(b.storage, ch_hash, author_hex, f"row {i}", base + i * 5)
            if i not in gap:
                _insert_message(a.storage, ch_hash, author_hex, f"row {i}", base + i * 5)

        a_sent = _tap(a)
        b_sent = _tap(b)
        a.sync_mgr._request_sync_for_channel(ch_hash)

        assert wait_for(
            lambda: len(a.storage.get_messages(ch_hash, limit=1000)) == total, timeout=30
        ), (f"backfill stalled at {len(a.storage.get_messages(ch_hash, limit=1000))} "
            f"of {total}")
        assert wait_for(
            lambda: a.sync_mgr.status.get_state(ch_hash) == SyncState.SYNCED, timeout=10
        )
        served = _rows_served(b_sent)
        assert len(served) == len(set(served)) == len(gap), \
            f"{len(served)} rows served for a gap of {len(gap)}"
        # One summary, one narrowing into the described buckets, one
        # re-summary after the capped batch, one narrowing for the rest.
        assert len(_requests(a_sent)) <= 5, \
            f"{len(_requests(a_sent))} requests to close one mid-history gap"

    def test_an_absence_longer_than_the_sync_window(self, peer_factory):
        """Away past SYNC_WINDOW_SECS: a deep exchange, still completed.

        A's own progress with B predates the window, so the request reaches
        back to it and is classified deep. The responder paces a fresh deep
        ask but must keep serving the narrowing steps of the exchange it
        opened, or A would be stranded halfway with nothing left to ask.
        """
        a, b, author_hex, ch_hash = self._pair(peer_factory, "beyond-window")
        now = time.time()
        old_base = now - SYNC_WINDOW_SECS - 3 * 86400
        for i in range(20):
            for holder in (a, b):
                _insert_message(holder.storage, ch_hash, author_hex, f"old {i}",
                                old_base + i * 60)
        recent_base = now - SYNC_WINDOW_SECS - 86400
        span = SYNC_WINDOW_SECS + 86400 - 120
        missed = 120
        for i in range(missed):
            _insert_message(b.storage, ch_hash, author_hex, f"while away {i}",
                            recent_base + i * (span / missed))
        a.storage.advance_peer_sync_progress(ch_hash, b.identity.hash_hex,
                                             old_base + 19 * 60)

        b_sent = _tap(b)
        a.sync_mgr._request_sync_for_channel(ch_hash)

        assert wait_for(
            lambda: len(a.storage.get_messages(ch_hash, limit=1000)) == 20 + missed,
            timeout=30,
        ), (f"deep backfill stalled at {len(a.storage.get_messages(ch_hash, limit=1000))} "
            f"of {20 + missed}")
        assert wait_for(
            lambda: a.sync_mgr.status.get_state(ch_hash) == SyncState.SYNCED, timeout=10
        ), f"channel never settled, got {a.sync_mgr.status.get_state(ch_hash)}"
        served = _rows_served(b_sent)
        assert len(served) == len(set(served)) == missed

    def test_both_sides_wrote_at_length_while_apart(self, peer_factory):
        """Two long disjoint histories, interleaved in time, merge both ways."""
        a, b, author_hex, ch_hash = self._pair(peer_factory, "both-wrote")
        base = time.time() - 3600
        shared = 50
        each = 80
        for i in range(shared):
            for holder in (a, b):
                _insert_message(holder.storage, ch_hash, author_hex, f"shared {i}",
                                base + i)
        apart = base + shared
        for i in range(each):
            _insert_message(a.storage, ch_hash, a.identity.hash_hex, f"a alone {i}",
                            apart + i * 2)
            _insert_message(b.storage, ch_hash, b.identity.hash_hex, f"b alone {i}",
                            apart + i * 2 + 1)

        a.sync_mgr._request_sync_for_channel(ch_hash)
        b.sync_mgr._request_sync_for_channel(ch_hash)

        total = shared + 2 * each
        for peer in (a, b):
            assert wait_for(
                lambda: len(peer.storage.get_messages(ch_hash, limit=1000)) == total,
                timeout=30,
            ), (f"{peer.identity.hash_hex[:8]} stalled at "
                f"{len(peer.storage.get_messages(ch_hash, limit=1000))} of {total}")
        for peer in (a, b):
            assert wait_for(
                lambda: peer.sync_mgr.status.get_state(ch_hash) == SyncState.SYNCED,
                timeout=10,
            ), f"{peer.identity.hash_hex[:8]} never settled"
