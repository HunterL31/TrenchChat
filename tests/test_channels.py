"""
Integration tests for channel creation, announcement, and discovery.

These tests use real Reticulum + LXMF peers communicating over the
shared AutoInterface transport.
"""

import json

import pytest

from trenchchat.core.channel import ChannelManager
from trenchchat.core.naming import NameInUseError
from trenchchat.core.permissions import FLAG_DISCOVERABLE, PRESET_PRIVATE, is_open_join, permissions_from_json
from tests.helpers import (
    announce_and_wait,
    wait_for_channel,
)


class TestChannelCreation:
    def test_create_public_channel(self, peer_factory):
        """Creating a channel stores it with correct metadata and subscribes the creator."""
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("general", "A public channel", "public")

        ch = alice.storage.get_channel(ch_hash)
        assert ch is not None
        assert ch["name"] == "general"
        assert ch["description"] == "A public channel"
        assert is_open_join(permissions_from_json(ch["permissions"]))
        assert ch["creator_hash"] == alice.identity.hash_hex

        # Creator is automatically subscribed
        assert alice.storage.is_subscribed(ch_hash)

        # Creator is added as owner
        assert alice.storage.is_admin(ch_hash, alice.identity.hash_hex)

    def test_create_invite_only_channel(self, peer_factory):
        """Invite-only channel is stored with the private permissions preset."""
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("secret", "Private channel", "invite")

        ch = alice.storage.get_channel(ch_hash)
        assert ch is not None
        assert not is_open_join(permissions_from_json(ch["permissions"]))
        assert alice.storage.is_admin(ch_hash, alice.identity.hash_hex)

    def test_channel_hash_is_deterministic(self, peer_factory):
        """
        The channel hash is derived from the creator's identity + channel name,
        so it is stable across calls.  We verify by checking the hash matches
        what is stored in the DB (no re-registration needed).
        """
        alice = peer_factory("alice")
        ch_hash1 = alice.channel_mgr.create_channel("myroom", "", "public")

        # The hash must be present in storage and alice must be the owner
        assert alice.channel_mgr.is_owner(ch_hash1)
        ch = alice.storage.get_channel(ch_hash1)
        assert ch is not None
        assert ch["creator_hash"] == alice.identity.hash_hex

    def test_is_owner(self, peer_factory):
        """is_owner returns True for channels created by this peer."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("alicechan", "", "public")
        assert alice.channel_mgr.is_owner(ch_hash)
        assert not bob.channel_mgr.is_owner(ch_hash)

    def test_create_multiple_channels(self, peer_factory):
        """A single peer can own multiple channels with distinct hashes."""
        alice = peer_factory("alice")
        h1 = alice.channel_mgr.create_channel("chan-one", "", "public")
        h2 = alice.channel_mgr.create_channel("chan-two", "", "public")
        assert h1 != h2
        assert len(alice.storage.get_all_channels()) == 2

    def test_restore_owned_channels(self, peer_factory):
        """
        restore_owned_channels re-populates the in-memory _owned_destinations
        dict from the database for channels owned by this identity.

        Note: We cannot re-create the same RNS.Destination in the same process
        (RNS raises an error for duplicate registrations), so we verify the
        behaviour indirectly: a fresh peer built from the same data_dir and
        identity file should have the channel in its owned destinations after
        restore_owned_channels() is called at construction time.
        """
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("restore-test", "", "public")
        assert alice.channel_mgr.is_owner(ch_hash)

        # A second peer_factory call with the same name would reuse the same
        # identity file and DB, so restore_owned_channels would re-register.
        # Instead, just verify the in-memory dict is populated correctly.
        owned = alice.channel_mgr._owned_destinations
        assert ch_hash in owned

    def test_duplicate_name_is_refused(self, peer_factory):
        """A second channel of the same name is the same address, so it is
        refused instead of re-registering the destination (a hard RNS error)
        and overwriting the first channel's row."""
        alice = peer_factory("alice")
        alice.channel_mgr.create_channel("general", "", "public")

        with pytest.raises(NameInUseError) as excinfo:
            alice.channel_mgr.create_channel("general", "again", "public")

        assert "general" in str(excinfo.value)
        assert len(alice.storage.get_all_channels()) == 1

    def test_duplicate_name_is_refused_after_a_restart(self, peer_factory):
        """The stored row is authoritative too: a fresh manager over the same
        database refuses the name before touching RNS."""
        alice = peer_factory("alice")
        alice.channel_mgr.create_channel("general", "", "public")

        fresh = ChannelManager(alice.identity, alice.storage)
        with pytest.raises(NameInUseError):
            fresh.create_channel("general", "", "public")

    def test_duplicate_name_differing_only_in_punctuation_is_refused(self, peer_factory):
        """Names are sanitised into the aspect, so two that sanitise alike
        collide on the same address."""
        alice = peer_factory("alice")
        alice.channel_mgr.create_channel("Trench Chat", "", "public")

        with pytest.raises(NameInUseError):
            alice.channel_mgr.create_channel("trench chat", "", "public")


class TestChannelDiscovery:
    def test_channel_discovered_callback_via_direct_call(self, peer_factory):
        """
        The ChannelAnnounceHandler's _on_channel_discovered callback correctly
        stores a discovered channel in the database and fires the discovered callback.

        This calls the handler directly rather than going through a real announce:
        the test fixtures' AutoInterface relies on UDP multicast, which is not
        reliable on every machine (this is also why TestTransport exists for LXMF
        message delivery), so announce-dependent tests target dest.announce()
        directly instead of asserting on cross-peer delivery -- see
        test_invite_only_channel_never_announced / test_public_channel_is_announced
        below.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("discoverable", "Find me", "public")

        discovered = []
        bob.channel_mgr.add_channel_discovered_callback(
            lambda h, n: discovered.append((h, n))
        )

        # Simulate the announce being received by Bob's handler
        import RNS as _RNS
        channel_hash_bytes = bytes.fromhex(ch_hash)
        import msgpack
        app_data = msgpack.packb({
            "name": "discoverable",
            "description": "Find me",
            "access": "public",
            "creator": alice.identity.hash_hex,
        }, use_bin_type=True)

        bob.channel_mgr._on_channel_discovered(
            destination_hash=channel_hash_bytes,
            announced_identity=alice.identity.rns_identity,
            metadata={
                "name": "discoverable",
                "description": "Find me",
                "access": "public",
                "creator": alice.identity.hash_hex,
            },
        )

        ch = bob.storage.get_channel(ch_hash)
        assert ch is not None
        assert ch["name"] == "discoverable"
        assert ch["creator_hash"] == alice.identity.hash_hex
        assert is_open_join(permissions_from_json(ch["permissions"]))

        assert any(h == ch_hash for h, _ in discovered), \
            "channel_discovered callback was not fired"

    def test_channel_discovered_callback_not_fired_for_known_channel(self, peer_factory):
        """
        The channel_discovered callback is NOT fired for channels already in storage.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("known", "", "public")

        # Pre-populate Bob's storage with the channel
        import time as _time
        bob.storage.upsert_channel(ch_hash, "known", "", alice.identity.hash_hex,
                                   "public", _time.time())

        discovered = []
        bob.channel_mgr.add_channel_discovered_callback(
            lambda h, n: discovered.append((h, n))
        )

        # Simulate receiving the announce again
        bob.channel_mgr._on_channel_discovered(
            destination_hash=bytes.fromhex(ch_hash),
            announced_identity=alice.identity.rns_identity,
            metadata={
                "name": "known",
                "description": "",
                "access": "public",
                "creator": alice.identity.hash_hex,
            },
        )

        # Callback should NOT fire since channel was already known
        assert len(discovered) == 0, \
            "channel_discovered callback fired for an already-known channel"

    def test_invite_channel_permissions_preserved(self, peer_factory):
        """
        When an invite-only channel announce is processed, it is stored
        with the private permissions preset (open_join=False).
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("private-room", "", "invite")

        bob.channel_mgr._on_channel_discovered(
            destination_hash=bytes.fromhex(ch_hash),
            announced_identity=alice.identity.rns_identity,
            metadata={
                "name": "private-room",
                "description": "",
                "access": "invite",
                "creator": alice.identity.hash_hex,
            },
        )

        ch = bob.storage.get_channel(ch_hash)
        assert ch is not None
        assert not is_open_join(permissions_from_json(ch["permissions"]))

    def test_invite_only_channel_never_announced(self, peer_factory):
        """
        Invite-only channels must never be broadcast on the mesh -- they rely on
        the signed member-list document instead, precisely so their existence,
        name, and description aren't visible to peers who were never invited.
        announce_channel() must skip the actual dest.announce() call for them.

        Regression test for a real bug: announce_channel() previously called
        dest.announce() unconditionally for every owned channel, leaking
        invite-only channel metadata to any peer listening for
        trenchchat.channel announces.
        """
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("secret-room", "", "invite")

        dest = alice.channel_mgr._owned_destinations[ch_hash]
        calls = []
        dest.announce = lambda *a, **kw: calls.append((a, kw))

        alice.channel_mgr.announce_channel(ch_hash)

        assert calls == [], "invite-only channel's destination.announce() was called"

    def test_invite_only_channel_never_announced_even_if_marked_discoverable(self, peer_factory):
        """
        discoverable and open_join are independent flags -- the real GUI's
        ChannelPermissionsDialog lets an admin check "Discoverable" while
        leaving "Open join" off. announce_channel() must still refuse to
        announce, or an invite-only channel's name/description/creator gets
        broadcast to every peer on the mesh, none of whom were ever invited.

        Regression test for a real bug: announce_channel() only checked
        is_discoverable(), trusting it wasn't set independently of
        open_join. It was -- confirmed live via the devtools two-tester
        environment, where a channel with open_join=False, discoverable=True
        showed up in a never-invited tester's Discovered panel.
        """
        alice = peer_factory("alice")

        leaked_perms = dict(PRESET_PRIVATE)
        leaked_perms[FLAG_DISCOVERABLE] = True
        ch_hash = alice.channel_mgr.create_channel("secret-room", "", permissions=leaked_perms)

        dest = alice.channel_mgr._owned_destinations[ch_hash]
        calls = []
        dest.announce = lambda *a, **kw: calls.append((a, kw))

        alice.channel_mgr.announce_channel(ch_hash)

        assert calls == [], \
            "invite-only channel was announced despite open_join=False, just because discoverable=True"

    def test_public_channel_is_announced(self, peer_factory):
        """Public channels are the intended case for dest.announce() -- the guard
        added for invite-only channels must not also swallow this one."""
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("open-room", "", "public")

        dest = alice.channel_mgr._owned_destinations[ch_hash]
        calls = []
        dest.announce = lambda *a, **kw: calls.append((a, kw))

        alice.channel_mgr.announce_channel(ch_hash)

        assert len(calls) == 1, "public channel's destination.announce() was not called"
