"""
The RRC wire format is an interop contract, not ours to change.

Every number asserted here comes from the RRC specification
(https://rrc.kc1awv.net/, document 3) or rrcd's EX1-RRCD extension document.
A failure means either a typo or a deliberate protocol change, and a
deliberate change breaks every other RRC client and hub, so it is never the
right way to make this file pass.

The rest of the file is the other half of the contract: unpack_envelope is
the only thing standing between a hostile hub and the rest of the client, so
it is fed everything a hub should not be able to send.
"""

import cbor2
import pytest

from trenchchat.core import rrc_wire as w


class TestSpecNumbers:
    """The assignments in specification document 3, table by table."""

    def test_envelope_keys(self):
        assert (w.K_V, w.K_T, w.K_ID, w.K_TS, w.K_SRC) == (0, 1, 2, 3, 4)
        assert (w.K_ROOM, w.K_BODY, w.K_NICK, w.K_DST) == (5, 6, 7, 8)

    def test_message_types(self):
        assert (w.T_HELLO, w.T_WELCOME) == (1, 2)
        assert (w.T_JOIN, w.T_JOINED, w.T_PART, w.T_PARTED) == (10, 11, 12, 13)
        assert (w.T_MSG, w.T_NOTICE, w.T_ACTION) == (20, 21, 22)
        assert (w.T_PING, w.T_PONG) == (30, 31)
        assert w.T_ERROR == 40
        assert w.T_RESOURCE_ENVELOPE == 50

    def test_body_and_capability_keys(self):
        assert (w.B_NAME, w.B_VERSION, w.B_CAPS, w.B_LIMITS) == (0, 1, 2, 3)
        assert (w.CAP_RESOURCE_ENVELOPE, w.CAP_ACTION, w.CAP_DIRECT_NOTICE) == (0, 1, 2)

    def test_resource_envelope_body_keys(self):
        assert (w.B_RES_ID, w.B_RES_KIND, w.B_RES_SIZE) == (0, 1, 2)
        assert (w.B_RES_SHA256, w.B_RES_ENCODING) == (3, 4)
        assert w.RES_KINDS == ("notice", "motd", "blob")

    def test_hub_limit_names(self):
        assert w.LIMIT_NICK_BYTES == "max_nick_bytes"
        assert w.LIMIT_ROOMS_PER_SESSION == "max_rooms_per_session"
        assert w.LIMIT_ROOM_NAME_BYTES == "max_room_name_bytes"
        assert w.LIMIT_MSG_BODY_BYTES == "max_msg_body_bytes"
        assert w.LIMIT_MSGS_PER_MINUTE == "rate_limit_msgs_per_minute"

    def test_hub_destination_aspect(self):
        assert f"{w.HUB_APP_NAME}.{w.HUB_ASPECT}" == "rrc.hub"

    def test_message_id_is_eight_bytes(self):
        assert w.MESSAGE_ID_BYTES == 8
        assert len(w.new_message_id()) == 8


class TestEncoding:
    """What goes on the wire is a CBOR map with unsigned integer keys."""

    def test_envelope_is_a_cbor_map_keyed_by_integers(self):
        raw = w.pack_envelope(w.T_MSG, body="hi", room="#general", src=b"\x01" * 16)
        decoded = cbor2.loads(raw)
        assert isinstance(decoded, dict)
        assert all(isinstance(key, int) for key in decoded)
        assert decoded[w.K_V] == w.RRC_VERSION
        assert decoded[w.K_T] == w.T_MSG
        assert decoded[w.K_ROOM] == "#general"
        assert decoded[w.K_BODY] == "hi"

    def test_the_fixed_keys_are_always_present(self):
        decoded = cbor2.loads(w.pack_envelope(w.T_PING, src=b"\x02" * 16))
        for key in (w.K_V, w.K_T, w.K_ID, w.K_TS, w.K_SRC):
            assert key in decoded

    def test_optional_keys_are_absent_when_unset(self):
        decoded = cbor2.loads(w.pack_envelope(w.T_PING, src=b"\x02" * 16))
        for key in (w.K_ROOM, w.K_BODY, w.K_NICK, w.K_DST):
            assert key not in decoded

    def test_a_full_length_message_still_fits_one_packet(self):
        """The body cap is sized against the slowest link, not the fastest.

        A room name, a nickname and a maximum-length body together have to
        fit the 465 bytes Reticulum leaves at its default MTU with 32-byte
        addresses, or a full message stops being one packet on a LoRa link.
        """
        raw = w.pack_envelope(
            w.T_MSG,
            body="x" * w.MAX_MSG_BODY_BYTES,
            room="#" + "r" * (w.MAX_ROOM_NAME_BYTES - 1),
            nick="n" * w.MAX_NICK_BYTES,
            src=b"\x03" * 16,
        )
        assert len(raw) <= 465

    @pytest.mark.parametrize("msg_type", [
        w.T_HELLO, w.T_WELCOME, w.T_JOIN, w.T_JOINED, w.T_PART, w.T_PARTED,
        w.T_MSG, w.T_NOTICE, w.T_ACTION, w.T_PING, w.T_PONG, w.T_ERROR,
        w.T_RESOURCE_ENVELOPE,
    ])
    def test_every_message_type_round_trips(self, msg_type):
        msg_id = w.new_message_id()
        raw = w.pack_envelope(msg_type, body="body", src=b"\x04" * 16,
                              msg_id=msg_id, timestamp_ms=1700000000000)
        out = w.unpack_envelope(raw)
        assert out is not None
        assert out[w.K_T] == msg_type
        assert out[w.K_ID] == msg_id
        assert out[w.K_TS] == 1700000000000
        assert w.body_text(out) == "body"

    def test_room_and_direct_destination_are_mutually_exclusive(self):
        with pytest.raises(ValueError):
            w.pack_envelope(w.T_NOTICE, room="#general", dst=b"\x05" * 16)

    def test_packing_refuses_a_bad_message_id(self):
        with pytest.raises(ValueError):
            w.pack_envelope(w.T_MSG, msg_id=b"short")

    @pytest.mark.parametrize("room", ["general", "", "#", "#a\nb", "#" + "r" * 200])
    def test_packing_refuses_an_invalid_room(self, room):
        with pytest.raises(ValueError):
            w.pack_envelope(w.T_MSG, room=room)

    def test_packing_refuses_an_invalid_nickname(self):
        with pytest.raises(ValueError):
            w.pack_envelope(w.T_MSG, room="#general", nick="bad\x00nick")


class TestRoomNames:
    def test_normalise_adds_the_hash_and_lowercases(self):
        assert w.normalise_room("General") == "#general"
        assert w.normalise_room("  #LoRa  ") == "#lora"

    def test_normalise_leaves_an_already_normal_name_alone(self):
        assert w.normalise_room("#general") == "#general"

    def test_a_room_name_arrives_case_folded(self):
        """Hubs treat room names case-insensitively, so two clients that
        type the name differently must land in the same room."""
        raw = cbor2.dumps({
            w.K_V: 1, w.K_T: w.T_MSG, w.K_ID: b"\x00" * 8,
            w.K_TS: w.now_ms(), w.K_SRC: b"\x06" * 16,
            w.K_ROOM: "#General", w.K_BODY: "hi",
        })
        assert w.unpack_envelope(raw)[w.K_ROOM] == "#general"


class TestUnpackRefusals:
    """unpack_envelope is the trust boundary: it never raises, and it
    returns None rather than passing anything on that it cannot vouch for."""

    def _envelope(self, overrides: dict | None = None) -> bytes:
        fields = {
            w.K_V: 1, w.K_T: w.T_MSG, w.K_ID: b"\x00" * 8,
            w.K_TS: w.now_ms(), w.K_SRC: b"\x07" * 16, w.K_BODY: "hi",
        }
        fields.update(overrides or {})
        return cbor2.dumps(fields)

    @pytest.mark.parametrize("data", [b"", b"\xff\xff\xff", b"not cbor at all", None, 42])
    def test_garbage_is_refused_without_raising(self, data):
        assert w.unpack_envelope(data) is None

    def test_a_truncated_envelope_is_refused(self):
        raw = self._envelope()
        for cut in range(1, len(raw)):
            assert w.unpack_envelope(raw[:cut]) is None or cut == len(raw)

    def test_an_oversized_envelope_is_refused_before_decoding(self):
        raw = self._envelope({w.K_BODY: "x" * (w.MAX_ENVELOPE_BYTES * 2)})
        assert len(raw) > w.MAX_ENVELOPE_BYTES
        assert w.unpack_envelope(raw) is None

    def test_a_non_map_payload_is_refused(self):
        assert w.unpack_envelope(cbor2.dumps([1, 2, 3])) is None
        assert w.unpack_envelope(cbor2.dumps("a string")) is None

    @pytest.mark.parametrize("bad", ["1", None, True, -1, 1.5])
    def test_a_non_integer_message_type_is_refused(self, bad):
        assert w.unpack_envelope(self._envelope({w.K_T: bad})) is None

    def test_a_missing_version_or_type_is_refused(self):
        assert w.unpack_envelope(cbor2.dumps({w.K_T: w.T_MSG})) is None
        assert w.unpack_envelope(cbor2.dumps({w.K_V: 1})) is None

    def test_an_unknown_message_type_decodes_and_is_left_to_the_caller(self):
        """Forward compatibility: a client must ignore a type it does not
        know, which it can only do if the envelope still parses."""
        out = w.unpack_envelope(self._envelope({w.K_T: 199}))
        assert out is not None
        assert out[w.K_T] == 199

    def test_an_invalid_room_name_drops_the_whole_envelope(self):
        assert w.unpack_envelope(self._envelope({w.K_ROOM: "no-hash"})) is None
        assert w.unpack_envelope(self._envelope({w.K_ROOM: "#with\nnewline"})) is None
        assert w.unpack_envelope(self._envelope({w.K_ROOM: 17})) is None

    def test_room_and_direct_destination_together_are_refused(self):
        raw = self._envelope({w.K_ROOM: "#general", w.K_DST: b"\x08" * 16})
        assert w.unpack_envelope(raw) is None

    def test_a_bad_nickname_is_dropped_but_the_message_survives(self):
        """A nickname is advisory, so losing it beats losing the line."""
        out = w.unpack_envelope(self._envelope({w.K_NICK: "bad\x07nick"}))
        assert out is not None
        assert w.K_NICK not in out
        assert w.body_text(out) == "hi"

    def test_an_oversized_nickname_is_dropped(self):
        out = w.unpack_envelope(self._envelope({w.K_NICK: "n" * 200}))
        assert out is not None and w.K_NICK not in out

    def test_a_wrong_length_message_id_is_dropped(self):
        out = w.unpack_envelope(self._envelope({w.K_ID: b"\x00" * 4}))
        assert out is not None and w.K_ID not in out

    def test_an_oversized_sender_hash_is_dropped(self):
        out = w.unpack_envelope(self._envelope({w.K_SRC: b"\x09" * 500}))
        assert out is not None and w.K_SRC not in out

    def test_a_far_future_timestamp_is_dropped(self):
        """Unbounded, it pins a line to the top of the transcript for as
        long as the session lasts."""
        out = w.unpack_envelope(self._envelope({w.K_TS: w.now_ms() + 10**12}))
        assert out is not None and w.K_TS not in out

    @pytest.mark.parametrize("bad", [-1, "yesterday", 1.5, True])
    def test_an_implausible_timestamp_is_dropped(self, bad):
        out = w.unpack_envelope(self._envelope({w.K_TS: bad}))
        assert out is not None and w.K_TS not in out

    def test_a_body_carrying_a_cbor_tag_is_refused(self):
        """CBOR tags decode to arbitrary Python objects. None of them appear
        in an RRC body, so they never reach a caller expecting text."""
        raw = self._envelope({w.K_BODY: cbor2.CBORTag(1, 1700000000)})
        assert w.unpack_envelope(raw) is None

    def test_a_deeply_nested_body_is_refused(self):
        body = "leaf"
        for _ in range(12):
            body = [body]
        assert w.unpack_envelope(self._envelope({w.K_BODY: body})) is None

    def test_unknown_envelope_keys_are_ignored_not_returned(self):
        out = w.unpack_envelope(self._envelope({99: "future field"}))
        assert out is not None
        assert 99 not in out

    def test_body_text_of_a_non_text_body_reads_as_empty(self):
        out = w.unpack_envelope(self._envelope({w.K_BODY: {1: 2}}))
        assert out is not None
        assert w.body_text(out) == ""


class TestCapabilities:
    def _hello(self, caps) -> dict:
        raw = w.pack_envelope(w.T_HELLO, src=b"\x0a" * 16, body={
            w.B_NAME: "TrenchChat", w.B_VERSION: "0.1.0", w.B_CAPS: caps,
        })
        return w.unpack_envelope(raw)

    def test_a_capability_map_is_read(self):
        caps = self._hello({w.CAP_ACTION: True, w.CAP_DIRECT_NOTICE: True})
        assert w.capabilities_of(caps) == {w.CAP_ACTION: True, w.CAP_DIRECT_NOTICE: True}

    def test_a_capability_list_is_read_as_a_map(self):
        """Some rrcd clients have shipped capabilities as a list."""
        caps = self._hello([w.CAP_ACTION, w.CAP_RESOURCE_ENVELOPE])
        assert w.capabilities_of(caps) == {
            w.CAP_ACTION: True, w.CAP_RESOURCE_ENVELOPE: True,
        }

    def test_a_missing_or_malformed_capability_map_reads_as_none(self):
        assert w.capabilities_of({}) == {}
        assert w.capabilities_of(self._hello("nonsense")) == {}

    def test_a_named_capability_map_is_read(self):
        """Specification document 3 leaves B_CAPS open and its own wording
        describes string keys, so a conformant peer may name them. Reading
        only numbers would see such a peer as supporting nothing."""
        caps = self._hello({"action": True, "resource_envelope": False})
        assert w.capabilities_of(caps) == {
            w.CAP_ACTION: True, w.CAP_RESOURCE_ENVELOPE: False,
        }

    def test_a_named_capability_list_is_read(self):
        caps = self._hello(["action", "direct_notice"])
        assert w.capabilities_of(caps) == {
            w.CAP_ACTION: True, w.CAP_DIRECT_NOTICE: True,
        }

    def test_a_name_is_matched_regardless_of_case_and_padding(self):
        assert w.capabilities_of(self._hello({" ACTION ": True})) == \
            {w.CAP_ACTION: True}

    def test_a_name_nobody_has_defined_is_ignored_not_guessed_at(self):
        caps = self._hello({"telepathy": True, "action": True})
        assert w.capabilities_of(caps) == {w.CAP_ACTION: True}

    def test_what_goes_out_carries_both_spellings(self):
        """A peer that reads numbers and one that reads names must both see
        the same answer, and neither may be told we support something extra."""
        both = w.advertise_capabilities({w.CAP_ACTION: True})
        assert both == {w.CAP_ACTION: True, "action": True}
        assert w.capabilities_of({w.K_BODY: {w.B_CAPS: both}}) == \
            {w.CAP_ACTION: True}

    def test_every_capability_number_has_exactly_one_name(self):
        assert set(w.CAP_NAMES) == {
            w.CAP_RESOURCE_ENVELOPE, w.CAP_ACTION, w.CAP_DIRECT_NOTICE,
        }
        assert len(set(w.CAP_NAMES.values())) == len(w.CAP_NAMES)


class TestHubLimits:
    def _welcome(self, limits) -> dict:
        raw = w.pack_envelope(w.T_WELCOME, src=b"\x0b" * 16, body={
            w.B_NAME: "hub", w.B_VERSION: "0.1", w.B_CAPS: {},
            w.B_LIMITS: limits,
        })
        return w.unpack_envelope(raw)

    def test_advertised_limits_are_taken(self):
        limits = w.limits_of(self._welcome({w.LIMIT_MSG_BODY_BYTES: 200}))
        assert limits[w.LIMIT_MSG_BODY_BYTES] == 200

    def test_an_unadvertised_limit_falls_back_to_ours(self):
        """A hub need not advertise any of these, and a missing one must
        not read as no limit at all."""
        limits = w.limits_of(self._welcome({w.LIMIT_MSG_BODY_BYTES: 200}))
        assert limits[w.LIMIT_NICK_BYTES] == w.MAX_NICK_BYTES
        assert w.limits_of({}) == w.DEFAULT_LIMITS

    @pytest.mark.parametrize("value", [0, -5, 10**9, "lots", True])
    def test_an_implausible_limit_does_not_size_our_buffers(self, value):
        limits = w.limits_of(self._welcome({w.LIMIT_MSG_BODY_BYTES: value}))
        assert limits[w.LIMIT_MSG_BODY_BYTES] == w.MAX_MSG_BODY_BYTES
