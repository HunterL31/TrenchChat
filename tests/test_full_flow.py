"""
Full end-to-end functional tests: channel creation -> invite/subscribe ->
messaging -> verified receipt by every party.

Unlike test_channels.py / test_invites.py / test_messaging.py, which each
exercise one subsystem at a time (often wiring up membership or subscriber
state by hand via direct storage calls), these tests drive the *complete*
user-facing flow through the real protocol handlers end to end, and assert
on outcomes visible to every participant -- mirroring what a human using
the GUI would actually experience.

Recipient computation mirrors trenchchat/gui/main_window.py's
_on_send_message: open-join channels deliver to the live subscriber set,
invite-only channels deliver to the members table. tests/helpers.py's
get_subscriber_hashes() implements this same branch and is used throughout
so these tests fail if that boundary ever drifts from the real GUI logic.
"""

import time

import pytest

from tests.helpers import (
    get_subscriber_hashes,
    wait_for,
    wait_for_member,
    wait_for_message,
    wait_for_subscriber,
)


def _join_invite_only_channel(inviter, invitee, ch_hash: str):
    """Drive the real invite -> join_request -> member_list_update handshake."""
    def on_invite(channel_hash_hex, channel_name, token, expiry, admin_hex):
        invitee.invite_mgr.send_join_request(channel_hash_hex, token, expiry, admin_hex)

    invitee.invite_mgr.add_invite_callback(on_invite)
    inviter.invite_mgr.send_invite(ch_hash, invitee.identity.hash_hex)

    assert wait_for_member(inviter.storage, ch_hash, invitee.identity.hash_hex, timeout=5), \
        f"{invitee.name} was not added to {inviter.name}'s member list"
    assert wait_for_member(invitee.storage, ch_hash, invitee.identity.hash_hex, timeout=5), \
        f"{invitee.name} did not receive their own member list update"


class TestPublicChannelFullLifecycle:
    def test_create_subscribe_send_receive_reply(self, peer_factory):
        """
        Alice creates a public channel. Bob and Carol discover it and
        subscribe via the real subscription protocol (not hand-wired
        storage state). Alice sends a message computed via the same
        recipient logic the GUI uses; both Bob and Carol receive it.
        Bob replies; Alice and Carol both receive the reply. At the end,
        all three peers agree on the full message history for the channel.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("town-square", "Public square", "public")

        # Bob and Carol learn about the channel (discovery is exercised
        # separately in test_channels.py) and subscribe for real.
        for peer in (bob, carol):
            peer.storage.upsert_channel(ch_hash, "town-square", "Public square",
                                        alice.identity.hash_hex, "public", time.time())
            peer.subscription_mgr.subscribe(ch_hash, owner_hash_hex=alice.identity.hash_hex)

        assert wait_for_subscriber(alice, ch_hash, bob.identity.hash_hex, timeout=5)
        assert wait_for_subscriber(alice, ch_hash, carol.identity.hash_hex, timeout=5)

        recipients = get_subscriber_hashes(alice, ch_hash)
        assert set(recipients) == {alice.identity.hash_hex, bob.identity.hash_hex,
                                    carol.identity.hash_hex}

        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Hello everyone!",
            subscriber_hashes=recipients,
        )
        greeting_id = alice.storage.get_messages(ch_hash)[0]["message_id"]

        assert wait_for_message(bob.storage, ch_hash, greeting_id, timeout=5), \
            "Bob did not receive Alice's message"
        assert wait_for_message(carol.storage, ch_hash, greeting_id, timeout=5), \
            "Carol did not receive Alice's message"
        assert bob.storage.get_messages(ch_hash)[0]["content"] == "Hello everyone!"
        assert carol.storage.get_messages(ch_hash)[0]["content"] == "Hello everyone!"

        # Bob replies to the group.
        bob_recipients = get_subscriber_hashes(bob, ch_hash)
        bob.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Hi Alice, hi Carol!",
            reply_to=greeting_id,
            subscriber_hashes=bob_recipients,
        )
        reply_id = bob.storage.get_messages(ch_hash)[-1]["message_id"]

        assert wait_for_message(alice.storage, ch_hash, reply_id, timeout=5), \
            "Alice did not receive Bob's reply"
        assert wait_for_message(carol.storage, ch_hash, reply_id, timeout=5), \
            "Carol did not receive Bob's reply"

        # All three parties converge on the same set of messages.
        alice_ids = {m["message_id"] for m in alice.storage.get_messages(ch_hash)}
        bob_ids = {m["message_id"] for m in bob.storage.get_messages(ch_hash)}
        carol_ids = {m["message_id"] for m in carol.storage.get_messages(ch_hash)}
        assert alice_ids == bob_ids == carol_ids == {greeting_id, reply_id}

    def test_non_subscriber_does_not_receive_messages(self, peer_factory):
        """
        Dave never subscribes to Alice's public channel. Messages sent to
        the real subscriber list never reach him.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        dave = peer_factory("dave")

        ch_hash = alice.channel_mgr.create_channel("members-see-this", "", "public")

        bob.storage.upsert_channel(ch_hash, "members-see-this", "",
                                   alice.identity.hash_hex, "public", time.time())
        bob.subscription_mgr.subscribe(ch_hash, owner_hash_hex=alice.identity.hash_hex)
        assert wait_for_subscriber(alice, ch_hash, bob.identity.hash_hex, timeout=5)

        # Dave knows the channel exists (e.g. saw an announce) but never subscribed.
        dave.storage.upsert_channel(ch_hash, "members-see-this", "",
                                    alice.identity.hash_hex, "public", time.time())

        recipients = get_subscriber_hashes(alice, ch_hash)
        assert dave.identity.hash_hex not in recipients

        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Only subscribers see this",
            subscriber_hashes=recipients,
        )
        msg_id = alice.storage.get_messages(ch_hash)[0]["message_id"]
        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5)

        time.sleep(0.5)
        assert len(dave.storage.get_messages(ch_hash)) == 0, \
            "Dave received a message despite never subscribing"


class TestInviteOnlyChannelFullLifecycle:
    def test_invite_multiple_members_then_broadcast(self, peer_factory):
        """
        Alice creates an invite-only channel and invites Bob and Carol
        through the real invite -> join_request -> member_list_update
        handshake (test_invites.py's test_full_invite_flow covers a single
        invitee; this exercises convergence across a full group). Alice
        then sends a message computed via the real members-based recipient
        logic; both Bob and Carol receive it, and each other's membership
        is visible to them via the shared member list.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("inner-circle", "Invite only", "invite")

        _join_invite_only_channel(alice, bob, ch_hash)
        _join_invite_only_channel(alice, carol, ch_hash)

        # Alice's member list now has all three; Bob and Carol each learned
        # about the other via the member_list_update they received on join.
        assert wait_for_member(bob.storage, ch_hash, carol.identity.hash_hex, timeout=5), \
            "Bob never learned that Carol joined"
        assert wait_for_member(carol.storage, ch_hash, bob.identity.hash_hex, timeout=5), \
            "Carol never learned that Bob joined"

        recipients = get_subscriber_hashes(alice, ch_hash)
        assert set(recipients) == {alice.identity.hash_hex, bob.identity.hash_hex,
                                    carol.identity.hash_hex}

        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Welcome to the inner circle",
            subscriber_hashes=recipients,
        )
        msg_id = alice.storage.get_messages(ch_hash)[0]["message_id"]

        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "Bob did not receive Alice's message"
        assert wait_for_message(carol.storage, ch_hash, msg_id, timeout=5), \
            "Carol did not receive Alice's message"

    def test_uninvited_peer_excluded_from_membership_and_messages(self, peer_factory):
        """
        Alice invites Bob only. Dave is never invited: he is not in the
        member list, is excluded from the real recipient computation, and
        never receives a message sent to the group.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        dave = peer_factory("dave")

        ch_hash = alice.channel_mgr.create_channel("invite-only-club", "", "invite")
        _join_invite_only_channel(alice, bob, ch_hash)

        assert not alice.storage.is_member(ch_hash, dave.identity.hash_hex)
        recipients = get_subscriber_hashes(alice, ch_hash)
        assert dave.identity.hash_hex not in recipients

        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Members only",
            subscriber_hashes=recipients,
        )
        msg_id = alice.storage.get_messages(ch_hash)[0]["message_id"]
        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5)

        time.sleep(0.5)
        assert len(dave.storage.get_messages(ch_hash)) == 0, \
            "Dave received a message despite never being invited"

    def test_kicked_member_excluded_from_future_messages(self, peer_factory):
        """
        Alice invites Bob and Carol, then kicks Bob. A subsequent message
        computed via the real (post-kick) recipient list reaches Carol but
        not Bob, even though Bob was a legitimate member moments earlier.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("revocable", "", "invite")
        _join_invite_only_channel(alice, bob, ch_hash)
        _join_invite_only_channel(alice, carol, ch_hash)

        # Sanity: a pre-kick message reaches Bob.
        pre_kick_recipients = get_subscriber_hashes(alice, ch_hash)
        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Before the kick",
            subscriber_hashes=pre_kick_recipients,
        )
        pre_kick_id = alice.storage.get_messages(ch_hash)[0]["message_id"]
        assert wait_for_message(bob.storage, ch_hash, pre_kick_id, timeout=5)

        # Alice (owner) kicks Bob.
        alice.invite_mgr.publish_member_list(ch_hash, remove_members=[bob.identity.hash])
        assert wait_for(
            lambda: not alice.storage.is_member(ch_hash, bob.identity.hash_hex),
            timeout=5,
        ), "Bob was not removed from Alice's member list"
        assert wait_for(
            lambda: not carol.storage.is_member(ch_hash, bob.identity.hash_hex),
            timeout=5,
        ), "Carol did not receive the member list update removing Bob"

        post_kick_recipients = get_subscriber_hashes(alice, ch_hash)
        assert bob.identity.hash_hex not in post_kick_recipients
        assert carol.identity.hash_hex in post_kick_recipients

        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="After the kick",
            subscriber_hashes=post_kick_recipients,
        )
        post_kick_id = alice.storage.get_messages(ch_hash)[-1]["message_id"]

        assert wait_for_message(carol.storage, ch_hash, post_kick_id, timeout=5), \
            "Carol did not receive the post-kick message"

        time.sleep(0.5)
        bob_msgs = [m["message_id"] for m in bob.storage.get_messages(ch_hash)]
        assert post_kick_id not in bob_msgs, \
            "Bob received a message sent after he was kicked"
