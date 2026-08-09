"""
Tests for trenchchat.core.actions -- the shared entry points both
main_window.py and devtools/testenv/api.py call, so a caller with no other
feedback loop (an HTTP response, a scripted test) can tell success apart
from a silently-filtered request.
"""

import time

from tests.helpers import wait_for_member
from trenchchat.core import actions
from trenchchat.core.permissions import PRESET_PRIVATE, ROLE_MEMBER, ROLE_OWNER


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
