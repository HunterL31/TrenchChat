"""
Unit tests for the range arithmetic behind sync reconciliation.

No peers, no storage, no network: everything here is a list of rows and the
description another peer would receive of it.
"""

import hashlib

import pytest

from trenchchat.core.protocol import (
    RANGE_FINGERPRINT, RANGE_IDLIST, SYNC_FINGERPRINT_BYTES, SYNC_ID_PREFIX_BYTES,
)
from trenchchat.core import sync_ranges as sr


def _row(index: int, ts: float) -> dict:
    """A row whose id is a plausible digest, deterministic in *index*."""
    return {"message_id": hashlib.sha256(f"row-{index}".encode()).hexdigest(),
            "timestamp": float(ts)}


def _rows(count: int, start: float = 1000.0, step: float = 1.0) -> list[dict]:
    return [_row(i, start + i * step) for i in range(count)]


class TestFingerprint:
    def test_is_independent_of_order(self):
        rows = _rows(10)
        ids = [r["message_id"] for r in rows]
        assert sr.fingerprint(ids) == sr.fingerprint(list(reversed(ids)))

    def test_differs_for_a_different_set(self):
        ids = [r["message_id"] for r in _rows(10)]
        assert sr.fingerprint(ids) != sr.fingerprint(ids[:-1])

    def test_is_the_documented_width(self):
        assert len(sr.fingerprint([r["message_id"] for r in _rows(3)])) == \
            SYNC_FINGERPRINT_BYTES

    def test_empty_set_has_a_fingerprint(self):
        assert sr.fingerprint([]) == hashlib.sha256(b"").digest()[:SYNC_FINGERPRINT_BYTES]

    def test_matches_fingerprint_compares_count_and_digest(self):
        rows = _rows(5)
        ids = [r["message_id"] for r in rows]
        assert sr.matches_fingerprint(rows, len(ids), sr.fingerprint(ids))
        assert not sr.matches_fingerprint(rows[:-1], len(ids), sr.fingerprint(ids))


class TestDescribe:
    def test_a_small_range_is_one_id_list(self):
        described = sr.describe(_rows(sr.SYNC_LEAF_IDS), 0.0, 1e9)
        assert len(described) == 1
        lo, hi, mode, payload = described[0]
        assert (lo, hi, mode) == (0.0, 1e9, RANGE_IDLIST)
        assert len(payload) == sr.SYNC_LEAF_IDS
        assert all(len(p) == SYNC_ID_PREFIX_BYTES for p in payload)

    def test_holding_nothing_is_one_empty_id_list(self):
        assert sr.describe([], 5.0, 9.0) == [[5.0, 9.0, RANGE_IDLIST, []]]

    def test_an_id_list_is_sorted(self):
        payload = sr.describe(_rows(20), 0.0, 1e9)[0][3]
        assert payload == sorted(payload)

    def test_a_large_range_splits_into_at_most_the_fanout(self):
        described = sr.describe(_rows(400), 0.0, 1e9)
        assert 1 < len(described) <= sr.SYNC_RANGE_FANOUT

    def test_sub_ranges_cover_the_whole_span_contiguously(self):
        described = sr.describe(_rows(400), 100.0, 5000.0)
        assert described[0][0] == 100.0
        assert described[-1][1] == 5000.0
        for earlier, later in zip(described, described[1:]):
            assert earlier[1] == later[0]

    def test_every_row_lands_in_exactly_one_sub_range(self):
        rows = _rows(400)
        described = sr.describe(rows, 0.0, 1e9)
        counted = sum(len(sr.rows_in(rows, lo, hi)) for lo, hi, _m, _p in described)
        assert counted == len(rows)

    def test_a_huge_range_falls_back_to_fingerprints(self):
        described = sr.describe(_rows(4000), 0.0, 1e9)
        assert all(mode == RANGE_FINGERPRINT for _lo, _hi, mode, _p in described)
        counts = [payload[0] for _lo, _hi, _m, payload in described]
        assert sum(counts) == 4000

    def test_a_tied_timestamp_group_is_never_split(self):
        # Two hundred rows on one timestamp, surrounded by ordinary traffic.
        rows = _rows(100, start=1000.0) + [_row(1000 + i, 2000.0) for i in range(200)] \
            + _rows(100, start=3000.0)
        described = sr.describe(rows, 0.0, 1e9)
        for lo, hi, _mode, _payload in described:
            covered = [r for r in rows if lo <= r["timestamp"] < hi
                       and r["timestamp"] == 2000.0]
            assert len(covered) in (0, 200), \
                "a group sharing one timestamp was split across ranges"

    def test_one_timestamp_that_cannot_be_split_is_spelled_out(self):
        rows = [_row(i, 42.0) for i in range(sr.SYNC_LEAF_IDS + 5)]
        described = sr.describe(rows, 0.0, 100.0)
        assert len(described) == 1
        assert described[0][2] == RANGE_IDLIST
        assert len(described[0][3]) == sr.SYNC_LEAF_IDS + 5

    def test_one_timestamp_past_the_inbound_cap_becomes_a_fingerprint(self):
        rows = [_row(i, 42.0) for i in range(sr.MAX_SYNC_LIST_IDS + 1)]
        described = sr.describe(rows, 0.0, 100.0)
        assert len(described) == 1
        assert described[0][2] == RANGE_FINGERPRINT

    def test_rows_outside_the_span_are_ignored(self):
        rows = _rows(5, start=10.0) + _rows(5, start=500.0)
        described = sr.describe(rows, 0.0, 100.0)
        assert len(described[0][3]) == 5

    def test_hi_is_exclusive(self):
        rows = [_row(0, 10.0), _row(1, 20.0)]
        assert len(sr.describe(rows, 0.0, 20.0)[0][3]) == 1


class TestDiffs:
    def test_ids_they_lack_names_only_what_is_missing(self):
        rows = _rows(6)
        theirs = sr.prefixes_of(rows[:4])
        assert sr.ids_they_lack(rows, theirs) == [r["message_id"] for r in rows[4:]]

    def test_prefixes_we_lack_names_only_what_is_new(self):
        rows = _rows(6)
        theirs = sorted(sr.prefixes_of(rows))
        assert sr.prefixes_we_lack(rows[:4], theirs) == \
            sorted(sr.prefixes_of(rows[4:]))

    def test_matching_sets_leave_nothing_on_either_side(self):
        rows = _rows(6)
        theirs = sr.prefixes_of(rows)
        assert sr.ids_they_lack(rows, theirs) == []
        assert sr.prefixes_we_lack(rows, sorted(theirs)) == []


class TestValidation:
    def _idlist(self, count: int, lo=0.0, hi=10.0):
        rows = _rows(count)
        return sr.id_list_range(lo, hi, rows)

    def test_a_well_formed_description_round_trips(self):
        ranges = sr.describe(_rows(100), 0.0, 1e6)
        assert sr.unpack_ranges(sr.pack(ranges)) is not None

    def test_needs_round_trip(self):
        needs = [[1.0, 2.0, b"\x01" * SYNC_ID_PREFIX_BYTES]]
        assert sr.unpack_needs(sr.pack(needs)) == [(1.0, 2.0, b"\x01" * 8)]

    def test_a_non_list_is_refused(self):
        assert sr.validate_ranges({"lo": 1}) is None
        assert sr.validate_needs("nope") is None

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, "soon"])
    def test_an_implausible_bound_is_refused(self, bad):
        assert sr.validate_ranges([[bad, 10.0, RANGE_IDLIST, []]]) is None
        assert sr.validate_needs([[bad, 10.0, b"\x01" * 8]]) is None

    def test_lo_after_hi_is_refused(self):
        assert sr.validate_ranges([[10.0, 1.0, RANGE_IDLIST, []]]) is None

    def test_overlapping_ranges_are_refused(self):
        assert sr.validate_ranges([
            [0.0, 10.0, RANGE_IDLIST, []],
            [5.0, 20.0, RANGE_IDLIST, []],
        ]) is None

    def test_ascending_touching_ranges_are_accepted(self):
        assert sr.validate_ranges([
            [0.0, 10.0, RANGE_IDLIST, []],
            [10.0, 20.0, RANGE_IDLIST, []],
        ]) is not None

    def test_too_many_ranges_are_refused(self):
        many = [[float(i), float(i + 1), RANGE_IDLIST, []]
                for i in range(sr.MAX_SYNC_RANGES + 1)]
        assert sr.validate_ranges(many) is None

    def test_an_over_cap_id_list_is_refused(self):
        rows = [_row(i, 1.0) for i in range(sr.MAX_SYNC_LIST_IDS + 1)]
        assert sr.validate_ranges([sr.id_list_range(0.0, 2.0, rows)]) is None

    def test_an_id_list_split_over_the_cap_is_refused(self):
        half = sr.MAX_SYNC_LIST_IDS // 2 + 1
        first = sr.id_list_range(0.0, 1.0, [_row(i, 0.5) for i in range(half)])
        second = sr.id_list_range(1.0, 2.0,
                                  [_row(1000 + i, 1.5) for i in range(half)])
        assert sr.validate_ranges([first, second]) is None

    def test_a_bad_prefix_length_is_refused(self):
        assert sr.validate_ranges([[0.0, 1.0, RANGE_IDLIST, [b"\x01\x02"]]]) is None
        assert sr.validate_needs([[0.0, 1.0, b"\x01\x02"]]) is None

    def test_an_unsorted_id_list_is_refused(self):
        assert sr.validate_ranges([[0.0, 1.0, RANGE_IDLIST,
                                    [b"\xff" * 8, b"\x01" * 8]]]) is None

    def test_a_duplicated_prefix_is_refused(self):
        assert sr.validate_ranges([[0.0, 1.0, RANGE_IDLIST,
                                    [b"\x01" * 8, b"\x01" * 8]]]) is None

    def test_a_bad_fingerprint_width_is_refused(self):
        assert sr.validate_ranges([[0.0, 1.0, RANGE_FINGERPRINT, [1, b"short"]]]) is None

    def test_a_negative_count_is_refused(self):
        assert sr.validate_ranges([[0.0, 1.0, RANGE_FINGERPRINT,
                                    [-1, b"\x00" * SYNC_FINGERPRINT_BYTES]]]) is None

    def test_an_unknown_mode_is_refused(self):
        assert sr.validate_ranges([[0.0, 1.0, 99, []]]) is None

    def test_too_many_needs_are_refused(self):
        many = [[0.0, 1.0, bytes([i]) * 8] for i in range(sr.MAX_SYNC_NEEDS + 1)]
        assert sr.validate_needs(many) is None

    def test_a_payload_that_is_not_msgpack_is_refused(self):
        assert sr.unpack_ranges(b"not msgpack at all") is None
        assert sr.unpack_needs(b"\xc1") is None


class TestSignature:
    def test_the_same_question_has_the_same_signature(self):
        ranges = sr.describe(_rows(50), 0.0, 1e6)
        assert sr.signature(ranges, []) == \
            sr.signature(sr.unpack_ranges(sr.pack(ranges)), [])

    def test_a_narrower_question_has_a_different_signature(self):
        assert sr.signature(sr.describe(_rows(50), 0.0, 1e6), []) != \
            sr.signature(sr.describe(_rows(50), 0.0, 500.0), [])
