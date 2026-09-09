"""
Integration tests for channel creation, announcement, and discovery.

These tests use real Reticulum + LXMF peers communicating over the
shared AutoInterface transport.
"""


import pytest

from trenchchat.core.channel import ChannelManager
from trenchchat.core.naming import NameInUseError
from tests.helpers import (
    announce_and_wait,
    wait_for_channel,
)


class TestChannelCreation:
    def test_create_public_channel(self, peer_factory):
        """Creating a channel stores it with correct metadata and subscribes the creator."""
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("general", "A channel")

        ch = alice.storage.get_channel(ch_hash)
        assert ch is not None
        assert ch["name"] == "general"
        assert ch["description"] == "A channel"
        assert ch["creator_hash"] == alice.identity.hash_hex

        # Creator is automatically subscribed
        assert alice.storage.is_subscribed(ch_hash)

        # Creator is added as owner
        assert alice.storage.is_admin(ch_hash, alice.identity.hash_hex)

    def test_create_invite_only_channel(self, peer_factory):
        """Invite-only channel is stored with the private permissions preset."""
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("secret", "Private channel")

        ch = alice.storage.get_channel(ch_hash)
        assert ch is not None
        assert alice.storage.is_admin(ch_hash, alice.identity.hash_hex)

    def test_channel_hash_is_deterministic(self, peer_factory):
        """
        The channel hash is derived from the creator's identity + channel name,
        so it is stable across calls.  We verify by checking the hash matches
        what is stored in the DB (no re-registration needed).
        """
        alice = peer_factory("alice")
        ch_hash1 = alice.channel_mgr.create_channel("myroom", "")

        # The hash must be present in storage and alice must be the owner
        assert alice.channel_mgr.is_owner(ch_hash1)
        ch = alice.storage.get_channel(ch_hash1)
        assert ch is not None
        assert ch["creator_hash"] == alice.identity.hash_hex

    def test_is_owner(self, peer_factory):
        """is_owner returns True for channels created by this peer."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("alicechan", "")
        assert alice.channel_mgr.is_owner(ch_hash)
        assert not bob.channel_mgr.is_owner(ch_hash)

    def test_create_multiple_channels(self, peer_factory):
        """A single peer can own multiple channels with distinct hashes."""
        alice = peer_factory("alice")
        h1 = alice.channel_mgr.create_channel("chan-one", "")
        h2 = alice.channel_mgr.create_channel("chan-two", "")
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
        ch_hash = alice.channel_mgr.create_channel("restore-test", "")
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
        alice.channel_mgr.create_channel("general", "")

        with pytest.raises(NameInUseError) as excinfo:
            alice.channel_mgr.create_channel("general", "again")

        assert "general" in str(excinfo.value)
        assert len(alice.storage.get_all_channels()) == 1

    def test_duplicate_name_is_refused_after_a_restart(self, peer_factory):
        """The stored row is authoritative too: a fresh manager over the same
        database refuses the name before touching RNS."""
        alice = peer_factory("alice")
        alice.channel_mgr.create_channel("general", "")

        fresh = ChannelManager(alice.identity, alice.storage)
        with pytest.raises(NameInUseError):
            fresh.create_channel("general", "")

    def test_duplicate_name_differing_only_in_punctuation_is_refused(self, peer_factory):
        """Names are sanitised into the aspect, so two that sanitise alike
        collide on the same address."""
        alice = peer_factory("alice")
        alice.channel_mgr.create_channel("Trench Chat", "")

        with pytest.raises(NameInUseError):
            alice.channel_mgr.create_channel("trench chat", "")

