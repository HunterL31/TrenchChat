"""
Set reconciliation over message ids, expressed as timestamp ranges.

A watermark cannot describe a set. Two peers that both kept writing through a
partition each hold rows the other lacks on either side of it, and "everything
since T" cannot name which: whichever side's T is later hides the gap behind
it. What both sides can afford to exchange instead is a description of the set
they hold, coarse at first and refined only where it differs.

A side describes what it holds in [lo, hi) as ranges. The other side compares
that description against its own rows, sends what the first side is missing,
and asks for what it is missing itself. Where a range is too big to spell out,
it is split and only the halves that actually differ are looked at again, so
the traffic is proportional to the difference rather than to the history.

A range is [lo, hi, mode, payload], covering rows with lo <= timestamp < hi:

  RANGE_FINGERPRINT  payload [count, fp]: fp is SYNC_FINGERPRINT_BYTES of
                     SHA-256 over the range's ids, sorted and concatenated.
                     Equal count and fingerprint means equal sets.
  RANGE_IDLIST       payload [prefix, ...]: every id in the range by its
                     leading SYNC_ID_PREFIX_BYTES, ascending. Small enough to
                     diff outright, so a leaf range ends the exchange.

This module is the set arithmetic and nothing else: no storage, no RNS, no
network, so it can be reasoned about (and tested) on plain lists. A row is
anything indexable by "message_id" and "timestamp", which is what
Storage.get_message_index returns.
"""

import hashlib
import math

import msgpack

from trenchchat.core.protocol import (
    RANGE_FINGERPRINT, RANGE_IDLIST, SYNC_FINGERPRINT_BYTES,
    SYNC_ID_PREFIX_BYTES, unpack_wire,
)

# Sub-ranges a mismatched range is split into. Wide rather than binary: each
# round trip on a mesh costs seconds to minutes, so the thing worth minimising
# is round trips, not bytes per message.
SYNC_RANGE_FANOUT = 16

# A range holding at most this many local rows is spelled out as an id list
# instead of fingerprinted. Sized so that spelling one out (32 * 8 bytes) is
# cheaper than the round trip that comparing a fingerprint would cost.
SYNC_LEAF_IDS = 32

# What one description may cost on the wire, packed. Spelling ids out is what
# this actually bounds: a prefix costs 10 bytes packed, so a description that
# names every row costs about ten bytes a row, and a busy window ran to five
# kilobytes before this existed. At 1 kbps that budget is four seconds of
# airtime, and it clears one full leaf list (SYNC_LEAF_IDS ids pack to 344
# bytes) so a leaf can always be spelled out rather than fingerprinted
# forever. Over it, ranges are summarised by fingerprint instead: that costs a
# round trip rather than a set of ids, and a round trip is the cheaper of the
# two on a slow link.
SYNC_DESCRIPTION_BUDGET_BYTES = 512

# Inbound caps. These are the hard refusal bound, not the budget: a peer may
# legitimately send a description built under different constants, so what is
# refused is only what no honest peer could produce. A well-formed description
# of one window is at most SYNC_RANGE_FANOUT ranges, and a full split of id
# lists is exactly SYNC_RANGE_FANOUT * SYNC_LEAF_IDS prefixes.
MAX_SYNC_RANGES = 32
MAX_SYNC_LIST_IDS = SYNC_RANGE_FANOUT * SYNC_LEAF_IDS

# Rows one message may ask for by prefix. Matches sync.MAX_RESPONSE_MESSAGES:
# more than that cannot be answered in one response anyway.
MAX_SYNC_NEEDS = 50


def _id_bytes(message_id: str) -> bytes:
    """An id as bytes, tolerating a stored id that is not a hex digest."""
    try:
        return bytes.fromhex(message_id)
    except (ValueError, AttributeError, TypeError):
        return str(message_id).encode(errors="replace")


def id_prefix(message_id: str) -> bytes:
    """The leading bytes of an id, as an id list or a need names it."""
    return _id_bytes(message_id)[:SYNC_ID_PREFIX_BYTES]


def fingerprint(message_ids) -> bytes:
    """A digest over a set of ids, independent of the order they arrive in."""
    digest = hashlib.sha256()
    for message_id in sorted(message_ids):
        digest.update(_id_bytes(message_id))
    return digest.digest()[:SYNC_FINGERPRINT_BYTES]


def prefixes_of(rows) -> set[bytes]:
    """The id prefixes of a set of rows."""
    return {id_prefix(row["message_id"]) for row in rows}


def sort_rows(rows) -> list:
    """Rows in the order every range operation here assumes: oldest first."""
    return sorted(rows, key=lambda r: (r["timestamp"], r["message_id"]))


def id_list_range(lo: float, hi: float, rows) -> list:
    """A range naming every row in it by id prefix."""
    return [float(lo), float(hi), RANGE_IDLIST, sorted(prefixes_of(rows))]


def fingerprint_range(lo: float, hi: float, rows) -> list:
    """A range summarising the rows in it as a count and a digest."""
    ids = [row["message_id"] for row in rows]
    return [float(lo), float(hi), RANGE_FINGERPRINT, [len(ids), fingerprint(ids)]]


def rows_in(rows, lo: float, hi: float) -> list:
    """The rows a range covers: lo inclusive, hi exclusive."""
    return [row for row in rows if lo <= row["timestamp"] < hi]


def _split_bounds(rows: list, lo: float, hi: float,
                  fanout: int = SYNC_RANGE_FANOUT) -> list[tuple[float, float]]:
    """Contiguous [lo, hi) sub-ranges holding roughly equal counts of rows.

    Every boundary sits on a row's timestamp and never inside a group of rows
    sharing one: a boundary travels as a bare float with no tie-breaker, so a
    group split down the middle would land its halves in two ranges neither
    side could ever compare consistently. Rows must be sorted oldest first.
    """
    total = len(rows)
    target = max(1, math.ceil(total / fanout))
    bounds: list[tuple[float, float]] = []
    start = float(lo)
    count = 0
    for i, row in enumerate(rows):
        count += 1
        if count < target or i + 1 >= total or len(bounds) >= fanout - 1:
            continue
        boundary = float(rows[i + 1]["timestamp"])
        if boundary <= float(row["timestamp"]):
            continue
        bounds.append((start, boundary))
        start = boundary
        count = 0
    bounds.append((start, float(hi)))
    return bounds


def summarise(rows, lo: float, hi: float) -> list[list]:
    """The whole of [lo, hi) as one fingerprint: what a fresh request sends.

    A routine re-check is almost always a no-op, and asking it should cost
    about ninety bytes rather than a list of everything the asker holds. Only
    once a peer answers that the range differs is it worth spending a
    description on where (describe() below).

    Holding nothing stays a single empty id list, so a fresh join still asks
    for the window outright and gets rows in the first answer instead of
    spending a round trip proving it has none.
    """
    rows = rows_in(rows, lo, hi)
    if not rows:
        return [id_list_range(lo, hi, rows)]
    return [fingerprint_range(lo, hi, rows)]


def describe(rows, lo: float, hi: float,
             budget: int = SYNC_DESCRIPTION_BUDGET_BYTES) -> list[list]:
    """Where in [lo, hi) a difference might be: what a narrowing step sends.

    One id list when the range is small enough to spell out, otherwise up to
    SYNC_RANGE_FANOUT sub-ranges split by equal row count, each an id list or a
    fingerprint by the same rule. Holding nothing yields one empty id list.

    The result never exceeds *budget* packed, bar the one case below. Id
    lists are what grow, so they are the first thing given up: the largest is
    summarised as a fingerprint, then the next, oldest first among equals so
    the newest range (where a peer that is behind is most likely to be missing
    rows) keeps its ids longest.
    If every range is a fingerprint and there are still too many to fit, the
    same span is described again in fewer, coarser ranges. Narrowing is
    slower, never wider.

    A range whose rows all share a single timestamp cannot be split at all. It
    is spelled out however long that makes it, budget or no budget, because a
    fingerprint there could never be narrowed and the two sides would disagree
    about it forever. Past what a receiver will accept it is summarised
    anyway, and settles as a standing mismatch on one range rather than having
    the whole window refused.
    """
    rows = sort_rows(rows_in(rows, lo, hi))
    if len(rows) <= SYNC_LEAF_IDS:
        return [id_list_range(lo, hi, rows)]

    described = _split_describe(rows, lo, hi, SYNC_RANGE_FANOUT)
    if len(described) < 2:
        # One timestamp, so there is nothing to split and nothing a
        # fingerprint here could ever resolve: spell it out, budget or not.
        return described

    described = _fit_budget(described, rows, budget)
    if packed_size(described) <= budget:
        return described

    coarse = max(2, budget // _FINGERPRINT_RANGE_BYTES)
    return _fit_budget(_split_describe(rows, lo, hi, coarse), rows, budget)


def _split_describe(rows: list, lo: float, hi: float, fanout: int) -> list[list]:
    """Split [lo, hi) *fanout* ways and name each part by ids or fingerprint."""
    bounds = _split_bounds(rows, lo, hi, fanout)
    if len(bounds) < 2:
        if len(rows) > MAX_SYNC_LIST_IDS:
            return [fingerprint_range(lo, hi, rows)]
        return [id_list_range(lo, hi, rows)]

    described: list[list] = []
    for sub_lo, sub_hi in bounds:
        sub = rows_in(rows, sub_lo, sub_hi)
        described.append(id_list_range(sub_lo, sub_hi, sub)
                         if len(sub) <= SYNC_LEAF_IDS
                         else fingerprint_range(sub_lo, sub_hi, sub))
    return described


def _fit_budget(described: list[list], rows: list, budget: int) -> list[list]:
    """Summarise id lists, largest first, until the description fits."""
    while packed_size(described) > budget:
        widest = -1
        chosen = None
        for i, (_lo, _hi, mode, payload) in enumerate(described):
            if mode == RANGE_IDLIST and len(payload) > widest:
                widest, chosen = len(payload), i
        if chosen is None:
            break
        lo, hi, _mode, _payload = described[chosen]
        described[chosen] = fingerprint_range(lo, hi, rows_in(rows, lo, hi))
    return described


def _id_count(ranges) -> int:
    return sum(len(payload) for _lo, _hi, mode, payload in ranges
               if mode == RANGE_IDLIST)


def append_ranges(target: list, described: list,
                  budget: int = SYNC_DESCRIPTION_BUDGET_BYTES) -> bool:
    """Add a description to a message's ranges, if it still fits.

    A message may need to narrow several ranges at once, and the budget is
    what one message spends, not what one describe() call does. False when it
    no longer fits: the ranges left out are described again on the next
    request, where a smaller difference makes room for them, so narrowing gets
    slower rather than a message getting bigger.
    """
    if len(target) + len(described) > MAX_SYNC_RANGES:
        return False
    if _id_count(target) + _id_count(described) > MAX_SYNC_LIST_IDS:
        return False
    if target and packed_size(target + described) > budget:
        return False
    target.extend(described)
    return True


def is_summary(ranges, needs) -> bool:
    """True if this describes a whole window rather than narrowing inside one.

    A fresh ask is exactly one range: a fingerprint of everything the asker
    holds there, or an empty id list when it holds nothing. Anything else,
    more ranges, ids spelled out, or a need, is a step inside an exchange that
    has already established the two sides differ.
    """
    if needs or not ranges or len(ranges) > 1:
        return False
    _lo, _hi, mode, payload = ranges[0]
    return mode == RANGE_FINGERPRINT or not payload


def matches_fingerprint(rows, count: int, digest: bytes) -> bool:
    """True if these rows are the set the fingerprint describes."""
    ids = [row["message_id"] for row in rows]
    return len(ids) == count and fingerprint(ids) == digest


def ids_they_lack(rows, their_prefixes) -> list[str]:
    """Ids among *rows* that a peer's id list does not name."""
    theirs = set(their_prefixes)
    return [row["message_id"] for row in sort_rows(rows)
            if id_prefix(row["message_id"]) not in theirs]


def prefixes_we_lack(rows, their_prefixes) -> list[bytes]:
    """Prefixes a peer named that none of *rows* accounts for."""
    ours = prefixes_of(rows)
    return [prefix for prefix in their_prefixes if prefix not in ours]


# --- wire format ---

def _finite(value) -> float | None:
    """A timestamp off the wire, or None if it is not a usable one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def validate_ranges(value) -> list | None:
    """Ranges as received, or None if any part of them is malformed.

    Refused whole rather than in part: a peer that cannot describe a window
    correctly has told us nothing we can safely act on, and acting on the
    half that parsed would serve rows against a set we do not actually know.
    """
    if not isinstance(value, (list, tuple)) or len(value) > MAX_SYNC_RANGES:
        return None

    parsed: list = []
    previous_hi: float | None = None
    total_ids = 0
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 4:
            return None
        lo, hi, mode, payload = item
        lo = _finite(lo)
        hi = _finite(hi)
        if lo is None or hi is None or lo > hi:
            return None
        if previous_hi is not None and lo < previous_hi:
            return None
        previous_hi = hi

        if mode == RANGE_FINGERPRINT:
            if not isinstance(payload, (list, tuple)) or len(payload) != 2:
                return None
            count, digest = payload
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                return None
            if not isinstance(digest, bytes) or len(digest) != SYNC_FINGERPRINT_BYTES:
                return None
            parsed.append((lo, hi, RANGE_FINGERPRINT, (count, digest)))
        elif mode == RANGE_IDLIST:
            if not isinstance(payload, (list, tuple)):
                return None
            prefixes: list[bytes] = []
            for prefix in payload:
                if not isinstance(prefix, bytes) or len(prefix) != SYNC_ID_PREFIX_BYTES:
                    return None
                if prefixes and prefix <= prefixes[-1]:
                    return None
                prefixes.append(prefix)
            total_ids += len(prefixes)
            if total_ids > MAX_SYNC_LIST_IDS:
                return None
            parsed.append((lo, hi, RANGE_IDLIST, tuple(prefixes)))
        else:
            return None
    return parsed


def validate_needs(value) -> list | None:
    """Need triples as received, or None if any part of them is malformed."""
    if not isinstance(value, (list, tuple)) or len(value) > MAX_SYNC_NEEDS:
        return None

    parsed: list = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            return None
        lo, hi, prefix = item
        lo = _finite(lo)
        hi = _finite(hi)
        if lo is None or hi is None or lo > hi:
            return None
        if not isinstance(prefix, bytes) or len(prefix) != SYNC_ID_PREFIX_BYTES:
            return None
        parsed.append((lo, hi, prefix))
    return parsed


def pack(value) -> bytes:
    """Ranges or needs, packed for the field that carries them."""
    return msgpack.packb(value, use_bin_type=True)


def packed_size(ranges) -> int:
    """What a description costs on the wire, packed."""
    return len(pack(ranges))


# An upper bound on one fingerprint range packed, used to work out how many of
# them a budget affords. Measured rather than counted by hand, so a change to
# the field layout cannot leave it quietly wrong.
_FINGERPRINT_RANGE_BYTES = packed_size(
    [[1e10, 1e10, RANGE_FINGERPRINT, [10 ** 9, b"\x00" * SYNC_FINGERPRINT_BYTES]]]
)


def unpack_ranges(payload) -> list | None:
    """Validated ranges from a wire payload, or None if unusable."""
    return _unpack(payload, validate_ranges)


def unpack_needs(payload) -> list | None:
    """Validated need triples from a wire payload, or None if unusable."""
    return _unpack(payload, validate_needs)


def _unpack(payload, validator):
    if not isinstance(payload, bytes):
        return None
    try:
        unpacked = unpack_wire(payload)
    except Exception:
        return None
    return validator(unpacked)


def signature(ranges, needs) -> tuple:
    """A comparable summary of what a request asked, to spot a repeat.

    Two requests with the same signature ask the same question, so answering
    the second cannot tell us anything the first did not: that is what bounds
    a narrowing exchange against a peer whose answer never changes.
    """
    return (
        tuple((lo, hi, mode, tuple(payload) if isinstance(payload, (list, tuple))
               else payload)
              for lo, hi, mode, payload in (ranges or [])),
        tuple((lo, hi, prefix) for lo, hi, prefix in (needs or [])),
    )
