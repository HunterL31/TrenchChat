"""
Adversarial tests — bad clients that deliberately bypass permission restrictions.

Each test simulates a peer that ignores the normal UI/API flow and directly
crafts or injects protocol messages as if it were a malicious or buggy client.
The server-side (receiver) enforcement must reject every attempt.

Scenarios covered:
  SEND_MESSAGE
    - Member with revoked send_message sends a chat message directly
    - Non-member sends a chat message to an invite-only channel

  INVITE / JOIN_REQUEST
    - Non-member sends a join_request with a forged (self-signed) token
    - Member without INVITE permission sends a join_request to add a stranger
    - Replaying an already-used (expired) invite token
    - Token issued for a different channel is submitted for this channel
    - Token issued for Carol is submitted by Dave claiming to be Carol

  KICK (remove_members)
    - Member without KICK calls publish_member_list(remove_members=...)
    - Crafted MT_MEMBER_LIST_UPDATE with a removal, signed by a non-admin

  MANAGE_ROLES (add_admins / remove_admins)
    - Member without MANAGE_ROLES calls publish_member_list(add_admins=...)
    - Crafted MT_MEMBER_LIST_UPDATE promoting self, signed by a non-admin

  MANAGE_CHANNEL
    - Member without MANAGE_CHANNEL calls broadcast_permissions directly
      (the core does not gate broadcast_permissions on MANAGE_CHANNEL, but
       the permissions it embeds are already in the DB — so a member can't
       change the DB without MANAGE_CHANNEL; this test confirms that)

  MEMBER LIST INTEGRITY
    - Replay of an older (lower-version) member list doc is rejected
    - Crafted doc that demotes Alice (removes her from admins), signed by Bob
    - Crafted doc that removes Alice from owners, signed by Bob
    - Crafted doc that adds Bob to owners, signed by Bob
    - Version tiebreak: same version+timestamp, higher signer hash loses
    - Doc for channel A delivered as if it were for channel B is rejected
"""

import struct
import time

import msgpack
import pytest

from tests.helpers import wait_for, wait_for_member
from trenchchat.core.invite import _sign, _signed_payload
from trenchchat.core.permissions import (
    ALL_PERMISSIONS, INVITE, KICK, MANAGE_CHANNEL, MANAGE_ROLES,
    PRESET_PRIVATE, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER, SEND_MESSAGE,
)
from trenchchat.core.protocol import (
    F_ADMIN_HASH, F_CHANNEL_HASH, F_EXPIRY_TS, F_INVITE_TOKEN,
    F_INVITEE_HASH, F_MEMBER_LIST_DOC, F_MSG_TYPE,
    MT_JOIN_REQUEST, MT_MEMBER_LIST_UPDATE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_channel_with_member(peer_factory, *, member_perms=None):
    """
    Create alice (owner) and bob (member) on a shared invite-only channel.

    Returns (alice, bob, ch_hash).
    member_perms overrides the member permission list in the channel config.
    """
    alice = peer_factory("alice")
    bob = peer_factory("bob")

    perms = dict(PRESET_PRIVATE)
    if member_perms is not None:
        perms[ROLE_MEMBER] = list(member_perms)

    ch_hash = alice.channel_mgr.create_channel("test-ch", "", permissions=perms)
    alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])
    assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)

    # Mirror the channel and membership on Bob's side so his receiver can
    # apply the same permission checks.
    bob.storage.upsert_channel(ch_hash, "test-ch", "", alice.identity.hash_hex,
                               perms, time.time())
    bob.storage.subscribe(ch_hash)
    bob.storage.upsert_member(ch_hash, bob.identity.hash_hex, "Bob", role=ROLE_MEMBER)
    bob.storage.upsert_member(ch_hash, alice.identity.hash_hex, "Alice", role=ROLE_OWNER)
    bob.storage.set_channel_permissions(ch_hash, perms)

    return alice, bob, ch_hash


# ---------------------------------------------------------------------------
# SEND_MESSAGE
# ---------------------------------------------------------------------------

class TestAdversarialSendMessage:
    def test_revoked_member_message_dropped(self, peer_factory):
        """A member whose send_message permission has been revoked cannot send."""
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[]  # no send_message
        )

        bob.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="I should not be able to send",
            subscriber_hashes=[alice.identity.hash_hex],
        )

        time.sleep(0.3)
        msgs = alice.storage.get_messages(ch_hash)
        assert all(m["sender_hash"] != bob.identity.hash_hex for m in msgs), \
            "Alice accepted a message from Bob who lacks send_message"

    def test_non_member_message_dropped(self, peer_factory):
        """A peer who is not a member of an invite-only channel cannot send."""
        alice = peer_factory("alice")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("members-only", "", "invite")
        alice.invite_mgr.publish_member_list(ch_hash)

        # Carol is not a member — she just knows the channel hash
        carol.storage.upsert_channel(ch_hash, "members-only", "",
                                     alice.identity.hash_hex, "invite", time.time())
        carol.storage.subscribe(ch_hash)

        carol.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="I am not a member",
            subscriber_hashes=[alice.identity.hash_hex],
        )

        time.sleep(0.3)
        msgs = alice.storage.get_messages(ch_hash)
        assert len(msgs) == 0, "Alice accepted a message from non-member Carol"


# ---------------------------------------------------------------------------
# INVITE / JOIN_REQUEST
# ---------------------------------------------------------------------------

class TestAdversarialInvite:
    def test_self_signed_join_request_rejected(self, peer_factory):
        """
        Carol forges an invite token by signing the payload with her own key.
        Alice must reject the join request because the signature won't verify
        against any known admin identity.
        """
        alice = peer_factory("alice")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("forge-test", "", "invite")
        alice.invite_mgr.publish_member_list(ch_hash)

        expiry = time.time() + 3600
        # Carol signs with her own key, pretending to be an admin
        payload = (carol.identity.hash
                   + bytes.fromhex(ch_hash)
                   + struct.pack(">d", expiry))
        forged_token = _sign(carol.identity.rns_identity, payload)

        fields = {
            F_MSG_TYPE:     MT_JOIN_REQUEST,
            F_CHANNEL_HASH: bytes.fromhex(ch_hash),
            F_INVITE_TOKEN: forged_token,
            F_INVITEE_HASH: carol.identity.hash,
            F_EXPIRY_TS:    expiry,
            F_ADMIN_HASH:   carol.identity.hash,  # claims to be admin
        }
        alice.invite_mgr._handle_join_request(fields, ch_hash)

        assert not alice.storage.is_member(ch_hash, carol.identity.hash_hex), \
            "Alice accepted a join request with a self-signed forged token"

    def test_member_without_invite_cannot_approve_join(self, peer_factory):
        """
        Bob is a member but lacks INVITE permission.
        He calls publish_member_list(add_members=[carol]) directly — the core
        must accept the add_members (INVITE gates join-request approval, not
        direct adds by the owner), but when Bob tries to handle a join_request
        on Alice's behalf the _handle_join_request check must block him.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]  # no INVITE
        )
        carol = peer_factory("carol")

        # Bob tries to approve Carol via _handle_join_request
        expiry = time.time() + 3600
        payload = (carol.identity.hash
                   + bytes.fromhex(ch_hash)
                   + struct.pack(">d", expiry))
        # Use Alice's token (valid signature) but Bob is the one handling it
        token, _ = alice.invite_mgr.generate_invite_token(
            ch_hash, carol.identity.hash, ttl=3600
        )

        fields = {
            F_MSG_TYPE:     MT_JOIN_REQUEST,
            F_CHANNEL_HASH: bytes.fromhex(ch_hash),
            F_INVITE_TOKEN: token,
            F_INVITEE_HASH: carol.identity.hash,
            F_EXPIRY_TS:    expiry,
            F_ADMIN_HASH:   alice.identity.hash,
        }
        # Bob's invite_mgr receives the join request — he lacks INVITE
        bob.invite_mgr._handle_join_request(fields, ch_hash)

        time.sleep(0.3)
        assert not bob.storage.is_member(ch_hash, carol.identity.hash_hex), \
            "Bob approved a join request despite lacking INVITE permission"

    def test_expired_token_rejected(self, peer_factory):
        """A join request carrying an already-expired token is rejected."""
        alice = peer_factory("alice")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("expire-ch", "", "invite")
        alice.invite_mgr.publish_member_list(ch_hash)

        token, expiry = alice.invite_mgr.generate_invite_token(
            ch_hash, carol.identity.hash, ttl=-1  # already expired
        )

        fields = {
            F_MSG_TYPE:     MT_JOIN_REQUEST,
            F_CHANNEL_HASH: bytes.fromhex(ch_hash),
            F_INVITE_TOKEN: token,
            F_INVITEE_HASH: carol.identity.hash,
            F_EXPIRY_TS:    expiry,
            F_ADMIN_HASH:   alice.identity.hash,
        }
        alice.invite_mgr._handle_join_request(fields, ch_hash)

        assert not alice.storage.is_member(ch_hash, carol.identity.hash_hex), \
            "Alice accepted a join request with an expired token"


# ---------------------------------------------------------------------------
# KICK
# ---------------------------------------------------------------------------

class TestAdversarialKick:
    def test_member_without_kick_cannot_remove_via_api(self, peer_factory):
        """
        Bob lacks KICK. Calling publish_member_list(remove_members=[carol])
        must be silently ignored by the core.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]  # no KICK
        )
        carol = peer_factory("carol")

        # Add Carol as a member first (via Alice)
        alice.invite_mgr.publish_member_list(ch_hash, add_members=[carol.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, carol.identity.hash_hex)

        # Bob tries to kick Carol
        bob.invite_mgr.publish_member_list(ch_hash, remove_members=[carol.identity.hash])

        time.sleep(0.3)
        # Carol must still be a member on Alice's side (Bob's remove was ignored)
        assert alice.storage.is_member(ch_hash, carol.identity.hash_hex), \
            "Bob removed Carol despite lacking KICK permission"

    def test_crafted_member_list_removal_rejected(self, peer_factory):
        """
        Bob crafts a raw MT_MEMBER_LIST_UPDATE doc that removes Carol,
        signed with Bob's key (not an admin/owner key).
        Alice must reject it because Bob is not in the admins/owners set.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        carol = peer_factory("carol")

        alice.invite_mgr.publish_member_list(ch_hash, add_members=[carol.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, carol.identity.hash_hex)

        existing = alice.storage.get_member_list_version(ch_hash)
        current_v = existing["version"]

        # Bob builds a doc that removes Carol, signed by Bob (not an admin)
        members_without_carol = [alice.identity.hash, bob.identity.hash]
        admins = [alice.identity.hash]
        owners = [alice.identity.hash]
        version = current_v + 1
        published_at = time.time()
        payload = _signed_payload(
            bytes.fromhex(ch_hash), version, published_at,
            members_without_carol, admins, owners, b"",
        )
        sig = _sign(bob.identity.rns_identity, payload)
        doc = {
            "channel_hash": bytes.fromhex(ch_hash),
            "version":      version,
            "published_at": published_at,
            "members":      members_without_carol,
            "admins":       admins,
            "owners":       owners,
            "permissions":  b"",
            "signatures":   {bob.identity.hash: sig},
        }

        accepted = alice.invite_mgr._accept_document(doc, ch_hash)
        assert not accepted, "Alice accepted a member list signed by a non-admin"
        assert alice.storage.is_member(ch_hash, carol.identity.hash_hex), \
            "Carol was removed by a non-admin crafted member list"


# ---------------------------------------------------------------------------
# MANAGE_ROLES
# ---------------------------------------------------------------------------

class TestAdversarialManageRoles:
    def test_member_without_manage_roles_cannot_promote_via_api(self, peer_factory):
        """
        Bob lacks MANAGE_ROLES. Calling publish_member_list(add_admins=[bob])
        must be silently ignored.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]  # no MANAGE_ROLES
        )

        bob.invite_mgr.publish_member_list(ch_hash, add_admins=[bob.identity.hash])

        time.sleep(0.3)
        assert alice.storage.get_role(ch_hash, bob.identity.hash_hex) == ROLE_MEMBER, \
            "Bob promoted himself to admin despite lacking MANAGE_ROLES"

    def test_crafted_member_list_self_promotion_rejected(self, peer_factory):
        """
        Bob crafts a MT_MEMBER_LIST_UPDATE that adds himself to the admins list,
        signed with his own key. Alice must reject it.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )

        existing = alice.storage.get_member_list_version(ch_hash)
        current_v = existing["version"]

        # Bob crafts a doc promoting himself to admin
        members = [alice.identity.hash, bob.identity.hash]
        admins_with_bob = [alice.identity.hash, bob.identity.hash]
        owners = [alice.identity.hash]
        version = current_v + 1
        published_at = time.time()
        payload = _signed_payload(
            bytes.fromhex(ch_hash), version, published_at,
            members, admins_with_bob, owners, b"",
        )
        sig = _sign(bob.identity.rns_identity, payload)
        doc = {
            "channel_hash": bytes.fromhex(ch_hash),
            "version":      version,
            "published_at": published_at,
            "members":      members,
            "admins":       admins_with_bob,
            "owners":       owners,
            "permissions":  b"",
            "signatures":   {bob.identity.hash: sig},
        }

        accepted = alice.invite_mgr._accept_document(doc, ch_hash)
        assert not accepted, "Alice accepted a self-promotion doc signed by a non-admin"
        assert alice.storage.get_role(ch_hash, bob.identity.hash_hex) == ROLE_MEMBER, \
            "Bob's role was changed by a crafted member list he signed himself"


# ---------------------------------------------------------------------------
# MANAGE_CHANNEL
# ---------------------------------------------------------------------------

class TestAdversarialManageChannel:
    def test_member_without_manage_channel_cannot_change_permissions(self, peer_factory):
        """
        Bob lacks MANAGE_CHANNEL. Directly calling set_channel_permissions
        on his own storage does not affect Alice's storage, and
        broadcast_permissions is not gated on MANAGE_CHANNEL in the core
        (the GUI is), but Alice's receiver will only accept the embedded
        permissions if the member list doc is signed by a valid admin/owner.
        Bob's broadcast_permissions call signs with Bob's key (non-admin),
        so Alice must reject the document and keep her original permissions.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]  # no MANAGE_CHANNEL
        )

        original_perms = alice.storage.get_channel_permissions(ch_hash)

        # Bob locally changes his copy of the permissions
        evil_perms = dict(PRESET_PRIVATE)
        evil_perms[ROLE_MEMBER] = list(ALL_PERMISSIONS)  # grant members everything
        bob.storage.set_channel_permissions(ch_hash, evil_perms)

        # Bob broadcasts the change — his doc is signed by a non-admin key
        bob.invite_mgr.broadcast_permissions(ch_hash)

        time.sleep(0.3)
        alice_perms = alice.storage.get_channel_permissions(ch_hash)
        assert alice_perms.get(ROLE_MEMBER) == original_perms.get(ROLE_MEMBER), \
            "Alice accepted a permissions change broadcast by a non-admin member"

    def test_owner_can_change_permissions(self, peer_factory):
        """Sanity check: the owner's broadcast_permissions IS accepted."""
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )

        new_perms = dict(PRESET_PRIVATE)
        new_perms[ROLE_MEMBER] = [SEND_MESSAGE, INVITE]
        alice.storage.set_channel_permissions(ch_hash, new_perms)
        alice.invite_mgr.broadcast_permissions(ch_hash)

        assert wait_for(
            lambda: bob.storage.has_permission(ch_hash, bob.identity.hash_hex, INVITE),
            timeout=5,
        ), "Bob did not receive Alice's permission update"


# ---------------------------------------------------------------------------
# INVITE TOKEN — cross-channel and cross-invitee misuse
# ---------------------------------------------------------------------------

class TestAdversarialTokenMisuse:
    def test_token_for_wrong_channel_rejected(self, peer_factory):
        """
        A valid token issued by Alice for channel A is submitted as a join
        request for channel B.  The token payload binds the channel hash, so
        verification must fail and Carol must not be added to channel B.
        """
        alice = peer_factory("alice")
        carol = peer_factory("carol")

        ch_a = alice.channel_mgr.create_channel("channel-a", "", "invite")
        ch_b = alice.channel_mgr.create_channel("channel-b", "", "invite")
        alice.invite_mgr.publish_member_list(ch_a)
        alice.invite_mgr.publish_member_list(ch_b)

        # Token is legitimately issued for channel A
        token, expiry = alice.invite_mgr.generate_invite_token(
            ch_a, carol.identity.hash, ttl=3600
        )

        # Submit it as a join request for channel B
        fields = {
            F_MSG_TYPE:     MT_JOIN_REQUEST,
            F_CHANNEL_HASH: bytes.fromhex(ch_b),
            F_INVITE_TOKEN: token,
            F_INVITEE_HASH: carol.identity.hash,
            F_EXPIRY_TS:    expiry,
            F_ADMIN_HASH:   alice.identity.hash,
        }
        alice.invite_mgr._handle_join_request(fields, ch_b)

        assert not alice.storage.is_member(ch_b, carol.identity.hash_hex), \
            "Alice accepted a channel-A token as a valid join request for channel B"

    def test_token_for_wrong_invitee_rejected(self, peer_factory):
        """
        A valid token issued for Carol is submitted by Dave, who swaps the
        F_INVITEE_HASH field to his own hash.  The token payload includes the
        invitee hash, so verification against Dave's hash must fail.
        """
        alice = peer_factory("alice")
        carol = peer_factory("carol")
        dave  = peer_factory("dave")

        ch_hash = alice.channel_mgr.create_channel("swap-test", "", "invite")
        alice.invite_mgr.publish_member_list(ch_hash)

        # Token is legitimately issued for Carol
        token, expiry = alice.invite_mgr.generate_invite_token(
            ch_hash, carol.identity.hash, ttl=3600
        )

        # Dave submits Carol's token but claims to be the invitee
        fields = {
            F_MSG_TYPE:     MT_JOIN_REQUEST,
            F_CHANNEL_HASH: bytes.fromhex(ch_hash),
            F_INVITE_TOKEN: token,
            F_INVITEE_HASH: dave.identity.hash,   # swapped to Dave
            F_EXPIRY_TS:    expiry,
            F_ADMIN_HASH:   alice.identity.hash,
        }
        alice.invite_mgr._handle_join_request(fields, ch_hash)

        assert not alice.storage.is_member(ch_hash, dave.identity.hash_hex), \
            "Alice added Dave using a token that was issued for Carol"
        assert not alice.storage.is_member(ch_hash, carol.identity.hash_hex), \
            "Carol was added even though she never sent a join request"


# ---------------------------------------------------------------------------
# MEMBER LIST INTEGRITY
# ---------------------------------------------------------------------------

def _build_crafted_doc(signer, ch_hash: str, version: int,
                       members: list, admins: list, owners: list,
                       permissions_blob: bytes = b"") -> dict:
    """Build a member list doc signed by *signer* (an RNS.Identity)."""
    published_at = time.time()
    payload = _signed_payload(
        bytes.fromhex(ch_hash), version, published_at,
        members, admins, owners, permissions_blob,
    )
    sig = _sign(signer, payload)
    signer_hash = signer.hash
    return {
        "channel_hash": bytes.fromhex(ch_hash),
        "version":      version,
        "published_at": published_at,
        "members":      members,
        "admins":       admins,
        "owners":       owners,
        "permissions":  permissions_blob,
        "signatures":   {signer_hash: sig},
    }


class TestAdversarialMemberListIntegrity:
    def test_replay_of_old_version_rejected(self, peer_factory):
        """
        Bob captures Alice's v1 member list doc and re-sends it after Alice
        has already published v2.  The receiver must reject the older version.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )

        # Alice is at v1 after _setup_channel_with_member.
        # Capture the current (v1) blob before Alice advances to v2.
        existing_v1 = alice.storage.get_member_list_version(ch_hash)
        assert existing_v1 is not None
        import msgpack as _msgpack
        old_doc_raw = _msgpack.unpackb(existing_v1["document_blob"], raw=True)
        old_doc = {
            "channel_hash": old_doc_raw[b"channel_hash"],
            "version":      old_doc_raw[b"version"],
            "published_at": old_doc_raw[b"published_at"],
            "members":      list(old_doc_raw[b"members"]),
            "admins":       list(old_doc_raw[b"admins"]),
            "owners":       list(old_doc_raw.get(b"owners", [])),
            "permissions":  old_doc_raw.get(b"permissions", b""),
            "signatures":   dict(old_doc_raw[b"signatures"]),
        }

        # Alice publishes v2 (adds nothing, just increments version)
        alice.invite_mgr.publish_member_list(ch_hash)
        assert alice.storage.get_member_list_version(ch_hash)["version"] == 2

        # Bob replays the v1 doc at Alice's receiver
        accepted = alice.invite_mgr._accept_document(old_doc, ch_hash)
        assert not accepted, "Alice accepted a replayed older-version member list doc"
        assert alice.storage.get_member_list_version(ch_hash)["version"] == 2, \
            "Version was rolled back by a replayed doc"

    def test_crafted_doc_cannot_demote_admin(self, peer_factory):
        """
        Bob crafts a doc that removes Alice from the admins list (demoting her
        to a plain member), signed by Bob.  Alice must reject it and keep her
        admin role.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )

        existing = alice.storage.get_member_list_version(ch_hash)
        current_v = existing["version"]

        # Doc with Alice removed from admins, signed by Bob (non-admin)
        members = [alice.identity.hash, bob.identity.hash]
        admins_without_alice = []          # Alice demoted
        owners = [alice.identity.hash]
        doc = _build_crafted_doc(
            bob.identity.rns_identity, ch_hash, current_v + 1,
            members, admins_without_alice, owners,
        )

        accepted = alice.invite_mgr._accept_document(doc, ch_hash)
        assert not accepted, "Alice accepted a doc that demotes her, signed by a non-admin"
        assert alice.storage.is_admin(ch_hash, alice.identity.hash_hex), \
            "Alice's admin role was removed by a crafted doc"

    def test_crafted_doc_cannot_remove_owner(self, peer_factory):
        """
        Bob crafts a doc that removes Alice from the owners list entirely,
        signed by Bob.  Must be rejected.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )

        existing = alice.storage.get_member_list_version(ch_hash)
        current_v = existing["version"]

        members = [alice.identity.hash, bob.identity.hash]
        admins  = [alice.identity.hash]
        owners_without_alice = []          # Alice removed from owners
        doc = _build_crafted_doc(
            bob.identity.rns_identity, ch_hash, current_v + 1,
            members, admins, owners_without_alice,
        )

        accepted = alice.invite_mgr._accept_document(doc, ch_hash)
        assert not accepted, "Alice accepted a doc that strips her owner status"
        assert alice.storage.get_role(ch_hash, alice.identity.hash_hex) == ROLE_OWNER, \
            "Alice's owner role was stripped by a crafted doc"

    def test_crafted_doc_cannot_add_self_to_owners(self, peer_factory):
        """
        Bob crafts a doc that adds himself to the owners list, signed by Bob.
        Must be rejected — Bob is not a trusted signer.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )

        existing = alice.storage.get_member_list_version(ch_hash)
        current_v = existing["version"]

        members = [alice.identity.hash, bob.identity.hash]
        admins  = [alice.identity.hash]
        owners_with_bob = [alice.identity.hash, bob.identity.hash]
        doc = _build_crafted_doc(
            bob.identity.rns_identity, ch_hash, current_v + 1,
            members, admins, owners_with_bob,
        )

        accepted = alice.invite_mgr._accept_document(doc, ch_hash)
        assert not accepted, "Alice accepted a doc that grants Bob owner status"
        assert alice.storage.get_role(ch_hash, bob.identity.hash_hex) == ROLE_MEMBER, \
            "Bob's role was elevated to owner by a crafted doc"

    def test_version_tiebreak_higher_signer_hash_loses(self, peer_factory):
        """
        Two valid docs with the same version and timestamp compete.
        The tiebreak rule is: lowest signing admin hash wins.
        A doc signed by a higher hash must be rejected when a lower-hash doc
        is already stored.
        """
        alice = peer_factory("alice")
        bob   = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("tiebreak-ch", "", "invite")
        # Add Bob as admin so he is a trusted signer
        alice.invite_mgr.publish_member_list(
            ch_hash, add_members=[bob.identity.hash], add_admins=[bob.identity.hash]
        )
        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)

        existing = alice.storage.get_member_list_version(ch_hash)
        current_v = existing["version"]
        shared_ts = time.time()

        members = [alice.identity.hash, bob.identity.hash]
        admins  = [alice.identity.hash, bob.identity.hash]
        owners  = [alice.identity.hash]

        # Build two competing docs at the same version+timestamp
        alice_payload = _signed_payload(
            bytes.fromhex(ch_hash), current_v + 1, shared_ts,
            members, admins, owners, b"",
        )
        bob_payload = _signed_payload(
            bytes.fromhex(ch_hash), current_v + 1, shared_ts,
            members, admins, owners, b"",
        )
        alice_sig = _sign(alice.identity.rns_identity, alice_payload)
        bob_sig   = _sign(bob.identity.rns_identity, bob_payload)

        alice_doc = {
            "channel_hash": bytes.fromhex(ch_hash),
            "version":      current_v + 1,
            "published_at": shared_ts,
            "members":      members,
            "admins":       admins,
            "owners":       owners,
            "permissions":  b"",
            "signatures":   {alice.identity.hash: alice_sig},
        }
        bob_doc = {
            "channel_hash": bytes.fromhex(ch_hash),
            "version":      current_v + 1,
            "published_at": shared_ts,
            "members":      members,
            "admins":       admins,
            "owners":       owners,
            "permissions":  b"",
            "signatures":   {bob.identity.hash: bob_sig},
        }

        # Determine which signer hash is lower — that doc should win
        if alice.identity.hash < bob.identity.hash:
            winner_doc, loser_doc = alice_doc, bob_doc
            winner_name = "alice"
        else:
            winner_doc, loser_doc = bob_doc, alice_doc
            winner_name = "bob"

        # Accept the winner first, then try to accept the loser
        assert alice.invite_mgr._accept_document(winner_doc, ch_hash), \
            "Winner doc was rejected"
        accepted_loser = alice.invite_mgr._accept_document(loser_doc, ch_hash)
        assert not accepted_loser, \
            f"Loser doc (higher signer hash) was accepted over the winner ({winner_name})"

    def test_doc_for_wrong_channel_rejected(self, peer_factory):
        """
        A valid member list doc for channel A is submitted to _accept_document
        as if it were for channel B.  The trusted signers for B do not include
        Alice (the channel-A admin), so the doc must be rejected.
        """
        alice = peer_factory("alice")
        bob   = peer_factory("bob")

        ch_a = alice.channel_mgr.create_channel("channel-a", "", "invite")
        ch_b = alice.channel_mgr.create_channel("channel-b", "", "invite")

        # Publish initial member lists for both channels
        alice.invite_mgr.publish_member_list(ch_a, add_members=[bob.identity.hash])
        alice.invite_mgr.publish_member_list(ch_b, add_members=[bob.identity.hash])
        assert wait_for_member(alice.storage, ch_a, bob.identity.hash_hex)
        assert wait_for_member(alice.storage, ch_b, bob.identity.hash_hex)

        # Build a legitimate doc for channel A (signed by Alice)
        existing_a = alice.storage.get_member_list_version(ch_a)
        current_v_a = existing_a["version"]
        members = [alice.identity.hash, bob.identity.hash]
        admins  = [alice.identity.hash]
        owners  = [alice.identity.hash]
        published_at = time.time()
        payload = _signed_payload(
            bytes.fromhex(ch_a), current_v_a + 1, published_at,
            members, admins, owners, b"",
        )
        sig = _sign(alice.identity.rns_identity, payload)
        doc_for_a = {
            "channel_hash": bytes.fromhex(ch_a),
            "version":      current_v_a + 1,
            "published_at": published_at,
            "members":      members,
            "admins":       admins,
            "owners":       owners,
            "permissions":  b"",
            "signatures":   {alice.identity.hash: sig},
        }

        # Now try to accept this channel-A doc as if it were for channel B.
        # The payload was signed over ch_a's hash, so signature verification
        # against the ch_b payload will fail.
        existing_b_v = alice.storage.get_member_list_version(ch_b)["version"]
        accepted = alice.invite_mgr._accept_document(doc_for_a, ch_b)
        assert not accepted, \
            "Alice accepted a member list doc whose payload was signed for a different channel"
        assert alice.storage.get_member_list_version(ch_b)["version"] == existing_b_v, \
            "Channel B's version was modified by a doc intended for channel A"
