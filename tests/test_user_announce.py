"""
The trenchchat.user announce is a capability beacon, not a name carrier.

The display name travels in the lxmf.delivery announce, where every LXMF
client publishes and reads it; the user announce only says the identity runs
TrenchChat. These pin that split, the legacy fallback for peers that still put
a name in the user announce, and the heartbeat cadence being in line with
other LXMF clients.
"""

import msgpack
import pytest
import RNS

from trenchchat.network.announce import UserAnnounceHandler, lxmf_display_name
from trenchchat.network.router import REANNOUNCE_INTERVAL_SECS

# recall_app_data touches the running Reticulum instance.
pytestmark = pytest.mark.usefixtures("rns_instance")


def _remember_delivery_announce(identity: RNS.Identity, app_data: bytes) -> None:
    delivery_hash = RNS.Destination.hash(identity.hash, "lxmf", "delivery")
    RNS.Identity.remember(b"\x01" * 32, delivery_hash, identity.get_public_key(), app_data)


def _lxmf_app_data(name: str) -> bytes:
    return msgpack.packb([name.encode("utf-8"), None])


def _legacy_user_app_data(name: str) -> bytes:
    return msgpack.packb({"name": name}, use_bin_type=True)


def _hear_user_announce(identity: RNS.Identity, app_data: bytes) -> list:
    seen = []
    handler = UserAnnounceHandler(lambda peer_hex, name, iface: seen.append((peer_hex, name)))
    handler.received_announce(b"\x00" * 16, identity, app_data, b"")
    return seen


def test_name_comes_from_the_delivery_announce():
    identity = RNS.Identity()
    _remember_delivery_announce(identity, _lxmf_app_data("Alice"))

    assert _hear_user_announce(identity, b"") == [(identity.hash.hex(), "Alice")]


def test_delivery_name_wins_over_a_legacy_user_announce_name():
    identity = RNS.Identity()
    _remember_delivery_announce(identity, _lxmf_app_data("Alice"))

    seen = _hear_user_announce(identity, _legacy_user_app_data("Old Alice"))

    assert seen == [(identity.hash.hex(), "Alice")]


def test_legacy_user_announce_name_fills_in_before_any_delivery_announce():
    identity = RNS.Identity()

    seen = _hear_user_announce(identity, _legacy_user_app_data("Alice"))

    assert seen == [(identity.hash.hex(), "Alice")]


def test_an_unnamed_peer_is_still_reported():
    identity = RNS.Identity()

    assert _hear_user_announce(identity, b"") == [(identity.hash.hex(), "")]


def test_original_lxmf_announce_format_is_read():
    identity = RNS.Identity()
    _remember_delivery_announce(identity, b"Plain Bob")

    assert lxmf_display_name(identity.hash) == "Plain Bob"


def test_unreadable_delivery_app_data_is_no_name():
    identity = RNS.Identity()
    _remember_delivery_announce(identity, b"\x93\xff")

    assert lxmf_display_name(identity.hash) == ""


def test_user_announce_carries_no_payload(peer_factory, monkeypatch):
    peer = peer_factory("beacon")
    announced = []

    def _record(self, app_data=None, **kwargs):
        announced.append((self.hash, app_data))

    monkeypatch.setattr(RNS.Destination, "announce", _record)
    peer.router.announce_user()

    assert len(announced) == 1
    assert announced[0][1] is None


def test_heartbeat_is_in_line_with_other_lxmf_clients():
    assert REANNOUNCE_INTERVAL_SECS >= 3600
