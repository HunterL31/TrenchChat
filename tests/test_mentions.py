"""Mentions: reading a ping out of the message text that carries it.

A mention claims no protocol field, so these are the whole wire contract.
What they guard is that a hash is read as a mention only when it really names
one identity: an over-eager match pings somebody who was never addressed, and
a missed one loses the address entirely.
"""

import time

from tests.helpers import wait_for_message
from trenchchat.core.authorship import sign_message, verify_message
from trenchchat.core.protocol import (
    MAX_MENTIONS_PER_MESSAGE,
    mention_token,
    mentions_identity,
    mentions_in,
)

ALICE = "aa" * 16
BOB = "bb" * 16
ME = "cc" * 16


class TestMentionsIn:
    def test_reads_a_mention_out_of_ordinary_text(self):
        assert mentions_in(f"morning {mention_token(ALICE)}, ready?") == [ALICE]

    def test_reads_several_in_the_order_written(self):
        content = f"{mention_token(BOB)} and {mention_token(ALICE)}"
        assert mentions_in(content) == [BOB, ALICE]

    def test_the_same_identity_twice_is_one_mention(self):
        content = f"{mention_token(ALICE)} {mention_token(ALICE)}"
        assert mentions_in(content) == [ALICE]

    def test_text_with_no_at_sign_holds_no_mention(self):
        assert mentions_in("nothing to see") == []

    def test_a_longer_hex_run_names_somebody_else(self):
        assert mentions_in(f"@{ALICE}ffffffff") == []

    def test_a_shorter_hex_run_is_not_a_mention(self):
        assert mentions_in("@abc123") == []

    def test_upper_case_hex_names_the_same_identity(self):
        assert mentions_in(f"@{ALICE.upper()}") == [ALICE]

    def test_a_mention_next_to_punctuation_still_counts(self):
        assert mentions_in(f"(cc {mention_token(BOB)})") == [BOB]

    def test_what_is_read_back_is_bounded(self):
        many = " ".join(
            mention_token(f"{n:032x}") for n in range(MAX_MENTIONS_PER_MESSAGE + 10)
        )
        assert len(mentions_in(many)) == MAX_MENTIONS_PER_MESSAGE


class TestMentionsIdentity:
    def test_true_for_the_identity_named(self):
        assert mentions_identity(f"hi {mention_token(ME)}", ME) is True

    def test_false_for_an_identity_not_named(self):
        assert mentions_identity(f"hi {mention_token(ALICE)}", ME) is False

    def test_a_hash_that_only_starts_a_longer_run_is_not_a_mention(self):
        assert mentions_identity(f"@{ME}deadbeef", ME) is False

    def test_an_empty_identity_is_never_mentioned(self):
        assert mentions_identity(f"@{ME}", "") is False


class TestMentionsOnTheWire:
    """A mention is text, so every path a message already takes carries it.

    These cover the two that would otherwise need handling of their own: the
    author signature, which covers the content and so covers the ping inside
    it, and delivery, which relays the content verbatim.
    """

    def test_a_relay_cannot_add_a_ping_to_a_signed_message(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        digest = dict(
            channel_hash_hex="ab" * 16,
            message_id="cd" * 32,
            timestamp=1000.0,
            reply_to=None,
            last_seen_id=None,
            image_data=None,
        )
        content = "nothing for you"
        signature = sign_message(
            alice.identity.rns_identity, content=content, **digest)
        bob.storage.remember_identity_key(
            alice.identity.hash_hex,
            alice.identity.rns_identity.get_public_key(),
        )

        assert verify_message(bob.storage, alice.identity.hash_hex, signature,
                              content=content, **digest)

        forged = f"{content} {mention_token(bob.identity.hash_hex)}"
        assert not verify_message(bob.storage, alice.identity.hash_hex, signature,
                                  content=forged, **digest)

    def test_a_mention_reaches_a_subscriber_unchanged(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("pings", "", "public")
        bob.storage.upsert_channel(ch_hash, "pings", "", alice.identity.hash_hex,
                                   "public", time.time())
        bob.storage.subscribe(ch_hash)

        content = f"over to you {mention_token(bob.identity.hash_hex)}"
        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content=content,
            subscriber_hashes=[bob.identity.hash_hex],
        )
        msg_id = alice.storage.get_messages(ch_hash)[0]["message_id"]

        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5)
        stored = bob.storage.get_messages(ch_hash)[0]
        assert stored["content"] == content
        assert mentions_identity(stored["content"], bob.identity.hash_hex)
