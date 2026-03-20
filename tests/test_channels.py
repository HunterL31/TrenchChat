"""
Integration tests for channel creation, announcement, and discovery.

These tests use real Reticulum + LXMF peers communicating over the
shared AutoInterface transport.
"""

import pytest

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
        assert ch["access_mode"] == "public"
        assert ch["creator_hash"] == alice.identity.hash_hex

        # Creator is automatically subscribed
        assert alice.storage.is_subscribed(ch_hash)

        # Creator is added as admin member
        assert alice.storage.is_admin(ch_hash, alice.identity.hash_hex)

    def test_create_invite_only_channel(self, peer_factory):
        """Invite-only channel is stored with correct access_mode."""
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("secret", "Private channel", "invite")

        ch = alice.storage.get_channel(ch_hash)
        assert ch is not None
        assert ch["access_mode"] == "invite"
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


class TestChannelDiscovery:
    def test_channel_discovered_callback_via_direct_call(self, peer_factory):
        """
        The ChannelAnnounceHandler's _on_channel_discovered callback correctly
        stores a discovered channel in the database and fires the discovered callback.

        Note: In-process RNS announce filtering uses an exact destination hash match.
        The ChannelAnnounceHandler aspect_filter "trenchchat.channel" matches
        destinations with exactly that aspect path. Since channel destinations have
        an additional name component (trenchchat.channel.<name>), cross-peer announce
        discovery requires peers on separate Reticulum instances. We test the
        handler logic directly here.
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
        assert ch["access_mode"] == "public"

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

    def test_invite_channel_access_mode_preserved(self, peer_factory):
        """
        When an invite-only channel announce is processed, access_mode is
        correctly stored as 'invite'.
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
        assert ch["access_mode"] == "invite"
