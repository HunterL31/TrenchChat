"""
Tests for trenchchat.core.actions -- the shared entry points both
main_window.py and devtools/testenv/api.py call, so a caller with no other
feedback loop (an HTTP response, a scripted test) can tell success apart
from a silently-filtered request.
"""

import time

from tests.helpers import wait_for_member
from trenchchat.core import actions
from trenchchat.core.permissions import (
    FLAG_DISCOVERABLE, FLAG_OPEN_JOIN, PRESET_OPEN, PRESET_PRIVATE, ROLE_MEMBER, ROLE_OWNER,
)


def _setup_channel_with_member(peer_factory, *, member_perms=None):
    """Create alice (owner) and bob (member) on a shared invite-only channel."""
    alice = peer_factory("alice")
    bob = peer_factory("bob")

    perms = dict(PRESET_PRIVATE)
    if member_perms is not None:
        perms[ROLE_MEMBER] = list(member_perms)

    ch_hash = alice.channel_mgr.create_channel("test-ch", "", permissions=perms)
    alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])
    assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)

    bob.storage.upsert_channel(ch_hash, "test-ch", "", alice.identity.hash_hex,
                               perms, time.time())
    bob.storage.subscribe(ch_hash)
    bob.storage.upsert_member(ch_hash, bob.identity.hash_hex, "Bob", role=ROLE_MEMBER)
    bob.storage.upsert_member(ch_hash, alice.identity.hash_hex, "Alice", role=ROLE_OWNER)
    bob.storage.set_channel_permissions(ch_hash, perms)

    return alice, bob, ch_hash


class TestUpdateMembership:
    def test_authorized_kick_returns_true_and_applies(self, peer_factory):
        """Owner (has KICK) removing a member returns True and the removal sticks."""
        alice, bob, ch_hash = _setup_channel_with_member(peer_factory)

        applied = actions.update_membership(
            alice.storage, alice.invite_mgr, ch_hash, alice.identity.hash_hex,
            remove_members=[bob.identity.hash],
        )

        assert applied is True
        assert not alice.storage.is_member(ch_hash, bob.identity.hash_hex)

    def test_unauthorized_kick_returns_false_and_is_a_noop(self, peer_factory):
        """
        Regression test: update_membership() used to return None unconditionally,
        so an API endpoint wrapping it (devtools' POST .../roles) always reported
        {"ok": true} even when the actor lacked permission and nothing happened --
        indistinguishable from a real success. It must now report the drop.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[]  # no KICK
        )
        carol = peer_factory("carol")
        alice.invite_mgr.publish_member_list(ch_hash, add_members=[carol.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, carol.identity.hash_hex)

        applied = actions.update_membership(
            bob.storage, bob.invite_mgr, ch_hash, bob.identity.hash_hex,
            remove_members=[carol.identity.hash],
        )

        assert applied is False
        assert alice.storage.is_member(ch_hash, carol.identity.hash_hex), \
            "Bob removed Carol despite lacking KICK permission"

    def test_no_changes_requested_returns_false(self, peer_factory):
        """Calling with nothing to do is a no-op, not an implicit success."""
        alice, _bob, ch_hash = _setup_channel_with_member(peer_factory)

        applied = actions.update_membership(
            alice.storage, alice.invite_mgr, ch_hash, alice.identity.hash_hex,
        )

        assert applied is False


class TestJoinPublicChannel:
    def test_open_join_channel_can_be_joined(self, peer_factory):
        """Sanity check: subscribing to a genuinely open-join channel works."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("public-room", "", permissions=PRESET_OPEN)
        bob.storage.upsert_channel(ch_hash, "public-room", "", alice.identity.hash_hex,
                                   PRESET_OPEN, time.time())

        joined = actions.join_public_channel(bob.storage, bob.subscription_mgr, ch_hash)

        assert joined is True
        assert bob.storage.is_subscribed(ch_hash)

    def test_invite_only_channel_cannot_be_self_joined(self, peer_factory):
        """
        A locally-known invite-only channel must never be joinable via a bare
        subscribe, even if a row for it somehow exists in local storage --
        membership there is only ever granted through a signed member-list
        document from an admin/owner.

        Regression test for a real bug: ChannelPermissionsDialog lets
        discoverable and open_join be toggled independently, so an
        invite-only channel could be marked discoverable, get announced
        (see the announce_channel fix for that half), and a peer who merely
        *heard* about it that way -- never invited -- could then call this
        and self-admit. join_public_channel must refuse regardless of how
        the row got into local storage.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        # Simulates Bob having discovered an invite-only channel's metadata
        # (e.g. via a leaked announce) without ever being invited.
        leaked_perms = dict(PRESET_PRIVATE)
        leaked_perms[FLAG_DISCOVERABLE] = True
        assert leaked_perms[FLAG_OPEN_JOIN] is False

        ch_hash = alice.channel_mgr.create_channel("secret-room", "", permissions=leaked_perms)
        bob.storage.upsert_channel(ch_hash, "secret-room", "", alice.identity.hash_hex,
                                   leaked_perms, time.time())

        joined = actions.join_public_channel(bob.storage, bob.subscription_mgr, ch_hash)

        assert joined is False
        assert not bob.storage.is_subscribed(ch_hash), \
            "Bob self-joined an invite-only channel via a bare subscribe"
