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
from types import SimpleNamespace
from unittest.mock import patch

import msgpack
import pytest
import LXMF
import RNS

from tests.conftest import forge
from tests.helpers import sign_as, wait_for, wait_for_member
from trenchchat.network.router import (
    PATH_REQUEST_GLOBAL_BURST, PATH_REQUEST_MAX_SOURCES, QUARANTINE_MAX_PER_SENDER,
)
from trenchchat.core import actions
from trenchchat.core.invite import _sign, _signed_payload
from trenchchat.core.messaging import _compute_message_id
from trenchchat.core.naming import dm_hash_for
from trenchchat.core.protocol import (
    DM_ENVELOPE_TYPE, LXMF_FIELD_CUSTOM_DATA, LXMF_FIELD_CUSTOM_TYPE,
    pack_dm_envelope, pack_fields,
)
from trenchchat.core.friends import (
    MAX_HELD_MESSAGES, MAX_HELD_PER_SENDER, MAX_PENDING_FRIEND_REQUESTS,
    MAX_REQUEST_BODY_CHARS,
)
from trenchchat.core.storage import FRIEND_PENDING_IN, FRIEND_PENDING_OUT
from trenchchat.core.permissions import (
    ALL_PERMISSIONS, FULL_SYNC, INVITE, KICK, MANAGE_CHANNEL, MANAGE_ROLES,
    PRESET_OPEN, PRESET_PRIVATE, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER, SEND_MESSAGE,
    is_open_join, permissions_from_json,
)
from trenchchat.core.subscription import SubscriptionManager, _subscriber_payload
from trenchchat.core.protocol import (
    F_SUBSCRIBER_LIST, F_SUBSCRIBER_SIG, F_SUBSCRIBER_VERSION, MT_SUBSCRIBER_LIST,
    F_ADMIN_HASH, F_CHANNEL_HASH, F_DISPLAY_NAME, F_EXPIRY_TS, F_INVITE_TOKEN,
    F_INVITEE_HASH, F_INVITE_ISSUED_TS, F_MEMBER_LIST_DOC, F_MESSAGE_ID, F_MSG_TYPE,
    F_TIMESTAMP,
    F_EMOJI_HASH, F_IMAGE_DATA, F_MISSED_FOR, F_REACTION_MSG_ID, F_REACTION_REMOVE,
    F_REACTION_UNICODE, F_AUTHOR_SIG,
    MT_FRIEND_ACCEPT, MT_GOODBYE, MT_JOIN_REQUEST, MT_MEMBER_LIST_UPDATE,
    MT_REACTION,
    MT_SYNC_RESPONSE, F_SYNC_MESSAGES,
)
from trenchchat.core.presence import PresenceManager
from trenchchat.core.image import MAX_IMAGE_BYTES


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

    def test_member_without_manage_channel_cannot_grant_self_full_sync(self, peer_factory):
        """
        full_sync is a per-role permission like any other (send_message,
        invite, ...), travelling inside the same signed permissions
        document -- Bob can't grant it to his own role by broadcasting his
        own doc, same as he can't grant himself KICK or MANAGE_ROLES.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]  # no MANAGE_CHANNEL
        )
        assert not alice.storage.has_permission(ch_hash, bob.identity.hash_hex, FULL_SYNC)

        evil_perms = dict(PRESET_PRIVATE)
        evil_perms[ROLE_MEMBER] = [SEND_MESSAGE, FULL_SYNC]
        bob.storage.set_channel_permissions(ch_hash, evil_perms)
        bob.invite_mgr.broadcast_permissions(ch_hash)

        time.sleep(0.3)
        assert not alice.storage.has_permission(ch_hash, bob.identity.hash_hex, FULL_SYNC), \
            "Bob granted himself full_sync without MANAGE_CHANNEL"

    def test_owner_can_grant_full_sync_to_one_role_but_not_another(self, peer_factory):
        """Sanity check: the owner's broadcast_permissions can grant full_sync
        to one role while withholding it from another, and it propagates to
        existing members -- the scenario this permission exists for (e.g.
        admin gets it, member doesn't)."""
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )

        new_perms = dict(PRESET_PRIVATE)
        new_perms[ROLE_ADMIN] = [SEND_MESSAGE, FULL_SYNC]
        new_perms[ROLE_MEMBER] = [SEND_MESSAGE]
        alice.storage.set_channel_permissions(ch_hash, new_perms)
        alice.invite_mgr.broadcast_permissions(ch_hash)

        assert wait_for(
            lambda: bob.storage.get_channel_permissions(ch_hash).get(ROLE_ADMIN) == [
                SEND_MESSAGE, FULL_SYNC
            ],
            timeout=5,
        ), "Bob did not receive Alice's full_sync change"
        assert not bob.storage.has_permission(ch_hash, bob.identity.hash_hex, FULL_SYNC), \
            "Bob (plain member) ended up with full_sync from a change that only granted it to admin"

    def test_actions_edit_channel_permissions_rejects_unauthorized_local_caller(self, peer_factory):
        """
        Direct-call coverage for actions.edit_channel_permissions -- the
        shared entry point devtools/testenv/api.py's update_permissions
        calls. Its own
        MANAGE_CHANNEL check (not the signed-document path the other
        MANAGE_CHANNEL tests in this class cover) is what stands between a
        modified/compromised local client and rewriting the local
        permissions store directly, bypassing the GUI entirely.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]  # no MANAGE_CHANNEL
        )
        before = bob.storage.get_channel_permissions(ch_hash)

        evil_perms = dict(PRESET_PRIVATE)
        evil_perms[ROLE_MEMBER] = list(ALL_PERMISSIONS)
        applied = actions.edit_channel_permissions(
            bob.storage, bob.invite_mgr, ch_hash, bob.identity.hash_hex, evil_perms,
        )

        assert applied is False
        assert bob.storage.get_channel_permissions(ch_hash) == before, \
            "Bob's local permissions store was rewritten by a direct call without MANAGE_CHANNEL"


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


# ---------------------------------------------------------------------------
# MEMBERSHIP TENURE — sync replay by kicked members
# ---------------------------------------------------------------------------

class TestAdversarialTenure:
    def test_tenure_recorded_on_kick(self, peer_factory):
        """
        When Alice kicks Bob, Bob's tenure interval is closed at published_at.
        Messages Bob sends after the kick timestamp are invalid.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        # publish_member_list in _setup already created tenure rows for alice and bob.
        # No need to manually open tenure — doing so would create stale open intervals.
        assert alice.storage.has_any_tenure(ch_hash), \
            "Tenure should be populated by publish_member_list"

        # Capture Bob's join timestamp before the kick
        bob_joined_ts = alice.storage.get_member_list_version(ch_hash)["published_at"]

        alice.invite_mgr.publish_member_list(
            ch_hash, remove_members=[bob.identity.hash]
        )
        assert wait_for(
            lambda: not alice.storage.is_member(ch_hash, bob.identity.hash_hex),
            timeout=5,
        ), "Bob was not removed from the member list"

        # Bob was a member before the kick
        assert alice.storage.was_member_at(ch_hash, bob.identity.hash_hex, bob_joined_ts)
        # Bob is not a member at or after the published_at of the kick doc
        kick_published_at = alice.storage.get_member_list_version(ch_hash)["published_at"]
        assert not alice.storage.was_member_at(
            ch_hash, bob.identity.hash_hex, kick_published_at
        )

    def test_kick_reaches_the_removed_member(self, peer_factory):
        """The removal doc must be sent to the peer being removed.

        _broadcast_member_list iterates the members table, but _accept_document
        has already dropped the kicked peer from it -- so the one peer who needs
        to learn they were removed (and drop the channel and its stale role) got
        nothing. They must be a recipient of this broadcast.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )

        sent_to = []
        original = alice.invite_mgr._send_raw

        def recording_send_raw(dest_hex, fields):
            if fields.get(F_MSG_TYPE) == MT_MEMBER_LIST_UPDATE:
                sent_to.append(dest_hex)
            return original(dest_hex, fields)

        alice.invite_mgr._send_raw = recording_send_raw
        alice.invite_mgr.publish_member_list(
            ch_hash, remove_members=[bob.identity.hash]
        )

        assert bob.identity.hash_hex in sent_to, \
            "the removal document was never sent to the kicked member"

    def test_forged_joined_at_claim_from_untrusted_signer_rejected(self, peer_factory):
        """
        Bob crafts a raw MT_MEMBER_LIST_UPDATE doc claiming he joined the
        channel long before he actually did (backdated joined_at for
        himself), signed with his own key rather than an admin/owner's.

        This is the exact scenario update_tenure()'s joined_at_map exists to
        protect against: a member trying to unlock history from before they
        were actually added by asserting an earlier join date. The forged
        claim never even reaches update_tenure(), because _validate_document
        rejects the whole document first -- Bob isn't a trusted signer, so
        neither his backdated joined_at nor anything else in his crafted doc
        is trusted, regardless of what it claims.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        real_bob_joined_at = alice.storage.get_open_tenure_joined_at(
            ch_hash, bob.identity.hash_hex
        )
        assert real_bob_joined_at is not None

        existing = alice.storage.get_member_list_version(ch_hash)
        version = existing["version"] + 1
        published_at = time.time()
        members = [alice.identity.hash, bob.identity.hash]
        admins = [alice.identity.hash]
        owners = [alice.identity.hash]
        forged_joined_at = {
            alice.identity.hash: real_bob_joined_at - 10_000,
            bob.identity.hash:   real_bob_joined_at - 10_000,
        }
        payload = _signed_payload(
            bytes.fromhex(ch_hash), version, published_at,
            members, admins, owners, b"", forged_joined_at,
        )
        sig = _sign(bob.identity.rns_identity, payload)
        doc = {
            "channel_hash": bytes.fromhex(ch_hash),
            "version":      version,
            "published_at": published_at,
            "members":      members,
            "admins":       admins,
            "owners":       owners,
            "permissions":  b"",
            "joined_at":    forged_joined_at,
            "signatures":   {bob.identity.hash: sig},
        }

        accepted = alice.invite_mgr._accept_document(doc, ch_hash)
        assert not accepted, "Alice accepted a member list signed by a non-admin"

        # Bob's tenure must be untouched by the forged claim.
        assert alice.storage.get_open_tenure_joined_at(
            ch_hash, bob.identity.hash_hex
        ) == real_bob_joined_at, "Bob's forged backdated joined_at was applied"
        assert not alice.storage.was_member_at(
            ch_hash, bob.identity.hash_hex, real_bob_joined_at - 5_000
        ), "Bob's forged claim unlocked history from before he actually joined"

    def test_synced_message_from_kicked_member_in_gap_is_rejected(self, peer_factory):
        """
        Bob is kicked and sends a message locally during the gap period.
        When that gap message arrives in a sync response, Alice must reject it
        because Bob's tenure interval is closed at the kick's published_at.
        The rejection must happen before any potential re-add opens a new interval.
        """
        from trenchchat.core.messaging import _compute_message_id

        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        # publish_member_list already created tenure rows — confirm they exist
        assert alice.storage.has_any_tenure(ch_hash)

        # Alice kicks Bob
        alice.invite_mgr.publish_member_list(
            ch_hash, remove_members=[bob.identity.hash]
        )
        assert wait_for(
            lambda: not alice.storage.is_member(ch_hash, bob.identity.hash_hex),
            timeout=5,
        )
        kick_published_at = alice.storage.get_member_list_version(ch_hash)["published_at"]

        # Bob sends a message locally during the gap (after kick_published_at).
        # Verify the tenure check works before any re-add could widen the window.
        gap_ts = kick_published_at + 10
        gap_content = "Message during gap — invalid"
        gap_msg_id = _compute_message_id(gap_content, bob.identity.hash_hex, gap_ts)

        assert not alice.storage.was_member_at(ch_hash, bob.identity.hash_hex, gap_ts), \
            "Tenure says Bob was a member during the gap — test setup error"

        # Simulate a sync response delivering Bob's gap message
        alice.sync_mgr._handle_sync_response(
            {0x08: __import__("msgpack").packb([{
                "sender_hash":  bob.identity.hash_hex,
                "sender_name":  "Bob",
                "content":      gap_content,
                "timestamp":    gap_ts,
                "message_id":   gap_msg_id,
                "reply_to":     None,
                "last_seen_id": None,
            }], use_bin_type=True)},
            ch_hash,
        )

        assert not alice.storage.message_exists(gap_msg_id), \
            "Alice accepted a gap message from a kicked member via sync"

    def test_synced_message_from_valid_tenure_is_accepted(self, peer_factory):
        """
        A message sent before Bob was kicked (within valid tenure) must be
        accepted by the sync response handler.
        """
        from trenchchat.core.messaging import _compute_message_id

        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        # publish_member_list already created tenure rows
        assert alice.storage.has_any_tenure(ch_hash)

        # Legitimate message timestamped before the kick (use the join published_at)
        bob_join_ts = alice.storage.get_member_list_version(ch_hash)["published_at"]
        valid_ts = bob_join_ts - 1  # technically before the signed doc, but within open interval
        # Use a timestamp clearly within the tenure window
        valid_ts = bob_join_ts + 0.001
        valid_content = "Message before kick — valid"
        valid_msg_id = _compute_message_id(valid_content, bob.identity.hash_hex, valid_ts)

        # Alice kicks Bob — the kick published_at must be > valid_ts
        time.sleep(0.01)  # ensure kick timestamp is strictly after valid_ts
        alice.invite_mgr.publish_member_list(
            ch_hash, remove_members=[bob.identity.hash]
        )
        assert wait_for(
            lambda: not alice.storage.is_member(ch_hash, bob.identity.hash_hex),
            timeout=5,
        )
        kick_published_at = alice.storage.get_member_list_version(ch_hash)["published_at"]
        assert kick_published_at > valid_ts, \
            "Test setup error: kick_published_at must be after valid_ts"

        # Sync delivers the pre-kick message — must be accepted.
        # Responses are only applied in answer to a request we issued, so
        # solicit one first; an unsolicited response is covered separately by
        # TestAdversarialSyncInjection.
        alice.sync_mgr._record_pending_request(ch_hash, bob.identity.hash_hex)
        alice.sync_mgr._handle_sync_response(
            {0x08: __import__("msgpack").packb([{
                "sender_hash":  bob.identity.hash_hex,
                "sender_name":  "Bob",
                "content":      valid_content,
                "timestamp":    valid_ts,
                "message_id":   valid_msg_id,
                "reply_to":     None,
                "last_seen_id": None,
                "author_sig":   sign_as(bob.identity.hash_hex, ch_hash,
                                        valid_msg_id, valid_ts, valid_content),
            }], use_bin_type=True)},
            ch_hash,
            bob.identity.hash_hex,
        )

        assert alice.storage.message_exists(valid_msg_id), \
            "Alice rejected a legitimate pre-kick message via sync"

    def test_cancel_pending_on_kick(self, peer_factory):
        """
        When the local identity is removed from a channel, pending outbound
        messages for that channel are discarded.
        """
        from trenchchat.core.messaging import _compute_message_id

        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )

        # Manually queue a pending outbound message on Bob's side
        ts = time.time()
        content = "Queued while member"
        msg_id = _compute_message_id(content, bob.identity.hash_hex, ts)
        bob.messaging._pending[alice.identity.hash_hex] = [{
            "channel_hash_hex":  ch_hash,
            "content":           content,
            "timestamp":         ts,
            "msg_id":            msg_id,
            "display_name":      "Bob",
            "reply_to":          None,
            "last_seen_id":      None,
            "subscriber_hashes": [alice.identity.hash_hex],
        }]

        # Mirror kick on Bob's storage so the member list callback fires correctly
        bob.storage.upsert_channel(ch_hash, "test-ch", "", alice.identity.hash_hex,
                                   "invite", time.time())
        bob.storage.subscribe(ch_hash)

        # Simulate receiving the kick by calling the member list callback on Bob's SyncManager
        # (remove Bob from Bob's own member table to reflect the kick)
        bob.storage.remove_member(ch_hash, bob.identity.hash_hex)
        bob.sync_mgr._on_member_list_updated(ch_hash)

        # Pending messages for that channel should be gone
        pending_for_channel = [
            p for msgs in bob.messaging._pending.values()
            for p in msgs
            if p.get("channel_hash_hex") == ch_hash
        ]
        assert not pending_for_channel, \
            "Pending outbound messages were not cleared after Bob was kicked"


# ---------------------------------------------------------------------------
# INBOUND MESSAGE AUTHENTICATION (LXMF signature enforcement)
# ---------------------------------------------------------------------------

class TestAdversarialUnauthenticatedDelivery:
    """
    LXMF records a failed signature check on the message and delivers it
    anyway; source_hash is attacker-chosen wire data.  Router._authenticate is
    the only thing standing between a spoofed source_hash and every
    sender-identity check in the core managers, so the gate is exercised here
    at the router's real entry point.
    """

    def _chat_lxm(self, sender, recipient, ch_hash, content, msg_id, ts=None):
        dest = RNS.Destination(
            recipient.identity.rns_identity, RNS.Destination.OUT,
            RNS.Destination.SINGLE, "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(dest, sender.router.delivery_destination, content,
                             desired_method=LXMF.LXMessage.DIRECT)
        ts = time.time() if ts is None else ts
        lxm.fields = pack_fields({
            F_CHANNEL_HASH: bytes.fromhex(ch_hash),
            F_DISPLAY_NAME: "Alice",
            F_TIMESTAMP:    ts,
            F_MESSAGE_ID:   msg_id,
            # Signed as the real sender: a genuine client's message carries
            # one, so the forgery tests below must differ only in the LXMF
            # signature they are actually about.
            F_AUTHOR_SIG:   sign_as(sender.identity.hash_hex, ch_hash, msg_id,
                                    ts, content),
        })
        return lxm

    def test_forged_chat_message_is_dropped(self, peer_factory):
        """
        A peer sets source_hash to Alice's delivery hash so that
        RNS.Identity.recall() resolves to Alice's real identity, making
        sender_hex look authentic to every downstream check.  LXMF flags the
        signature as invalid; the router must drop the message.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        lxm = self._chat_lxm(alice, bob, ch_hash, "spoofed", "forged-msg-1")

        bob.router._on_message_received(forge(lxm))
        time.sleep(0.3)

        ids = [m["message_id"] for m in bob.storage.get_messages(ch_hash)]
        assert "forged-msg-1" not in ids, \
            "Bob stored a message whose LXMF signature did not validate"

    def test_authentic_chat_message_is_delivered(self, peer_factory):
        """
        Positive control for the test above: the identical message with a
        valid signature must be stored.  Without this, a router that dropped
        everything would pass the forgery test.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        ts = time.time()
        msg_id = _compute_message_id("genuine", alice.identity.hash_hex, ts)
        lxm = self._chat_lxm(alice, bob, ch_hash, "genuine", msg_id, ts=ts)
        lxm.signature_validated = True

        bob.router._on_message_received(lxm)
        time.sleep(0.3)

        ids = [m["message_id"] for m in bob.storage.get_messages(ch_hash)]
        assert msg_id in ids, \
            "A correctly signed message was not delivered"

    def test_forged_member_list_update_never_reaches_invite_manager(self, peer_factory):
        """
        The member list document carries its own Ed25519 signatures, but the
        router must not hand an unauthenticated message to the invite manager
        at all -- defence in depth, and the same gate protects the message
        types that have no inner signature.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        # Let the legitimate member list doc from setup land first, otherwise
        # its callback is what we would end up observing.
        time.sleep(0.5)
        seen: list = []
        bob.invite_mgr.add_member_list_callback(lambda ch: seen.append(ch))

        dest = RNS.Destination(
            bob.identity.rns_identity, RNS.Destination.OUT,
            RNS.Destination.SINGLE, "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(dest, alice.router.delivery_destination, "",
                             desired_method=LXMF.LXMessage.DIRECT)
        lxm.fields = {
            F_MSG_TYPE:        MT_MEMBER_LIST_UPDATE,
            F_CHANNEL_HASH:    bytes.fromhex(ch_hash),
            F_MEMBER_LIST_DOC: msgpack.packb({"version": 99}, use_bin_type=True),
        }

        bob.router._on_message_received(forge(lxm))
        time.sleep(0.3)

        assert not seen, "An unauthenticated member list update was processed"

    def test_unknown_source_is_quarantined_not_dispatched(self, peer_factory):
        """
        A message whose sender identity is not yet known is not a forgery --
        we simply cannot check it yet.  It must be withheld from the delivery
        callbacks rather than trusted, and held for later re-validation.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        lxm = self._chat_lxm(alice, bob, ch_hash, "unknown source", "unknown-src-1")
        lxm.signature_validated = False
        lxm.unverified_reason = LXMF.LXMessage.SOURCE_UNKNOWN
        lxm.packed = b"\x00" * 32  # non-empty so it is held rather than discarded

        bob.router._on_message_received(lxm)
        time.sleep(0.3)

        ids = [m["message_id"] for m in bob.storage.get_messages(ch_hash)]
        assert "unknown-src-1" not in ids, \
            "A message with an unverifiable source was delivered"
        held = sum(len(v) for v in bob.router._quarantine.values())
        assert held == 1, f"Expected the message to be quarantined, found {held}"

    def test_quarantine_release_rejects_still_invalid_signature(self, peer_factory):
        """
        Arrival of the sender's identity is not itself evidence the message was
        genuine.  On release the signature must be re-checked against the newly
        known identity, and a message that still fails must be discarded.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        lxm = self._chat_lxm(alice, bob, ch_hash, "still bad", "release-bad-1")
        lxm.signature_validated = False
        lxm.unverified_reason = LXMF.LXMessage.SOURCE_UNKNOWN
        lxm.packed = b"\x01" * 64  # garbage: re-unpack fails or does not validate

        bob.router._on_message_received(lxm)
        assert sum(len(v) for v in bob.router._quarantine.values()) == 1

        bob.router.release_quarantined(alice.identity.hash_hex)
        time.sleep(0.3)

        ids = [m["message_id"] for m in bob.storage.get_messages(ch_hash)]
        assert "release-bad-1" not in ids, \
            "A quarantined message was delivered without re-validating its signature"

    def test_a_genuine_held_message_is_delivered_once_the_identity_resolves(
            self, peer_factory):
        """The other half of the quarantine: it is a delay, not a bin.

        A first message from a peer we have never heard is unverifiable when it
        lands, so it is held. Once their identity resolves -- by announce, or
        by the path response the quarantine itself requests -- the message must
        be re-validated and delivered. Without that release nothing ever
        arrives from a peer who has not announced recently, which is exactly
        how a freshly started client's invites went missing.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        content = "held until you knew me"
        ts = time.time()
        msg_id = _compute_message_id(content, alice.identity.hash_hex, ts)
        lxm = self._chat_lxm(alice, bob, ch_hash, content, msg_id, ts=ts)
        # Real packed bytes, so the release path has something it can genuinely
        # re-validate -- the other quarantine tests deliberately use garbage.
        lxm.pack()
        lxm.signature_validated = False
        lxm.unverified_reason = LXMF.LXMessage.SOURCE_UNKNOWN

        bob.router._on_message_received(lxm)
        assert sum(len(v) for v in bob.router._quarantine.values()) == 1
        assert not bob.storage.message_exists(msg_id)

        bob.router.release_quarantined(alice.identity.hash_hex)

        assert wait_for(lambda: bob.storage.message_exists(msg_id)), \
            "A held message was never delivered after its sender became known"

    def test_quarantine_is_bounded_per_sender(self, peer_factory):
        """
        The quarantine must not become its own memory-exhaustion vector: a
        peer that never announces can otherwise pin unbounded messages.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        for i in range(QUARANTINE_MAX_PER_SENDER * 3):
            lxm = self._chat_lxm(alice, bob, ch_hash, f"flood {i}", f"flood-{i}")
            lxm.signature_validated = False
            lxm.unverified_reason = LXMF.LXMessage.SOURCE_UNKNOWN
            lxm.packed = b"\x02" * 32
            bob.router._on_message_received(lxm)

        held = sum(len(v) for v in bob.router._quarantine.values())
        assert held <= QUARANTINE_MAX_PER_SENDER, \
            f"Quarantine grew to {held}, above the per-sender cap"


# ---------------------------------------------------------------------------
# SYNC INJECTION — unsolicited history and unauthorised hints
# ---------------------------------------------------------------------------

class TestAdversarialSyncInjection:
    """
    A sync response writes messages into the channel transcript with the
    sender attribution taken from its own payload.  Nothing in the payload is
    signed, so accepting one that answers no request lets any peer forge
    history from any author.
    """

    def _payload(self, sender_hex, content, ts, msg_id, ch_hash=None):
        """A sync payload, signed as its claimed author when ch_hash is given.

        Tests that expect the response to be refused earlier (unsolicited,
        replayed) can leave it unsigned -- the gate under test fires first.
        """
        row = {
            "sender_hash":  sender_hex,
            "sender_name":  "Alice",
            "content":      content,
            "timestamp":    ts,
            "message_id":   msg_id,
            "reply_to":     None,
            "last_seen_id": None,
        }
        if ch_hash:
            row["author_sig"] = sign_as(sender_hex, ch_hash, msg_id, ts, content)
        return {0x08: msgpack.packb([row], use_bin_type=True)}

    def test_unsolicited_sync_response_is_rejected(self, peer_factory):
        """Carol pushes history for a channel nobody asked her for."""
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        carol = peer_factory("carol")
        ts = time.time() - 60

        bob.sync_mgr._handle_sync_response(
            self._payload(alice.identity.hash_hex, "injected", ts, "injected-1"),
            ch_hash,
            carol.identity.hash_hex,
        )

        assert not bob.storage.message_exists("injected-1"), \
            "An unsolicited sync response injected a message into the transcript"

    def test_sync_response_cannot_be_replayed(self, peer_factory):
        """
        One request must authorise exactly one response, otherwise a single
        legitimate request becomes a standing licence to inject.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        ts = time.time() - 60

        first_id = _compute_message_id("first", alice.identity.hash_hex, ts)
        second_id = _compute_message_id("second", alice.identity.hash_hex, ts)

        bob.sync_mgr._record_pending_request(ch_hash, alice.identity.hash_hex)
        bob.sync_mgr._handle_sync_response(
            self._payload(alice.identity.hash_hex, "first", ts, first_id, ch_hash),
            ch_hash, alice.identity.hash_hex,
        )
        bob.sync_mgr._handle_sync_response(
            self._payload(alice.identity.hash_hex, "second", ts, second_id, ch_hash),
            ch_hash, alice.identity.hash_hex,
        )

        assert bob.storage.message_exists(first_id), \
            "The solicited response was rejected"
        assert not bob.storage.message_exists(second_id), \
            "A second response was accepted against a single consumed request"

    def test_unsolicited_empty_response_cannot_claim_we_are_synced(self, peer_factory):
        """
        The empty sync response is what lets a channel report itself up to
        date.  A peer we never asked must not be able to assert that for us --
        it would hide a real gap behind a green status.
        """
        from trenchchat.core.sync_status import SyncState

        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        carol = peer_factory("carol")

        bob.sync_mgr._handle_sync_response(
            {0x08: msgpack.packb([], use_bin_type=True)},
            ch_hash,
            carol.identity.hash_hex,
        )

        assert bob.sync_mgr.status.get_state(ch_hash) != SyncState.SYNCED, \
            "An unsolicited empty response marked the channel as fully synced"

    def test_endless_truncation_cannot_drive_unbounded_requests(self, peer_factory):
        """
        A responder that flags every batch truncated is asking us to keep
        requesting.  The chain has to stop on its own, or one peer can make us
        transmit indefinitely.
        """
        from trenchchat.core.sync import MAX_SYNC_CONTINUATIONS

        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )

        requests = 0
        original = bob.sync_mgr._send_raw

        def counting_send_raw(dest_hex, fields):
            nonlocal requests
            if fields.get(0x10) == "sync_request":
                requests += 1
            return original(dest_hex, fields)

        bob.sync_mgr._send_raw = counting_send_raw

        resume = time.time()
        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, resume)
        for _ in range(MAX_SYNC_CONTINUATIONS * 3):
            resume += 10
            bob.sync_mgr._handle_sync_response(
                {
                    0x08: msgpack.packb([], use_bin_type=True),
                    0x50: True,
                    0x51: resume,
                },
                ch_hash,
                alice.identity.hash_hex,
            )

        assert requests <= MAX_SYNC_CONTINUATIONS + 1, (
            f"a peer flagging every batch truncated drove {requests} requests, "
            f"past the {MAX_SYNC_CONTINUATIONS} continuation budget"
        )

    def test_repeated_resume_point_does_not_chain(self, peer_factory):
        """
        A truncated response that doesn't actually move the resume point has
        nothing more to give; continuing would just loop on the same batch.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )

        requests = 0
        original = bob.sync_mgr._send_raw

        def counting_send_raw(dest_hex, fields):
            nonlocal requests
            if fields.get(0x10) == "sync_request":
                requests += 1
            return original(dest_hex, fields)

        bob.sync_mgr._send_raw = counting_send_raw

        window_start = time.time()
        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, window_start)
        bob.sync_mgr._handle_sync_response(
            {
                0x08: msgpack.packb([], use_bin_type=True),
                0x50: True,
                0x51: window_start,
            },
            ch_hash,
            alice.identity.hash_hex,
        )

        assert requests == 1, \
            "a response that repeated its own resume point still chained a request"

    def test_missed_delivery_hint_from_non_member_is_rejected(self, peer_factory):
        """
        Hints steer which messages we later serve and are written straight to
        storage, so an outsider must not be able to seed them.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        carol = peer_factory("carol")

        bob.sync_mgr._handle_missed_delivery(
            {0x06: alice.identity.hash_hex, 0x07: "hint-msg-1"},
            ch_hash,
            carol.identity.hash_hex,
        )

        hinted = bob.storage.get_missed_message_ids(ch_hash, alice.identity.hash_hex)
        assert "hint-msg-1" not in hinted, \
            "A non-member seeded a missed-delivery hint"


# ---------------------------------------------------------------------------
# REACTIONS — membership and SEND_MESSAGE
# ---------------------------------------------------------------------------

class TestAdversarialReactions:
    """
    A reaction is a write into the channel attributed to the sender, so it
    needs the same authorisation a message does.  Previously the only check
    was is_subscribed, which says nothing about the *sender*.
    """

    def _react(self, peer, target, ch_hash, msg_id, emoji_hash, remove=False):
        dest = RNS.Destination(
            target.identity.rns_identity, RNS.Destination.OUT,
            RNS.Destination.SINGLE, "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(dest, peer.router.delivery_destination, "",
                             desired_method=LXMF.LXMessage.DIRECT)
        lxm.fields = {
            F_MSG_TYPE:         MT_REACTION,
            F_CHANNEL_HASH:     bytes.fromhex(ch_hash),
            F_REACTION_MSG_ID:  msg_id,
            F_EMOJI_HASH:       bytes.fromhex(emoji_hash),
            F_REACTION_REMOVE:  remove,
        }
        lxm.signature_validated = True
        target.router._on_message_received(lxm)
        time.sleep(0.2)

    def test_non_member_reaction_is_rejected(self, peer_factory):
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        carol = peer_factory("carol")
        emoji_hash = "ab" * 32

        self._react(carol, bob, ch_hash, "target-msg", emoji_hash)

        rows = bob.storage.get_reactions("target-msg")
        assert not any(r["reactor_hash"] == carol.identity.hash_hex for r in rows), \
            "A non-member's reaction was stored on an invite-only channel"

    def test_member_without_send_message_cannot_react(self, peer_factory):
        """Bob is a member but his send_message was revoked; Alice must
        refuse his reaction just as she refuses his messages."""
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[]  # member, but send_message revoked
        )
        emoji_hash = "cd" * 32

        self._react(bob, alice, ch_hash, "target-msg-2", emoji_hash)

        rows = alice.storage.get_reactions("target-msg-2")
        assert not any(r["reactor_hash"] == bob.identity.hash_hex for r in rows), \
            "A member without send_message had their reaction stored"

    def _react_unicode(self, peer, target, ch_hash, msg_id, emoji):
        """Same as _react, but over the unicode reaction field."""
        dest = RNS.Destination(
            target.identity.rns_identity, RNS.Destination.OUT,
            RNS.Destination.SINGLE, "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(dest, peer.router.delivery_destination, "",
                             desired_method=LXMF.LXMessage.DIRECT)
        lxm.fields = {
            F_MSG_TYPE:          MT_REACTION,
            F_CHANNEL_HASH:      bytes.fromhex(ch_hash),
            F_REACTION_MSG_ID:   msg_id,
            F_REACTION_UNICODE:  emoji,
            F_REACTION_REMOVE:   False,
        }
        lxm.signature_validated = True
        target.router._on_message_received(lxm)
        time.sleep(0.2)

    def test_non_member_unicode_reaction_is_rejected(self, peer_factory):
        """The unicode reaction field is gated exactly like the hash field."""
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        carol = peer_factory("carol")

        self._react_unicode(carol, bob, ch_hash, "target-msg-3", "\U0001F44D")

        rows = bob.storage.get_reactions("target-msg-3")
        assert not any(r["reactor_hash"] == carol.identity.hash_hex for r in rows), \
            "A non-member's unicode reaction was stored on an invite-only channel"

    def test_member_without_send_message_cannot_react_with_unicode(self, peer_factory):
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[]
        )

        self._react_unicode(bob, alice, ch_hash, "target-msg-4", "\U0001F44D")

        rows = alice.storage.get_reactions("target-msg-4")
        assert not any(r["reactor_hash"] == bob.identity.hash_hex for r in rows), \
            "A member without send_message had their unicode reaction stored"


# ---------------------------------------------------------------------------
# RESOURCE / PAYLOAD LIMITS
# ---------------------------------------------------------------------------

class TestAdversarialPayloadLimits:
    def test_oversized_inbound_image_is_dropped(self, peer_factory):
        """
        Attachment bytes are stored and later handed to the client's image
        decoders. Avatars and emoji are both capped on receipt; message
        images were not.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        dest = RNS.Destination(
            bob.identity.rns_identity, RNS.Destination.OUT,
            RNS.Destination.SINGLE, "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(dest, alice.router.delivery_destination, "huge",
                             desired_method=LXMF.LXMessage.DIRECT)
        ts = time.time()
        oversized = b"\x00" * (MAX_IMAGE_BYTES + 1)
        msg_id = _compute_message_id("huge", alice.identity.hash_hex, ts)
        lxm.fields = pack_fields({
            F_CHANNEL_HASH: bytes.fromhex(ch_hash),
            F_DISPLAY_NAME: "Alice",
            F_TIMESTAMP:    ts,
            F_MESSAGE_ID:   msg_id,
            F_IMAGE_DATA:   oversized,
            F_AUTHOR_SIG:   sign_as(alice.identity.hash_hex, ch_hash,
                                    msg_id, ts, "huge",
                                    image_data=oversized),
        })
        lxm.signature_validated = True

        bob.router._on_message_received(lxm)
        time.sleep(0.3)

        rows = [m for m in bob.storage.get_messages(ch_hash)
                if m["message_id"] == msg_id]
        assert rows, "The message itself should still be delivered"
        assert not rows[0]["image_data"], \
            "An over-cap image attachment was stored"


# ---------------------------------------------------------------------------
# ADMIN ADVERSARY — a trusted signer exceeding their own permissions
# ---------------------------------------------------------------------------

def _setup_channel_with_admin(peer_factory, *, admin_perms=None):
    """Alice (owner) and Bob (ADMIN) on a shared invite-only channel.

    Every other adversary in this file is a plain member, which is why the
    admin boundary went unchecked: a valid signature proves who wrote a
    document, not that they were allowed to write it.
    """
    alice = peer_factory("alice")
    bob = peer_factory("bob")

    perms = dict(PRESET_PRIVATE)
    if admin_perms is not None:
        perms[ROLE_ADMIN] = list(admin_perms)

    ch_hash = alice.channel_mgr.create_channel("admin-ch", "", permissions=perms)
    alice.invite_mgr.publish_member_list(
        ch_hash, add_members=[bob.identity.hash], add_admins=[bob.identity.hash]
    )
    assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)
    return alice, bob, ch_hash


class TestAdversarialAdminSigner:
    def _doc(self, ch_hash, version, members, admins, owners, signer,
             permissions=b""):
        published_at = time.time()
        payload = _signed_payload(
            bytes.fromhex(ch_hash), version, published_at,
            members, admins, owners, permissions,
        )
        return {
            "channel_hash": bytes.fromhex(ch_hash),
            "version":      version,
            "published_at": published_at,
            "members":      members,
            "admins":       admins,
            "owners":       owners,
            "permissions":  permissions,
            "signatures":   {signer.identity.hash: _sign(
                signer.identity.rns_identity, payload)},
        }

    def test_admin_cannot_promote_self_to_owner(self, peer_factory):
        alice, bob, ch_hash = _setup_channel_with_admin(peer_factory)
        v = alice.storage.get_member_list_version(ch_hash)["version"]

        doc = self._doc(
            ch_hash, v + 1,
            members=[alice.identity.hash, bob.identity.hash],
            admins=[alice.identity.hash, bob.identity.hash],
            owners=[alice.identity.hash, bob.identity.hash],  # Bob adds himself
            signer=bob,
        )
        assert not alice.invite_mgr._accept_document(doc, ch_hash), \
            "An admin promoted themselves to owner"
        assert alice.storage.get_role(ch_hash, bob.identity.hash_hex) != ROLE_OWNER

    def test_admin_cannot_demote_the_owner(self, peer_factory):
        alice, bob, ch_hash = _setup_channel_with_admin(peer_factory)
        v = alice.storage.get_member_list_version(ch_hash)["version"]

        doc = self._doc(
            ch_hash, v + 1,
            members=[alice.identity.hash, bob.identity.hash],
            admins=[bob.identity.hash],
            owners=[bob.identity.hash],  # Alice removed as owner
            signer=bob,
        )
        assert not alice.invite_mgr._accept_document(doc, ch_hash), \
            "An admin demoted the channel owner"
        assert alice.storage.get_role(ch_hash, alice.identity.hash_hex) == ROLE_OWNER

    def test_admin_cannot_depose_the_owner_by_dropping_them_from_members(
            self, peer_factory):
        """
        The owners list is left byte-identical, so the owner-set gate never
        fires -- but a role is derived from membership, so an owner absent
        from members has no row and therefore no permissions at all.
        """
        alice, bob, ch_hash = _setup_channel_with_admin(peer_factory)
        v = alice.storage.get_member_list_version(ch_hash)["version"]

        doc = self._doc(
            ch_hash, v + 1,
            members=[bob.identity.hash],                      # Alice dropped
            admins=[alice.identity.hash, bob.identity.hash],  # unchanged
            owners=[alice.identity.hash],                     # unchanged
            signer=bob,
        )
        assert not alice.invite_mgr._accept_document(doc, ch_hash), \
            "KICK alone removed the channel owner from the member list"
        assert alice.storage.is_member(ch_hash, alice.identity.hash_hex)
        assert alice.storage.get_role(ch_hash, alice.identity.hash_hex) == ROLE_OWNER
        assert alice.storage.has_permission(ch_hash, alice.identity.hash_hex, KICK)

    def test_kick_alone_cannot_remove_a_fellow_admin(self, peer_factory):
        """KICK says you may remove a member; removing an admin is MANAGE_ROLES.

        The admins list is left byte-identical to stored, so the MANAGE_ROLES
        gate on the admin *set* never fires -- dropping Carol from members is
        what strips her, and only the removal rules can catch it.
        """
        alice, bob, ch_hash = _setup_channel_with_admin(
            peer_factory, admin_perms=[SEND_MESSAGE, KICK]
        )
        carol = peer_factory("carol")
        alice.invite_mgr.publish_member_list(
            ch_hash, add_members=[carol.identity.hash],
            add_admins=[carol.identity.hash],
        )
        assert wait_for_member(alice.storage, ch_hash, carol.identity.hash_hex)
        stored = msgpack.unpackb(
            alice.storage.get_member_list_version(ch_hash)["document_blob"], raw=True
        )
        v = alice.storage.get_member_list_version(ch_hash)["version"]

        doc = self._doc(
            ch_hash, v + 1,
            members=[alice.identity.hash, bob.identity.hash],  # Carol dropped
            admins=list(stored[b"admins"]),                    # unchanged
            owners=list(stored[b"owners"]),                    # unchanged
            signer=bob,
        )
        assert not alice.invite_mgr._accept_document(doc, ch_hash), \
            "An admin holding only KICK removed a fellow admin"
        assert alice.storage.is_member(ch_hash, carol.identity.hash_hex)

    def test_an_admin_may_still_remove_a_plain_member(self, peer_factory):
        """The guard above must not block the ordinary kick it sits beside."""
        alice, bob, ch_hash = _setup_channel_with_admin(peer_factory)
        carol = peer_factory("carol")
        alice.invite_mgr.publish_member_list(ch_hash, add_members=[carol.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, carol.identity.hash_hex)
        v = alice.storage.get_member_list_version(ch_hash)["version"]

        doc = self._doc(
            ch_hash, v + 1,
            members=[alice.identity.hash, bob.identity.hash],  # Carol kicked
            admins=[alice.identity.hash, bob.identity.hash],
            owners=[alice.identity.hash],
            signer=bob,
        )
        assert alice.invite_mgr._accept_document(doc, ch_hash), \
            "A legitimate kick by an admin was refused"
        assert not alice.storage.is_member(ch_hash, carol.identity.hash_hex)

    def test_a_non_finite_version_is_refused(self, peer_factory):
        """
        version is signed wire data driving an ordering comparison. Stored,
        float("inf") compares greater than every later document forever.
        """
        alice, bob, ch_hash = _setup_channel_with_admin(peer_factory)
        stored = alice.storage.get_member_list_version(ch_hash)["version"]

        doc = self._doc(
            ch_hash, float("inf"),
            members=[alice.identity.hash, bob.identity.hash],
            admins=[alice.identity.hash, bob.identity.hash],
            owners=[alice.identity.hash],
            signer=bob,
        )
        assert not alice.invite_mgr._accept_document(doc, ch_hash), \
            "A document with an infinite version was accepted"
        assert alice.storage.get_member_list_version(ch_hash)["version"] == stored

    def test_a_poisoned_version_cannot_freeze_the_channel(self, peer_factory):
        """The point of refusing it: ordinary updates must still apply after."""
        alice, bob, ch_hash = _setup_channel_with_admin(peer_factory)
        carol = peer_factory("carol")
        alice.invite_mgr.publish_member_list(ch_hash, add_members=[carol.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, carol.identity.hash_hex)

        v = alice.storage.get_member_list_version(ch_hash)["version"]
        alice.invite_mgr._accept_document(
            self._doc(
                ch_hash, float("inf"),
                members=[alice.identity.hash, bob.identity.hash, carol.identity.hash],
                admins=[alice.identity.hash, bob.identity.hash],
                owners=[alice.identity.hash],
                signer=bob,
            ),
            ch_hash,
        )

        follow_up = self._doc(
            ch_hash, v + 1,
            members=[alice.identity.hash, bob.identity.hash],  # Carol kicked
            admins=[alice.identity.hash, bob.identity.hash],
            owners=[alice.identity.hash],
            signer=bob,
        )
        assert alice.invite_mgr._accept_document(follow_up, ch_hash), \
            "A poisoned version froze every later document"
        assert not alice.storage.is_member(ch_hash, carol.identity.hash_hex)

    def test_an_implausible_published_at_is_refused(self, peer_factory):
        """Signed over the bad value, so this cannot pass on a bad signature."""
        alice, bob, ch_hash = _setup_channel_with_admin(peer_factory)
        stored = alice.storage.get_member_list_version(ch_hash)["version"]

        members = [alice.identity.hash, bob.identity.hash]
        admins = [alice.identity.hash, bob.identity.hash]
        owners = [alice.identity.hash]
        published_at = float("inf")
        payload = _signed_payload(
            bytes.fromhex(ch_hash), stored + 1, published_at,
            members, admins, owners, b"",
        )
        doc = {
            "channel_hash": bytes.fromhex(ch_hash),
            "version":      stored + 1,
            "published_at": published_at,
            "members":      members,
            "admins":       admins,
            "owners":       owners,
            "permissions":  b"",
            "signatures":   {bob.identity.hash: _sign(
                bob.identity.rns_identity, payload)},
        }
        assert not alice.invite_mgr._accept_document(doc, ch_hash), \
            "A document with an infinite published_at was accepted"
        assert alice.storage.get_member_list_version(ch_hash)["version"] == stored

    def test_admin_without_manage_channel_cannot_rewrite_permissions(self, peer_factory):
        """
        PRESET_PRIVATE does not grant admins MANAGE_CHANNEL, yet the receiver
        applied any permissions blob carried by a doc from any trusted signer.
        """
        alice, bob, ch_hash = _setup_channel_with_admin(
            peer_factory, admin_perms=[SEND_MESSAGE, KICK, MANAGE_ROLES]
        )
        v = alice.storage.get_member_list_version(ch_hash)["version"]
        before = alice.storage.get_channel_permissions(ch_hash)

        evil = dict(PRESET_PRIVATE)
        evil[ROLE_MEMBER] = list(ALL_PERMISSIONS)
        doc = self._doc(
            ch_hash, v + 1,
            members=[alice.identity.hash, bob.identity.hash],
            admins=[alice.identity.hash, bob.identity.hash],
            owners=[alice.identity.hash],
            signer=bob,
            permissions=msgpack.packb(evil, use_bin_type=True),
        )
        assert not alice.invite_mgr._accept_document(doc, ch_hash), \
            "An admin without manage_channel rewrote the permission set"
        assert alice.storage.get_channel_permissions(ch_hash) == before

    def test_admin_without_kick_cannot_remove_members(self, peer_factory):
        alice, bob, ch_hash = _setup_channel_with_admin(
            peer_factory, admin_perms=[SEND_MESSAGE]  # no KICK
        )
        carol = peer_factory("carol")
        alice.invite_mgr.publish_member_list(ch_hash, add_members=[carol.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, carol.identity.hash_hex)
        v = alice.storage.get_member_list_version(ch_hash)["version"]

        doc = self._doc(
            ch_hash, v + 1,
            members=[alice.identity.hash, bob.identity.hash],  # Carol dropped
            admins=[alice.identity.hash, bob.identity.hash],
            owners=[alice.identity.hash],
            signer=bob,
        )
        assert not alice.invite_mgr._accept_document(doc, ch_hash), \
            "An admin without kick removed a member"
        assert alice.storage.is_member(ch_hash, carol.identity.hash_hex)

    def test_document_cannot_strip_all_authority(self, peer_factory):
        """
        A doc with no admins and no owners leaves trusted_signers empty, after
        which no further update can ever validate -- the channel is bricked.
        """
        alice, bob, ch_hash = _setup_channel_with_admin(
            peer_factory, admin_perms=list(ALL_PERMISSIONS)
        )
        v = alice.storage.get_member_list_version(ch_hash)["version"]

        doc = self._doc(
            ch_hash, v + 1,
            members=[alice.identity.hash, bob.identity.hash],
            admins=[], owners=[],
            signer=bob,
        )
        assert not alice.invite_mgr._accept_document(doc, ch_hash), \
            "A document stripped every admin and owner from the channel"

    def test_owner_can_still_manage_owners(self, peer_factory):
        """Positive control: the gates above must not block the owner."""
        alice, bob, ch_hash = _setup_channel_with_admin(peer_factory)
        v = alice.storage.get_member_list_version(ch_hash)["version"]

        doc = self._doc(
            ch_hash, v + 1,
            members=[alice.identity.hash, bob.identity.hash],
            admins=[alice.identity.hash, bob.identity.hash],
            owners=[alice.identity.hash, bob.identity.hash],
            signer=alice,
        )
        assert alice.invite_mgr._accept_document(doc, ch_hash), \
            "The owner was blocked from changing the owner list"


# ---------------------------------------------------------------------------
# INVITE TOKEN REUSE AND REVOCATION
# ---------------------------------------------------------------------------

class TestAdversarialTokenReuse:
    """
    The token is an unforgeable Ed25519 signature bound to invitee, channel
    and expiry — but it is a bearer credential, so unforgeable is not the same
    as un-replayable.
    """

    def _join(self, admin, joiner, ch_hash, token, expiry, sender_hex=None):
        admin.invite_mgr._handle_join_request(
            {
                F_INVITE_TOKEN: token,
                F_INVITEE_HASH: joiner.identity.hash,
                F_EXPIRY_TS:    expiry,
                F_ADMIN_HASH:   admin.identity.hash,
            },
            ch_hash,
            joiner.identity.hash_hex if sender_hex is None else sender_hex,
        )
        time.sleep(0.2)

    def test_token_is_single_use(self, peer_factory):
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        carol = peer_factory("carol")
        token, expiry = alice.invite_mgr.generate_invite_token(
            ch_hash, carol.identity.hash
        )

        self._join(alice, carol, ch_hash, token, expiry)
        assert alice.storage.is_member(ch_hash, carol.identity.hash_hex)

        # Kick her, then replay the very same token.
        alice.invite_mgr.publish_member_list(
            ch_hash, remove_members=[carol.identity.hash]
        )
        assert not alice.storage.is_member(ch_hash, carol.identity.hash_hex)

        self._join(alice, carol, ch_hash, token, expiry)
        assert not alice.storage.is_member(ch_hash, carol.identity.hash_hex), \
            "A kicked member re-joined by replaying their original invite token"

    def test_token_cannot_be_submitted_by_a_third_party(self, peer_factory):
        """
        The invitee was taken from the message body and never compared to the
        LXMF sender, so holding someone else's token was enough to force them
        into a channel without their participation.
        """
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        carol = peer_factory("carol")
        dave = peer_factory("dave")
        token, expiry = alice.invite_mgr.generate_invite_token(
            ch_hash, carol.identity.hash
        )

        # Dave submits Carol's token, claiming to be delivering it.
        self._join(alice, carol, ch_hash, token, expiry,
                   sender_hex=dave.identity.hash_hex)

        assert not alice.storage.is_member(ch_hash, carol.identity.hash_hex), \
            "A third party redeemed someone else's invite token"

    def test_kicked_admin_loses_signing_authority(self, peer_factory):
        """
        remove_members stripped only `members`, leaving the kicked admin in
        `admins` — and trusted_signers is derived from exactly that list, so
        they could sign themselves straight back in.
        """
        alice, bob, ch_hash = _setup_channel_with_admin(peer_factory)
        assert alice.storage.get_role(ch_hash, bob.identity.hash_hex) == ROLE_ADMIN

        alice.invite_mgr.publish_member_list(
            ch_hash, remove_members=[bob.identity.hash]
        )
        assert not alice.storage.is_member(ch_hash, bob.identity.hash_hex)

        # Bob signs a doc re-adding himself.
        v = alice.storage.get_member_list_version(ch_hash)["version"]
        published_at = time.time()
        members = [alice.identity.hash, bob.identity.hash]
        admins = [alice.identity.hash, bob.identity.hash]
        owners = [alice.identity.hash]
        payload = _signed_payload(
            bytes.fromhex(ch_hash), v + 1, published_at, members, admins,
            owners, b"",
        )
        doc = {
            "channel_hash": bytes.fromhex(ch_hash),
            "version":      v + 1,
            "published_at": published_at,
            "members":      members,
            "admins":       admins,
            "owners":       owners,
            "permissions":  b"",
            "signatures":   {bob.identity.hash: _sign(
                bob.identity.rns_identity, payload)},
        }

        assert not alice.invite_mgr._accept_document(doc, ch_hash), \
            "A kicked admin retained signing authority and re-added themselves"
        assert not alice.storage.is_member(ch_hash, bob.identity.hash_hex)


class TestAdversarialUnsolicitedChannelInjection:
    def test_doc_for_unknown_channel_not_naming_us_is_rejected(self, peer_factory):
        """
        A document for a channel hash the victim has never seen, signed by its
        own self-declared owner. Accepting it records the attacker as the sole
        trusted signer and auto-subscribes the victim.
        """
        alice = peer_factory("alice")
        mallory = peer_factory("mallory")
        ch_hash = "ab" * 16

        published_at = time.time()
        members = [mallory.identity.hash]
        payload = _signed_payload(
            bytes.fromhex(ch_hash), 1, published_at, members,
            [mallory.identity.hash], [mallory.identity.hash], b"",
        )
        doc = {
            "channel_hash": bytes.fromhex(ch_hash),
            "version":      1,
            "published_at": published_at,
            "members":      members,
            "admins":       [mallory.identity.hash],
            "owners":       [mallory.identity.hash],
            "permissions":  b"",
            "signatures":   {mallory.identity.hash: _sign(
                mallory.identity.rns_identity, payload)},
        }

        assert not alice.invite_mgr._accept_document(doc, ch_hash), \
            "An unsolicited member list doc for an unknown channel was accepted"
        assert alice.storage.get_channel(ch_hash) is None
        assert not alice.storage.is_subscribed(ch_hash), \
            "Victim was auto-subscribed to an attacker-defined channel"

    def test_doc_naming_us_is_held_not_applied(self, peer_factory):
        """
        A document that *does* name us is the unilateral-add case. It is held
        for confirmation, not applied: nothing is joined until the user says so.
        """
        alice = peer_factory("alice")
        mallory = peer_factory("mallory")
        ch_hash = "cd" * 16

        published_at = time.time()
        members = [mallory.identity.hash, alice.identity.hash]
        payload = _signed_payload(
            bytes.fromhex(ch_hash), 1, published_at, members,
            [mallory.identity.hash], [mallory.identity.hash], b"",
        )
        doc = {
            "channel_hash": bytes.fromhex(ch_hash),
            "version":      1,
            "published_at": published_at,
            "members":      members,
            "admins":       [mallory.identity.hash],
            "owners":       [mallory.identity.hash],
            "permissions":  b"",
            "signatures":   {mallory.identity.hash: _sign(
                mallory.identity.rns_identity, payload)},
        }

        alice.invite_mgr._hold_for_confirmation(doc, ch_hash, {})

        assert not alice.storage.is_subscribed(ch_hash), \
            "A held document auto-subscribed the victim"
        assert alice.storage.get_channel(ch_hash) is None
        assert not alice.storage.is_member(ch_hash, alice.identity.hash_hex)
        pending = [p["channel_hash"] for p
                   in alice.invite_mgr.list_pending_memberships()]
        assert ch_hash in pending, "The document was not held for confirmation"

    def test_declining_a_held_doc_applies_nothing(self, peer_factory):
        alice = peer_factory("alice")
        mallory = peer_factory("mallory")
        ch_hash = "ce" * 16
        published_at = time.time()
        members = [mallory.identity.hash, alice.identity.hash]
        payload = _signed_payload(
            bytes.fromhex(ch_hash), 1, published_at, members,
            [mallory.identity.hash], [mallory.identity.hash], b"",
        )
        doc = {
            "channel_hash": bytes.fromhex(ch_hash),
            "version":      1,
            "published_at": published_at,
            "members":      members,
            "admins":       [mallory.identity.hash],
            "owners":       [mallory.identity.hash],
            "permissions":  b"",
            "signatures":   {mallory.identity.hash: _sign(
                mallory.identity.rns_identity, payload)},
        }
        alice.invite_mgr._hold_for_confirmation(doc, ch_hash, {})

        alice.invite_mgr.decline_pending_membership(ch_hash)

        assert not alice.invite_mgr.list_pending_memberships()
        assert not alice.storage.is_subscribed(ch_hash)
        assert alice.storage.get_channel(ch_hash) is None

    def test_accepted_invite_anchors_the_first_document(self, peer_factory):
        """
        Positive control: once the user acts on an invite, the document that
        comes back from that admin is anchored to them and accepted.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        ch_hash = alice.channel_mgr.create_channel("anchored-ch", "", "invite")

        token, expiry = alice.invite_mgr.generate_invite_token(
            ch_hash, bob.identity.hash
        )
        bob.invite_mgr.send_join_request(
            ch_hash, token, expiry, alice.identity.hash_hex
        )

        assert wait_for(
            lambda: bob.storage.is_member(ch_hash, bob.identity.hash_hex),
            timeout=5,
        ), "Bob did not join via the real invite flow"


class TestAdversarialSubscriberList:
    """
    The subscriber set drives who outbound channel messages are delivered to,
    so forging or replaying it redirects or severs a peer's traffic.
    """

    def _setup(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        ch_hash = alice.channel_mgr.create_channel("open-ch", "", "public")
        bob.storage.upsert_channel(ch_hash, "open-ch", "", alice.identity.hash_hex,
                                   PRESET_OPEN, time.time())
        bob.storage.subscribe(ch_hash)
        return alice, bob, ch_hash

    def _send(self, sender, target, ch_hash, packed, version, sig):
        fields = {
            F_MSG_TYPE:           MT_SUBSCRIBER_LIST,
            F_CHANNEL_HASH:       bytes.fromhex(ch_hash),
            F_SUBSCRIBER_LIST:    packed,
            F_SUBSCRIBER_VERSION: version,
            F_SUBSCRIBER_SIG:     sig,
        }
        target.subscription_mgr._handle_subscriber_list(
            fields, ch_hash, sender.identity.hash_hex
        )

    def test_unsigned_subscriber_list_is_rejected(self, peer_factory):
        alice, bob, ch_hash = self._setup(peer_factory)
        packed = msgpack.packb(["cc" * 16], use_bin_type=True)

        bob.subscription_mgr._handle_subscriber_list(
            {
                F_MSG_TYPE:        MT_SUBSCRIBER_LIST,
                F_CHANNEL_HASH:    bytes.fromhex(ch_hash),
                F_SUBSCRIBER_LIST: packed,
            },
            ch_hash,
            alice.identity.hash_hex,
        )

        assert "cc" * 16 not in bob.subscription_mgr.get_subscribers(ch_hash), \
            "An unsigned subscriber list was applied"

    def test_forged_signature_is_rejected(self, peer_factory):
        alice, bob, ch_hash = self._setup(peer_factory)
        mallory = peer_factory("mallory")
        packed = msgpack.packb([mallory.identity.hash_hex], use_bin_type=True)
        payload = _subscriber_payload(ch_hash, 1, packed)
        # Signed by Mallory, but claiming to come from the owner.
        sig = _sign(mallory.identity.rns_identity, payload)

        self._send(alice, bob, ch_hash, packed, 1, sig)

        assert mallory.identity.hash_hex not in \
            bob.subscription_mgr.get_subscribers(ch_hash), \
            "A subscriber list with a forged owner signature was applied"

    def test_replayed_older_version_is_rejected(self, peer_factory):
        alice, bob, ch_hash = self._setup(peer_factory)

        def signed(members, version):
            packed = msgpack.packb(members, use_bin_type=True)
            sig = _sign(alice.identity.rns_identity,
                        _subscriber_payload(ch_hash, version, packed))
            return packed, version, sig

        current = ["aa" * 16, "bb" * 16]
        self._send(alice, bob, ch_hash, *signed(current, 5))
        assert set(bob.subscription_mgr.get_subscribers(ch_hash)) == set(current)

        # A genuine older list, replayed to resurrect a removed subscriber.
        self._send(alice, bob, ch_hash, *signed(["aa" * 16, "dd" * 16], 3))

        assert "dd" * 16 not in bob.subscription_mgr.get_subscribers(ch_hash), \
            "An older signed subscriber list was replayed successfully"

    def test_replay_is_still_rejected_after_a_restart(self, peer_factory):
        """The version watermark has to outlive the process.

        A captured older list stays validly signed forever, so if the
        watermark only lives in memory a restart re-opens the replay --
        resurrecting a removed subscriber, which is who delivery goes to.
        """
        alice, bob, ch_hash = self._setup(peer_factory)

        def signed(members, version):
            packed = msgpack.packb(members, use_bin_type=True)
            sig = _sign(alice.identity.rns_identity,
                        _subscriber_payload(ch_hash, version, packed))
            return packed, version, sig

        current = ["aa" * 16, "bb" * 16]
        self._send(alice, bob, ch_hash, *signed(current, 5))

        # Bob restarts: a fresh manager over the same storage, exactly as the
        # app rebuilds it. The roster is persisted, so the watermark must be.
        restarted = SubscriptionManager(bob.identity, bob.storage, bob.router)
        assert set(restarted.get_subscribers(ch_hash)) == set(current)

        restarted._handle_subscriber_list(
            {
                F_MSG_TYPE:           MT_SUBSCRIBER_LIST,
                F_CHANNEL_HASH:       bytes.fromhex(ch_hash),
                F_SUBSCRIBER_LIST:    signed(["aa" * 16, "dd" * 16], 3)[0],
                F_SUBSCRIBER_VERSION: 3,
                F_SUBSCRIBER_SIG:     signed(["aa" * 16, "dd" * 16], 3)[2],
            },
            ch_hash,
            alice.identity.hash_hex,
        )

        assert "dd" * 16 not in restarted.get_subscribers(ch_hash), \
            "An older signed subscriber list was replayed across a restart"
        assert set(restarted.get_subscribers(ch_hash)) == set(current)


# ---------------------------------------------------------------------------
# SERVERS
#
# A server roster is a signed list of channel hashes that the receiver turns
# into local channel rows re-parented under that server. Every entry is a
# capability claim, so each one is checked three independent ways.
# ---------------------------------------------------------------------------

from trenchchat.core.invite import encode_roster
from trenchchat.core.naming import channel_hash_for, server_hash_for
from trenchchat.core.permissions import CREATE_CHANNEL, PRESET_SERVER
from trenchchat.core.protocol import (
    F_CHANNEL_CREATOR, F_CHANNEL_NAME, F_SCOPE_KIND,
)


def _server_with_member(peer_factory, member_perms=None):
    """Alice owns a server; Bob is a member of it."""
    alice = peer_factory("alice")
    bob = peer_factory("bob")
    perms = dict(PRESET_SERVER)
    if member_perms is not None:
        perms[ROLE_MEMBER] = list(member_perms)
    s = actions.create_server(alice.server_mgr, alice.invite_mgr, "S", "", perms)

    def on_invite(scope_hex, name, token, expiry, admin_hex):
        bob.invite_mgr.send_join_request(scope_hex, token, expiry, admin_hex)

    bob.invite_mgr.add_invite_callback(on_invite)
    alice.invite_mgr.send_invite(s, bob.identity.hash_hex)
    assert wait_for_member(alice.storage, s, bob.identity.hash_hex, timeout=5)
    assert wait_for(lambda: bob.storage.get_server(s) is not None, timeout=5)
    # The server row lands with the invite; the member-list doc arrives in a
    # separate message, and the tests below read its stored version.
    assert wait_for(lambda: bob.storage.get_member_list_version(s) is not None, timeout=5)
    return alice, bob, s


def _server_doc(signer, server_hash: str, version: int, members, admins, owners,
                roster_rows, permissions_blob=b""):
    """A server member-list doc with a roster, signed by *signer*."""
    published_at = time.time()
    joined_at = {m: published_at for m in members}
    channels_blob = encode_roster(roster_rows)
    payload = _signed_payload(
        bytes.fromhex(server_hash), version, published_at,
        members, admins, owners=owners, permissions_blob=permissions_blob,
        joined_at=joined_at, channels_blob=channels_blob,
    )
    return {
        "channel_hash": bytes.fromhex(server_hash),
        "version":      version,
        "published_at": published_at,
        "members":      members,
        "admins":       admins,
        "owners":       owners,
        "permissions":  permissions_blob,
        "joined_at":    joined_at,
        "channels":     channels_blob,
        "signatures":   {signer.hash: _sign(signer, payload)},
    }


def _roster_row(creator_hash: bytes, name: str, ch_hash: str | None = None):
    return {
        "hash": ch_hash or channel_hash_for(creator_hash, name),
        "name": name,
        "description": "",
        "creator_hash": creator_hash.hex(),
        "created_at": time.time(),
    }


class TestAdversarialCreateChannel:
    def test_member_without_create_channel_cannot_create(self, peer_factory):
        """Outbound guard: a plain member calling the action directly."""
        alice, bob, s = _server_with_member(peer_factory)
        assert actions.create_channel_in_server(
            bob.storage, bob.channel_mgr, bob.invite_mgr,
            s, bob.identity.hash_hex, "sneaky",
        ) is None
        assert bob.storage.get_server_channels(s) == []

    def test_admin_without_create_channel_cannot_smuggle_roster_entry(self, peer_factory):
        """Core receiver enforcement: Bob is a *trusted signer* (admin) but the
        server's permissions deny admins CREATE_CHANNEL. His crafted document's
        member changes may apply; the roster addition must not."""
        perms = dict(PRESET_SERVER)
        perms[ROLE_ADMIN] = [SEND_MESSAGE, INVITE]          # no CREATE_CHANNEL
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "S", "", perms)

        def on_invite(scope_hex, name, token, expiry, admin_hex):
            bob.invite_mgr.send_join_request(scope_hex, token, expiry, admin_hex)
        bob.invite_mgr.add_invite_callback(on_invite)
        alice.invite_mgr.send_invite(s, bob.identity.hash_hex)
        assert wait_for_member(alice.storage, s, bob.identity.hash_hex, timeout=5)
        actions.update_membership(
            alice.storage, alice.invite_mgr, s, alice.identity.hash_hex,
            add_admins=[bob.identity.hash],
        )
        assert wait_for(
            lambda: alice.storage.get_role(s, bob.identity.hash_hex) == ROLE_ADMIN,
            timeout=5,
        )

        # An empty permissions blob asserts nothing, so the roster addition is
        # the only unauthorized change in the document and the CREATE_CHANNEL
        # gate is what has to catch it.
        existing = alice.storage.get_member_list_version(s)
        forged = _server_doc(
            bob.identity.rns_identity, s, existing["version"] + 1,
            members=[alice.identity.hash, bob.identity.hash],
            admins=[alice.identity.hash, bob.identity.hash],
            owners=[alice.identity.hash],
            roster_rows=[_roster_row(bob.identity.hash, "smuggled")],
        )
        assert alice.invite_mgr._accept_document(forged, s) is False, \
            "a document adding a channel without CREATE_CHANNEL was accepted"
        assert alice.storage.get_server_channels(s) == [], \
            "an admin without CREATE_CHANNEL smuggled a channel into the roster"

    def test_admin_with_create_channel_may_add_to_the_roster(self, peer_factory):
        """Control case: the gate must not reject a legitimate addition."""
        alice, bob, s = _server_with_member(peer_factory)
        actions.update_membership(
            alice.storage, alice.invite_mgr, s, alice.identity.hash_hex,
            add_admins=[bob.identity.hash],
        )
        assert wait_for(
            lambda: alice.storage.get_role(s, bob.identity.hash_hex) == ROLE_ADMIN,
            timeout=5,
        )
        existing = alice.storage.get_member_list_version(s)
        entry = _roster_row(bob.identity.hash, "legit")
        doc = _server_doc(
            bob.identity.rns_identity, s, existing["version"] + 1,
            members=[alice.identity.hash, bob.identity.hash],
            admins=[alice.identity.hash, bob.identity.hash],
            owners=[alice.identity.hash],
            roster_rows=[entry],
        )
        assert alice.invite_mgr._accept_document(doc, s) is True
        assert alice.storage.get_channel(entry["hash"]) is not None


class TestAdversarialRoster:
    def test_roster_cannot_adopt_an_existing_standalone_channel(self, peer_factory):
        """The headline attack: Mallory's roster names a private channel Bob is
        already in, trying to re-parent it and inherit its membership."""
        alice, bob, s = _server_with_member(peer_factory)

        secrets = actions.create_channel(
            bob.channel_mgr, bob.invite_mgr, "secrets", "", dict(PRESET_PRIVATE))
        assert bob.storage.get_channel(secrets)["server_hash"] is None

        existing = bob.storage.get_member_list_version(s)
        forged = _server_doc(
            alice.identity.rns_identity, s, existing["version"] + 1,
            members=[alice.identity.hash, bob.identity.hash],
            admins=[alice.identity.hash],
            owners=[alice.identity.hash],
            roster_rows=[_roster_row(bob.identity.hash, "secrets", ch_hash=secrets)],
            permissions_blob=msgpack.packb(dict(PRESET_SERVER), use_bin_type=True),
        )
        bob.invite_mgr._accept_document(forged, s)

        assert bob.storage.get_channel(secrets)["server_hash"] is None, \
            "an existing standalone channel was captured by a server roster"
        assert bob.storage.scope_for(secrets) == secrets
        assert bob.storage.is_member(secrets, alice.identity.hash_hex) is False, \
            "the attacker gained membership of the adopted channel"

    def test_roster_entry_not_bound_to_creator_is_dropped(self, peer_factory):
        """An entry whose hash isn't derivable from its claimed creator+name."""
        alice, bob, s = _server_with_member(peer_factory)
        existing = bob.storage.get_member_list_version(s)
        forged = _server_doc(
            alice.identity.rns_identity, s, existing["version"] + 1,
            members=[alice.identity.hash, bob.identity.hash],
            admins=[alice.identity.hash],
            owners=[alice.identity.hash],
            roster_rows=[_roster_row(alice.identity.hash, "fake", ch_hash="ab" * 16)],
            permissions_blob=msgpack.packb(dict(PRESET_SERVER), use_bin_type=True),
        )
        bob.invite_mgr._accept_document(forged, s)
        assert bob.storage.get_channel("ab" * 16) is None, \
            "a roster entry with a fabricated hash was materialised"

    def test_legitimate_roster_entry_is_accepted(self, peer_factory):
        """Control case — the defences must not reject honest rosters."""
        alice, bob, s = _server_with_member(peer_factory)
        existing = bob.storage.get_member_list_version(s)
        good = _roster_row(alice.identity.hash, "general")
        doc = _server_doc(
            alice.identity.rns_identity, s, existing["version"] + 1,
            members=[alice.identity.hash, bob.identity.hash],
            admins=[alice.identity.hash],
            owners=[alice.identity.hash],
            roster_rows=[good],
            permissions_blob=msgpack.packb(dict(PRESET_SERVER), use_bin_type=True),
        )
        assert bob.invite_mgr._accept_document(doc, s) is True
        assert bob.storage.get_channel(good["hash"]) is not None
        assert bob.storage.get_channel(good["hash"])["server_hash"] == s


class TestAdversarialServerBinding:
    def test_server_not_bound_to_claimed_creator_is_not_materialised(self, peer_factory):
        """Unsigned name/creator metadata must hash back to the scope itself."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        fake_scope = "cd" * 16
        bob.invite_mgr._materialise_server({
            F_SCOPE_KIND:      "server",
            F_CHANNEL_NAME:    "Totally Alice's Server",
            F_CHANNEL_CREATOR: alice.identity.hash_hex,
        }, fake_scope)
        assert bob.storage.get_server(fake_scope) is None, \
            "a server impersonating another identity was materialised"

    def test_correctly_bound_server_is_materialised(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        real = server_hash_for(alice.identity.hash, "Alice Server")
        bob.invite_mgr._materialise_server({
            F_SCOPE_KIND:      "server",
            F_CHANNEL_NAME:    "Alice Server",
            F_CHANNEL_CREATOR: alice.identity.hash_hex,
        }, real)
        assert bob.storage.get_server(real) is not None

    def test_standalone_channel_creator_must_bind_to_the_hash(self, peer_factory):
        """The same binding servers get, for a standalone channel's metadata.

        creator_hash arrives unsigned and then serves as a trusted-signer
        fallback for later documents, so an unbindable claim must not become a
        local channel row.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        assert bob.invite_mgr._creator_binds(
            channel_hash_for(alice.identity.hash, "real-ch"),
            alice.identity.hash_hex, "real-ch",
        ), "a correctly minted channel hash failed to bind"

        assert not bob.invite_mgr._creator_binds(
            "cd" * 16, alice.identity.hash_hex, "real-ch",
        ), "a channel hash unrelated to its claimed creator was accepted"

    def test_server_doc_for_another_server_is_rejected(self, peer_factory):
        alice, bob, s = _server_with_member(peer_factory)
        other = server_hash_for(alice.identity.hash, "Other")
        existing = bob.storage.get_member_list_version(s)
        doc = _server_doc(
            alice.identity.rns_identity, other, existing["version"] + 1,
            members=[alice.identity.hash, bob.identity.hash],
            admins=[alice.identity.hash],
            owners=[alice.identity.hash],
            roster_rows=[],
        )
        assert bob.invite_mgr._accept_document(doc, s) is False, \
            "a document for server A was accepted as an update for server B"


class TestGoodbyeSpoofing:
    """A graceful-shutdown notice must only ever sign off its own sender."""

    def _presence_wired(self, peer):
        presence = PresenceManager(peer.identity.hash_hex)
        peer.router.add_delivery_callback(presence.record_inbound)
        return presence

    def test_forged_goodbye_signs_nobody_off(self, peer_factory):
        """Carol claims to be Alice by setting source_hash to Alice's delivery
        hash. LXMF flags that as SIGNATURE_INVALID, so the router must drop it
        before presence ever sees it."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        presence = self._presence_wired(bob)
        presence.record_seen(alice.identity.hash_hex)

        dest = RNS.Destination(
            bob.identity.rns_identity, RNS.Destination.OUT,
            RNS.Destination.SINGLE, "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(dest, carol.router.delivery_destination, "",
                             desired_method=LXMF.LXMessage.DIRECT)
        lxm.fields = {F_MSG_TYPE: MT_GOODBYE}
        lxm.source_hash = alice.router.delivery_destination.hash

        bob.router._on_message_received(forge(lxm))
        time.sleep(0.3)

        assert presence.is_online(alice.identity.hash_hex), \
            "an unauthenticated goodbye marked another peer offline"

    def test_goodbye_cannot_carry_a_target(self, peer_factory):
        """Extra fields must not let a sender sign anyone else off. The notice
        applies to its authenticated sender and nobody else."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        victim_hex = "dd" * 16
        presence = self._presence_wired(bob)
        presence.record_seen(alice.identity.hash_hex)
        presence.record_seen(victim_hex)

        dest = RNS.Destination(
            bob.identity.rns_identity, RNS.Destination.OUT,
            RNS.Destination.SINGLE, "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(dest, alice.router.delivery_destination, "",
                             desired_method=LXMF.LXMessage.DIRECT)
        # A hostile client bolting a victim onto the notice.
        lxm.fields = {F_MSG_TYPE: MT_GOODBYE, F_MISSED_FOR: victim_hex}

        alice.router.send(lxm)
        assert wait_for(lambda: not presence.is_online(alice.identity.hash_hex),
                        timeout=3), "the sender's own goodbye was not honoured"
        assert presence.is_online(victim_hex), \
            "a goodbye signed off a peer named in its fields, not its sender"

    def test_goodbye_does_not_block_the_sender_returning(self, peer_factory):
        """A signed-off peer must come straight back on their next message --
        otherwise a cancelled shutdown would strand them offline."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        presence = self._presence_wired(bob)
        presence.record_seen(alice.identity.hash_hex)

        dest = RNS.Destination(
            bob.identity.rns_identity, RNS.Destination.OUT,
            RNS.Destination.SINGLE, "lxmf", "delivery",
        )
        goodbye = LXMF.LXMessage(dest, alice.router.delivery_destination, "",
                                 desired_method=LXMF.LXMessage.DIRECT)
        goodbye.fields = {F_MSG_TYPE: MT_GOODBYE}
        alice.router.send(goodbye)
        assert wait_for(lambda: not presence.is_online(alice.identity.hash_hex),
                        timeout=3)

        back = LXMF.LXMessage(dest, alice.router.delivery_destination, "hi",
                              desired_method=LXMF.LXMessage.DIRECT)
        alice.router.send(back)

        assert wait_for(lambda: presence.is_online(alice.identity.hash_hex),
                        timeout=3), "a goodbye left the sender stuck offline"


# ---------------------------------------------------------------------------
# VOICE_CHAT
# ---------------------------------------------------------------------------

from tests.test_voice import _craft_voice_message, _setup_invite_channel
from tests.test_voice_transport import _join_both
from trenchchat.core.permissions import VOICE_CHAT
from trenchchat.core.voice import MAX_VOICE_PARTICIPANTS
from trenchchat.core.protocol import (
    F_VOICE_JOINED_AT, F_VOICE_MUTED, MT_VOICE_JOIN, MT_VOICE_STATE,
)


def _voice_join_fields(ch_hash: str, timestamp: float | None = None) -> dict:
    now = timestamp if timestamp is not None else time.time()
    return {
        F_MSG_TYPE: MT_VOICE_JOIN,
        F_CHANNEL_HASH: bytes.fromhex(ch_hash),
        F_TIMESTAMP: now,
        F_VOICE_MUTED: False,
        F_VOICE_JOINED_AT: now,
    }


class TestAdversarialVoice:
    def test_member_without_voice_chat_join_rejected(self, peer_factory):
        """A member whose role lacks voice_chat can't inject a voice join."""
        alice, bob, ch_hash = _setup_invite_channel(
            peer_factory, member_perms=[SEND_MESSAGE])

        _craft_voice_message(bob, alice, _voice_join_fields(ch_hash))
        time.sleep(0.3)
        assert alice.voice_mgr.get_roster(ch_hash) == [], \
            "Alice accepted a voice join from a member without voice_chat"

    def test_non_member_voice_join_rejected(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        mallory = peer_factory("mallory")

        _craft_voice_message(mallory, alice, _voice_join_fields(ch_hash))
        time.sleep(0.3)
        assert alice.voice_mgr.get_roster(ch_hash) == [], \
            "Alice accepted a voice join from a non-member"

    def test_signalled_roster_flood_cannot_lock_out_a_legit_join(self, peer_factory):
        """The join cap counts real links, not the signalled roster.

        voice_join is unauthenticated, so an attacker can mint any number of
        roster entries from distinct hashes. If the local join cap trusted the
        signalled roster, that flood would fill it and refuse every legitimate
        member. Counting only real occupancy (established links) closes that.
        """
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)

        now = time.time()
        with alice.voice_mgr._lock:
            for i in range(MAX_VOICE_PARTICIPANTS):
                alice.voice_mgr._upsert_entry(
                    ch_hash, f"{i:032x}", muted=False, joined_at=now, now=now)

        assert alice.voice_mgr.join_voice(ch_hash) is True, \
            "a forged-roster flood locked a legitimate member out of voice"

    def test_forged_voice_join_dropped(self, peer_factory):
        """A join whose LXMF signature fails validation dies at the router."""
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)

        delivery_hash = RNS.Destination.hash(
            bytes.fromhex(alice.identity.hash_hex), "lxmf", "delivery")
        dest = RNS.Destination(
            RNS.Identity.recall(delivery_hash),
            RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(dest, bob.router.delivery_destination, "",
                             desired_method=LXMF.LXMessage.DIRECT)
        lxm.fields = _voice_join_fields(ch_hash)
        forge(lxm)
        bob.router.send(lxm)

        time.sleep(0.3)
        assert alice.voice_mgr.get_roster(ch_hash) == [], \
            "Alice accepted a forged voice join"

    def test_voice_state_only_touches_the_sender_entry(self, peer_factory):
        """Voice signalling asserts state about the sender only; no message
        can create or modify a roster entry for anyone else."""
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)

        fields = _voice_join_fields(ch_hash)
        fields[F_MSG_TYPE] = MT_VOICE_STATE
        _craft_voice_message(bob, alice, fields)

        assert wait_for(
            lambda: len(alice.voice_mgr.get_roster(ch_hash)) == 1,
            msg="bob's own roster entry",
        )
        entries = {e["identity_hash"] for e in alice.voice_mgr.get_roster(ch_hash)}
        assert entries == {bob.identity.hash_hex}

    def test_unauthorized_link_hello_refused(self, peer_factory):
        """A non-member driving the transport directly (bypassing join_voice
        entirely) is refused at the link-authorization boundary."""
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        mallory = peer_factory("mallory")

        assert alice.voice_mgr.join_voice(ch_hash) is True
        mallory.voice_transport.start(ch_hash)
        mallory.voice_transport.connect(alice.identity.hash_hex)
        time.sleep(1.2)  # past the initiator-wait fallback
        mallory.voice_transport.connect(alice.identity.hash_hex)
        time.sleep(0.3)

        assert mallory.identity.hash_hex not in \
            alice.voice_transport.connected_peers()
        assert mallory.voice_transport.peer_state(
            alice.identity.hash_hex) != "streaming"

        mallory.voice_transport.send_frames(0, [b"\x01" * 40])
        time.sleep(0.3)
        assert alice.voice_mgr.frame_stats()["rx_frames"].get(
            mallory.identity.hash_hex, 0) == 0

    def test_revoked_member_link_torn_down_on_tick(self, peer_factory):
        """Kicking voice_chat out from under a streaming participant cuts
        their stream on the next re-authorization sweep."""
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        _join_both(alice, bob, ch_hash)

        revoked = dict(PRESET_PRIVATE)
        revoked[ROLE_MEMBER] = [SEND_MESSAGE]
        alice.storage.set_channel_permissions(ch_hash, revoked)

        assert wait_for(
            lambda: bob.identity.hash_hex not in
            alice.voice_transport.connected_peers(),
            msg="revoked member disconnected",
        ), "Alice kept streaming to a member whose voice_chat was revoked"


class TestAdversarialRelayTampering:
    """A relay serves history it did not write, so it must not be able to edit it.

    Every other sync gate answers "may this message be here" -- membership,
    tenure, timestamp. None of them answers "did the named author write this",
    which is what these cover.
    """

    def _seed(self, peer_factory):
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        carol = peer_factory("carol")
        carol.storage.upsert_channel(ch_hash, "test-ch", "", alice.identity.hash_hex,
                                     PRESET_PRIVATE, time.time())
        carol.storage.subscribe(ch_hash)
        return alice, bob, carol, ch_hash

    def _row(self, author_hex, ch_hash, ts, content, sig=None):
        msg_id = _compute_message_id(content, author_hex, ts)
        return {
            "sender_hash":  author_hex,
            "sender_name":  "Alice",
            "content":      content,
            "timestamp":    ts,
            "message_id":   msg_id,
            "reply_to":     None,
            "last_seen_id": None,
            "author_sig":   sig if sig is not None else sign_as(
                author_hex, ch_hash, msg_id, ts, content),
        }

    def _serve(self, to_peer, from_peer, ch_hash, rows):
        to_peer.sync_mgr._record_pending_request(ch_hash, from_peer.identity.hash_hex)
        to_peer.sync_mgr._handle_sync_response(
            {F_MSG_TYPE:      MT_SYNC_RESPONSE,
             F_CHANNEL_HASH:  bytes.fromhex(ch_hash),
             F_SYNC_MESSAGES: msgpack.packb(rows, use_bin_type=True)},
            ch_hash, from_peer.identity.hash_hex,
        )

    def test_relay_cannot_edit_the_text_of_a_message_it_relays(self, peer_factory):
        alice, bob, carol, ch_hash = self._seed(peer_factory)
        ts = time.time() - 60
        genuine = self._row(alice.identity.hash_hex, ch_hash, ts,
                            "transfer approved")

        # Carol relays Alice's message with the text changed but everything
        # else -- author, id, timestamp -- left intact.
        tampered = dict(genuine)
        tampered["content"] = "transfer denied"
        self._serve(bob, carol, ch_hash, [tampered])

        assert not bob.storage.message_exists(genuine["message_id"]), \
            "a relay edited the text of a message it was only relaying"

    def test_tampered_copy_cannot_squat_a_genuine_message_id(self, peer_factory):
        """The damaging half: message_id is UNIQUE and first writer wins.

        If a tampered copy could land first, the genuine message would be
        silently discarded as a duplicate and could never replace it.
        """
        alice, bob, carol, ch_hash = self._seed(peer_factory)
        ts = time.time() - 60
        genuine = self._row(alice.identity.hash_hex, ch_hash, ts,
                            "the real text")

        # Carol keeps the genuine id but swaps the body: the id no longer
        # hashes its own content, so the receiver refuses it before it can
        # squat the genuine copy's slot.
        tampered = dict(genuine)
        tampered["content"] = "the substituted text"
        self._serve(bob, carol, ch_hash, [tampered])
        assert not bob.storage.message_exists(genuine["message_id"])

        # The genuine message still lands afterwards, with its real text.
        self._serve(bob, alice, ch_hash, [genuine])
        rows = [m for m in bob.storage.get_messages(ch_hash)
                if m["message_id"] == genuine["message_id"]]
        assert rows, "the genuine message was blocked by the tampered copy"
        assert rows[0]["content"] == "the real text"

    def test_relay_cannot_rethread_a_message(self, peer_factory):
        """Re-pointing reply_to grafts real words onto another conversation."""
        alice, bob, carol, ch_hash = self._seed(peer_factory)
        ts = time.time() - 60
        row = self._row(alice.identity.hash_hex, ch_hash, ts, "agreed")
        row["reply_to"] = "some-other-message"

        self._serve(bob, carol, ch_hash, [row])
        assert not bob.storage.message_exists(row["message_id"]), \
            "a relay re-pointed a message at a different conversation"

    def test_relay_cannot_invent_a_message_from_another_member(self, peer_factory):
        alice, bob, carol, ch_hash = self._seed(peer_factory)
        ts = time.time() - 60
        forged = self._row(alice.identity.hash_hex, ch_hash, ts,
                           "I never said this", sig=b"\x00" * 64)

        self._serve(bob, carol, ch_hash, [forged])
        assert not bob.storage.message_exists(forged["message_id"]), \
            "a relay invented a message attributed to another member"

    def test_a_genuine_relayed_message_is_still_accepted(self, peer_factory):
        """Positive control: a handler that rejected everything would pass above."""
        alice, bob, carol, ch_hash = self._seed(peer_factory)
        ts = time.time() - 60
        genuine = self._row(alice.identity.hash_hex, ch_hash, ts,
                            "relayed intact")

        self._serve(bob, carol, ch_hash, [genuine])
        assert bob.storage.message_exists(genuine["message_id"]), \
            "a genuine message relayed by a third peer was rejected"

    def test_relay_cannot_forge_an_id_squatting_another_message(self, peer_factory):
        """A synced row whose id belongs to a different message is refused.

        The id *is* the hash of its own content, recomputed on receipt exactly
        as the direct path does. A relay that hands over a row carrying some
        other message's id (to suppress the genuine copy as a duplicate) can no
        longer make that id land -- the recomputed hash won't match.
        """
        alice, bob, carol, ch_hash = self._seed(peer_factory)
        ts = time.time() - 60
        victim_id = _compute_message_id("the real thing", alice.identity.hash_hex, ts)

        forged = self._row(alice.identity.hash_hex, ch_hash, ts, "not the real thing")
        forged["message_id"] = victim_id
        forged["author_sig"] = sign_as(alice.identity.hash_hex, ch_hash, victim_id,
                                       ts, "not the real thing")

        self._serve(bob, carol, ch_hash, [forged])
        assert not bob.storage.message_exists(victim_id), \
            "a synced row squatted another message's id"

    def test_a_rejected_batch_does_not_report_the_channel_as_caught_up(
            self, peer_factory):
        """Silent rejection must not read as "up to date".

        A relay that serves nothing but tampered rows would otherwise leave the
        channel claiming SYNCED while the real history is still missing.
        """
        from trenchchat.core.sync_status import SyncState

        alice, bob, carol, ch_hash = self._seed(peer_factory)
        ts = time.time() - 60
        genuine = self._row(alice.identity.hash_hex, ch_hash, ts,
                            "the real text")
        tampered = dict(genuine)
        tampered["content"] = "rewritten"

        self._serve(bob, carol, ch_hash, [tampered])

        assert not bob.storage.message_exists(genuine["message_id"])
        status = bob.sync_mgr.status.get_status(ch_hash)
        assert status["state"] != SyncState.SYNCED.value, \
            "a channel whose only answer was rejected reported itself synced"
        assert status["peers"][0]["messages_rejected"] == 1


# ---------------------------------------------------------------------------
# Channel announces are discovery hints, not a channel of authority
# ---------------------------------------------------------------------------

class TestAdversarialChannelAnnounce:
    """app_data on a channel announce is unsigned and unversioned.

    RNS binds the destination hash to the announcing identity, so only a
    channel's creator can announce it -- but "creator" is not "still in
    charge", and the payload itself is free text either way.
    """

    def _announce(self, peer, channel_hash_hex, announcer, **metadata):
        peer.channel_mgr._on_channel_discovered(
            bytes.fromhex(channel_hash_hex),
            announcer.identity.rns_identity,
            metadata,
        )

    def _perms(self, peer, channel_hash_hex):
        return permissions_from_json(
            peer.storage.get_channel(channel_hash_hex)["permissions"])

    def test_an_announce_cannot_open_a_private_channel(self, peer_factory):
        """
        open_join is what makes the inbound message handler stop checking
        membership and SEND_MESSAGE, so flipping it opens a private
        transcript to anyone on the mesh.
        """
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("private", "", permissions=PRESET_PRIVATE)
        assert not is_open_join(self._perms(alice, ch_hash))

        self._announce(alice, ch_hash, alice, name="private", access="public")

        assert not is_open_join(self._perms(alice, ch_hash)), \
            "an unsigned announce flipped a private channel to open_join"

    def test_an_announce_cannot_revert_a_signed_permission_change(self, peer_factory):
        """MANAGE_CHANNEL owns this column; discovery metadata does not."""
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("open", "", permissions=PRESET_OPEN)

        tightened = dict(PRESET_OPEN)
        tightened[ROLE_MEMBER] = [SEND_MESSAGE]
        alice.storage.set_channel_permissions(ch_hash, tightened)

        self._announce(alice, ch_hash, alice, name="open", access="public")

        assert self._perms(alice, ch_hash)[ROLE_MEMBER] == [SEND_MESSAGE], \
            "a routine announce reverted a signed permission change"

    def test_creator_hash_comes_from_the_announcer_not_the_payload(self, peer_factory):
        """
        creator_hash is a trusted-signer fallback when validating member list
        documents, so a payload-supplied one launders signing authority.
        """
        alice = peer_factory("alice")
        victim = peer_factory("victim")
        attacker = peer_factory("attacker")
        forged_hash = "cc" * 16

        self._announce(alice, forged_hash, attacker,
                       name="theirs", access="public",
                       creator=victim.identity.hash_hex)

        stored = alice.storage.get_channel(forged_hash)
        assert stored["creator_hash"] == attacker.identity.hash_hex, \
            "an announce named someone else as the channel creator"

    def test_a_first_sighting_still_records_the_announced_metadata(self, peer_factory):
        """The narrowing must not break discovery itself."""
        alice = peer_factory("alice")
        attacker = peer_factory("attacker")
        new_hash = "dd" * 16

        self._announce(alice, new_hash, attacker, name="fresh",
                       description="hello", access="public")

        stored = alice.storage.get_channel(new_hash)
        assert stored["name"] == "fresh"
        assert stored["description"] == "hello"
        assert is_open_join(self._perms(alice, new_hash))

    def test_a_later_announce_still_refreshes_name_and_description(self, peer_factory):
        alice = peer_factory("alice")
        attacker = peer_factory("attacker")
        new_hash = "ee" * 16

        self._announce(alice, new_hash, attacker, name="before", access="public")
        self._announce(alice, new_hash, attacker, name="after",
                       description="renamed", access="public")

        stored = alice.storage.get_channel(new_hash)
        assert stored["name"] == "after"
        assert stored["description"] == "renamed"


# ---------------------------------------------------------------------------
# A kick has to reach every admin, not only the one that performed it
# ---------------------------------------------------------------------------

class TestAdversarialKickedMemberRejoin:
    """spent_invite_tokens and the revocation sentinel are written only by the
    peer that saw the redemption or performed the kick.

    A second admin holds neither, so a kicked member could replay their
    original join request to it and be published straight back in -- with no
    human involved, since the join-request handler is fully automatic. The
    departure itself rides in the signed member list document, so every peer
    that accepted the removal can make the call.
    """

    def _channel_with_two_admins_and_a_member(self, peer_factory):
        alice = peer_factory("alice")      # owner
        bob = peer_factory("bob")          # second admin
        carol = peer_factory("carol")      # invitee, later kicked

        ch_hash = alice.channel_mgr.create_channel(
            "private", "", permissions=PRESET_PRIVATE)

        # Bob joins through the real invite flow, so he holds the channel and
        # its document and will receive later updates.
        bob.invite_mgr.add_invite_callback(
            lambda ch, name, token, expiry, admin_hex:
                bob.invite_mgr.send_join_request(ch, token, expiry, admin_hex))
        alice.invite_mgr.send_invite(ch_hash, bob.identity.hash_hex)
        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex, timeout=5)
        assert wait_for_member(bob.storage, ch_hash, bob.identity.hash_hex, timeout=5)

        alice.invite_mgr.publish_member_list(ch_hash, add_admins=[bob.identity.hash])
        assert wait_for(
            lambda: bob.storage.get_role(ch_hash, bob.identity.hash_hex) == ROLE_ADMIN,
            timeout=5), "Bob was never promoted to admin at his own peer"

        captured = {}

        def on_invite(channel_hash_hex, channel_name, token, expiry, admin_hex):
            # Read before send_join_request, which clears the pending invite.
            captured.update(
                token=token, expiry=expiry, admin_hex=admin_hex,
                issued_at=carol.storage.get_pending_invite_issued_at(channel_hash_hex),
            )
            carol.invite_mgr.send_join_request(
                channel_hash_hex, token, expiry, admin_hex)

        carol.invite_mgr.add_invite_callback(on_invite)
        alice.invite_mgr.send_invite(ch_hash, carol.identity.hash_hex)

        assert wait_for_member(alice.storage, ch_hash, carol.identity.hash_hex, timeout=5)
        assert wait_for(
            lambda: bob.storage.is_member(ch_hash, carol.identity.hash_hex), timeout=5), \
            "Bob never learned Carol had joined"
        return alice, bob, carol, ch_hash, captured

    def _replay_to(self, admin_peer, ch_hash, carol, captured, issued_at):
        fields = {
            F_MSG_TYPE:      MT_JOIN_REQUEST,
            F_CHANNEL_HASH:  bytes.fromhex(ch_hash),
            F_INVITE_TOKEN:  captured["token"],
            F_INVITEE_HASH:  carol.identity.hash,
            F_EXPIRY_TS:     captured["expiry"],
            F_ADMIN_HASH:    bytes.fromhex(captured["admin_hex"]),
        }
        if issued_at is not None:
            fields[F_INVITE_ISSUED_TS] = issued_at
        admin_peer.invite_mgr._handle_join_request(
            fields, ch_hash, sender_hex=carol.identity.hash_hex)

    def test_a_kicked_member_cannot_rejoin_through_a_second_admin(self, peer_factory):
        alice, bob, carol, ch_hash, captured = \
            self._channel_with_two_admins_and_a_member(peer_factory)
        issued_at = captured["issued_at"]
        assert issued_at, "the invite carried no bound issue time"

        alice.invite_mgr.publish_member_list(
            ch_hash, remove_members=[carol.identity.hash])
        assert wait_for(
            lambda: not bob.storage.is_member(ch_hash, carol.identity.hash_hex),
            timeout=5), "Bob never accepted the removal"

        self._replay_to(bob, ch_hash, carol, captured, issued_at)

        assert not bob.storage.is_member(ch_hash, carol.identity.hash_hex), \
            "a kicked member replayed their original invite through a second admin"

    def test_a_token_with_no_bound_issue_time_is_refused_after_a_kick(self, peer_factory):
        """A token from a peer predating the field cannot be dated, so it loses.

        Minted through generate_invite_token with no issue time, which is
        byte-for-byte what an older peer signs.
        """
        alice, bob, carol, ch_hash, _ = \
            self._channel_with_two_admins_and_a_member(peer_factory)

        legacy_token, legacy_expiry = alice.invite_mgr.generate_invite_token(
            ch_hash, carol.identity.hash)
        legacy = {"token": legacy_token, "expiry": legacy_expiry,
                  "admin_hex": alice.identity.hash_hex}

        alice.invite_mgr.publish_member_list(
            ch_hash, remove_members=[carol.identity.hash])
        assert wait_for(
            lambda: not bob.storage.is_member(ch_hash, carol.identity.hash_hex),
            timeout=5), "Bob never accepted the removal"

        self._replay_to(bob, ch_hash, carol, legacy, None)

        assert not bob.storage.is_member(ch_hash, carol.identity.hash_hex), \
            "an undatable token was honoured after a kick"

    def test_a_fresh_invite_after_the_kick_still_works(self, peer_factory):
        """The point is to invalidate stale tokens, not to blacklist people."""
        alice, bob, carol, ch_hash, _ = \
            self._channel_with_two_admins_and_a_member(peer_factory)

        alice.invite_mgr.publish_member_list(
            ch_hash, remove_members=[carol.identity.hash])
        assert wait_for(
            lambda: not alice.storage.is_member(ch_hash, carol.identity.hash_hex),
            timeout=5)

        carol.invite_mgr.add_invite_callback(
            lambda ch, name, token, expiry, admin_hex:
                carol.invite_mgr.send_join_request(ch, token, expiry, admin_hex))
        alice.invite_mgr.send_invite(ch_hash, carol.identity.hash_hex)

        assert wait_for_member(alice.storage, ch_hash, carol.identity.hash_hex, timeout=5), \
            "a fresh invite issued after the kick was refused"


# ---------------------------------------------------------------------------
# message_id is a hash, not a label a sender may choose
# ---------------------------------------------------------------------------

class TestAdversarialMessageIdSquatting:
    """message_id is globally UNIQUE, so the first writer of one keeps it.

    The author signature binds a message to its author, but an attacker signs
    their *own* message -- so any member could mint a validly signed message
    under an id they had seen elsewhere. The genuine copy then lost the insert
    silently, and no future sweep would offer it again.
    """

    def _chat_lxm(self, sender, recipient, ch_hash, content, msg_id, ts):
        dest = RNS.Destination(
            recipient.identity.rns_identity, RNS.Destination.OUT,
            RNS.Destination.SINGLE, "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(dest, sender.router.delivery_destination, content,
                             desired_method=LXMF.LXMessage.DIRECT)
        lxm.fields = pack_fields({
            F_CHANNEL_HASH: bytes.fromhex(ch_hash),
            F_DISPLAY_NAME: "Peer",
            F_TIMESTAMP:    ts,
            F_MESSAGE_ID:   msg_id,
            F_AUTHOR_SIG:   sign_as(sender.identity.hash_hex, ch_hash, msg_id,
                                    ts, content),
        })
        lxm.signature_validated = True
        return lxm

    def test_a_member_cannot_claim_another_messages_id(self, peer_factory):
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        victim_ts = time.time()
        victim_id = _compute_message_id("the real thing", alice.identity.hash_hex,
                                        victim_ts)

        squat_ts = time.time()
        squat = self._chat_lxm(alice, bob, ch_hash, "not the real thing",
                               victim_id, squat_ts)
        bob.router._on_message_received(squat)
        time.sleep(0.3)

        ids = [m["message_id"] for m in bob.storage.get_messages(ch_hash)]
        assert victim_id not in ids, "a squatted message_id was accepted"

        genuine = self._chat_lxm(alice, bob, ch_hash, "the real thing",
                                 victim_id, victim_ts)
        bob.router._on_message_received(genuine)
        time.sleep(0.3)

        rows = [m for m in bob.storage.get_messages(ch_hash)
                if m["message_id"] == victim_id]
        assert rows and rows[0]["content"] == "the real thing", \
            "the genuine message could not claim its own id afterwards"


# ---------------------------------------------------------------------------
# Reactions riding along with a synced message are attacker-chosen too
# ---------------------------------------------------------------------------

class TestAdversarialSyncedReactions:
    """The message body is bound to its author by a signature; the reactions
    beside it are not, and every field is the responder's to choose."""

    def _seed(self, peer_factory):
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[SEND_MESSAGE]
        )
        carol = peer_factory("carol")
        carol.storage.upsert_channel(ch_hash, "test-ch", "", alice.identity.hash_hex,
                                     PRESET_PRIVATE, time.time())
        carol.storage.subscribe(ch_hash)
        return alice, bob, carol, ch_hash

    def _serve(self, to_peer, from_peer, ch_hash, rows):
        to_peer.sync_mgr._record_pending_request(ch_hash, from_peer.identity.hash_hex)
        to_peer.sync_mgr._handle_sync_response(
            {F_MSG_TYPE:      MT_SYNC_RESPONSE,
             F_CHANNEL_HASH:  bytes.fromhex(ch_hash),
             F_SYNC_MESSAGES: msgpack.packb(rows, use_bin_type=True)},
            ch_hash, from_peer.identity.hash_hex,
        )

    def _row_with_reaction(self, alice, ch_hash, reactor_hex, emoji="\U0001F44D"):
        ts = time.time() - 60
        msg_id = _compute_message_id("hello", alice.identity.hash_hex, ts)
        return {
            "sender_hash":  alice.identity.hash_hex,
            "sender_name":  "Alice",
            "content":      "hello",
            "timestamp":    ts,
            "message_id":   msg_id,
            "reply_to":     None,
            "last_seen_id": None,
            "author_sig":   sign_as(alice.identity.hash_hex, ch_hash, msg_id,
                                    ts, "hello"),
            "reactions":    [{"emoji": emoji, "reactor": reactor_hex,
                              "at": ts + 1}],
        }, msg_id

    def test_a_reaction_from_a_non_member_is_dropped(self, peer_factory):
        alice, bob, carol, ch_hash = self._seed(peer_factory)
        stranger = "ab" * 16
        row, msg_id = self._row_with_reaction(alice, ch_hash, stranger)

        self._serve(bob, carol, ch_hash, [row])

        reactors = [r["reactor_hash"] for r in bob.storage.get_reactions(msg_id)]
        assert stranger not in reactors, \
            "a synced reaction was stored for an identity that could not have sent it"

    def test_a_reaction_cannot_be_attributed_to_us(self, peer_factory):
        """Rendered as the local user's own reaction by both clients."""
        alice, bob, carol, ch_hash = self._seed(peer_factory)
        bob.storage.upsert_member(ch_hash, bob.identity.hash_hex, "Bob",
                                  role=ROLE_MEMBER)
        row, msg_id = self._row_with_reaction(alice, ch_hash, "cd" * 16)

        self._serve(bob, carol, ch_hash, [row])

        reactors = [r["reactor_hash"] for r in bob.storage.get_reactions(msg_id)]
        assert "cd" * 16 not in reactors

    def test_a_reaction_from_a_real_member_is_kept(self, peer_factory):
        """The guard must not drop legitimate backfilled reactions."""
        alice, bob, carol, ch_hash = self._seed(peer_factory)
        row, msg_id = self._row_with_reaction(alice, ch_hash,
                                              alice.identity.hash_hex)

        self._serve(bob, carol, ch_hash, [row])

        reactors = [r["reactor_hash"] for r in bob.storage.get_reactions(msg_id)]
        assert alice.identity.hash_hex in reactors, \
            "a legitimate synced reaction was dropped"


class TestAdversarialQuarantinePathRequests:
    """Quarantine path requests fire before authentication.

    The per-source throttle is keyed on source_hash -- wire data the sender
    chooses and nothing has verified at that point -- so varying it made every
    bucket fresh and restored the one-mesh-broadcast-per-packet amplification
    the throttle exists to prevent.
    """

    def _unknown_source_lxm(self, bob, source_hash: bytes):
        dest = RNS.Destination(
            bob.identity.rns_identity, RNS.Destination.OUT,
            RNS.Destination.SINGLE, "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(dest, dest, "x", desired_method=LXMF.LXMessage.DIRECT)
        lxm.source_hash = source_hash
        lxm.signature_validated = False
        lxm.unverified_reason = LXMF.LXMessage.SOURCE_UNKNOWN
        lxm.fields = {F_CHANNEL_HASH: b"\x00" * 16}
        # Only a message with packed bytes can be re-validated later, so only
        # one is quarantined -- and only a quarantined one requests a path.
        lxm.packed = b"\x00" * 32
        return lxm

    def test_rotating_the_claimed_source_cannot_evade_the_throttle(self, peer_factory):
        bob = peer_factory("bob")
        requested = []

        with patch("trenchchat.network.router.RNS.Transport.request_path",
                   side_effect=lambda h: requested.append(h)):
            for i in range(PATH_REQUEST_GLOBAL_BURST * 3):
                bob.router._on_message_received(
                    self._unknown_source_lxm(bob, i.to_bytes(16, "big")))

        assert len(requested) <= PATH_REQUEST_GLOBAL_BURST, (
            f"{len(requested)} mesh broadcasts from "
            f"{PATH_REQUEST_GLOBAL_BURST * 3} packets with rotating sources"
        )

    def test_the_source_table_stays_bounded_under_rotation(self, peer_factory):
        bob = peer_factory("bob")
        with patch("trenchchat.network.router.RNS.Transport.request_path"):
            for i in range(PATH_REQUEST_MAX_SOURCES * 2):
                bob.router._on_message_received(
                    self._unknown_source_lxm(bob, i.to_bytes(16, "big")))

        assert len(bob.router._path_request_rate) <= PATH_REQUEST_MAX_SOURCES


class TestDirectMessageGate:
    """
    Direct messages, driven straight at the core the way a bad client would.

    Every check here is on the receiving side, because that is the only side
    an attacker does not control. The frontend never appears.
    """

    @staticmethod
    def _dm_lxm(sender, recipient, conversation_hash, content, ts=None):
        """A direct message in the form a real TrenchChat client sends.

        No conversation address on the wire -- the receiver derives it from the
        sender it authenticated -- so everything TrenchChat adds rides in the
        LXMF custom-payload envelope. conversation_hash is only what the author
        signature is computed over.
        """
        dest = RNS.Destination(
            recipient.identity.rns_identity, RNS.Destination.OUT,
            RNS.Destination.SINGLE, "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(dest, sender.router.delivery_destination, content,
                             desired_method=LXMF.LXMessage.DIRECT)
        ts = time.time() if ts is None else ts
        msg_id = _compute_message_id(content, sender.identity.hash_hex, ts)
        lxm.fields = {
            LXMF_FIELD_CUSTOM_TYPE: DM_ENVELOPE_TYPE,
            LXMF_FIELD_CUSTOM_DATA: pack_dm_envelope(
                message_id=msg_id, timestamp=ts, display_name="Mallory",
                reply_to=None, last_seen_id=None,
                author_sig=sign_as(sender.identity.hash_hex, conversation_hash,
                                   msg_id, ts, content),
            ),
        }
        return lxm, msg_id

    @staticmethod
    def _control_lxm(sender, recipient, msg_type):
        dest = RNS.Destination(
            recipient.identity.rns_identity, RNS.Destination.OUT,
            RNS.Destination.SINGLE, "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(dest, sender.router.delivery_destination, "",
                             desired_method=LXMF.LXMessage.DIRECT)
        lxm.fields = {F_MSG_TYPE: msg_type, F_DISPLAY_NAME: "Mallory"}
        return lxm

    def test_direct_message_from_a_stranger_is_dropped(self, peer_factory):
        """No friendship at all: the message must not be stored."""
        mallory = peer_factory("mallory")
        bob = peer_factory("bob")

        conversation = dm_hash_for(mallory.identity.hash_hex, bob.identity.hash_hex)
        lxm, msg_id = self._dm_lxm(mallory, bob, conversation, "let me in")
        mallory.router.send(lxm)

        time.sleep(0.4)
        assert not bob.storage.message_exists(msg_id)
        assert bob.direct_mgr.conversations() == []

    def test_one_sided_friendship_does_not_open_the_gate(self, peer_factory):
        """
        Mallory has added Bob; Bob has not added Mallory. Adding someone is a
        statement about who *we* accept, so it must not let them reach us.
        """
        mallory = peer_factory("mallory")
        bob = peer_factory("bob")
        mallory.friends_mgr.add_friend(bob.identity.hash_hex)

        conversation = dm_hash_for(mallory.identity.hash_hex, bob.identity.hash_hex)
        lxm, msg_id = self._dm_lxm(mallory, bob, conversation, "we are friends, right")
        mallory.router.send(lxm)

        time.sleep(0.4)
        assert not bob.storage.message_exists(msg_id)

    def test_a_pending_request_does_not_open_the_gate(self, peer_factory):
        """Asking to be added is not being added."""
        mallory = peer_factory("mallory")
        bob = peer_factory("bob")

        mallory.friends_mgr.send_friend_request(bob.identity.hash_hex, "hi")
        assert wait_for(lambda: bob.storage.get_friend_state(
            mallory.identity.hash_hex) == FRIEND_PENDING_IN)

        conversation = dm_hash_for(mallory.identity.hash_hex, bob.identity.hash_hex)
        lxm, msg_id = self._dm_lxm(mallory, bob, conversation, "jumping the queue")
        mallory.router.send(lxm)

        time.sleep(0.4)
        assert not bob.storage.message_exists(msg_id)

    def test_a_conversation_cannot_be_addressed_by_the_sender_at_all(
            self, peer_factory):
        """The address is derived, never accepted.

        A direct message carries no conversation hash, so there is nothing to
        aim somewhere it does not belong: whatever Mallory sends lands in the
        conversation between Mallory and the recipient, and nowhere else. The
        old shape of this attack -- naming Bob and Carol's conversation -- has
        no way to be expressed on the wire any more, and a message that does
        carry a channel hash is read as a channel message, where Mallory is
        not a member.
        """
        mallory = peer_factory("mallory")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        mallory.friends_mgr.add_friend(bob.identity.hash_hex)
        bob.friends_mgr.add_friend(mallory.identity.hash_hex)
        bob.friends_mgr.add_friend(carol.identity.hash_hex)

        foreign = dm_hash_for(bob.identity.hash_hex, carol.identity.hash_hex)
        # Signed as though for Bob and Carol's conversation, and sent anyway.
        lxm, _ = self._dm_lxm(mallory, bob, foreign, "signed, Carol")
        mallory.router.send(lxm)

        time.sleep(0.4)
        assert bob.storage.get_messages(foreign) == [], \
            "a message reached a conversation its sender is not half of"

        # Naming it as a channel instead reaches nothing either.
        channel_style, _ = self._dm_lxm(mallory, bob, foreign, "as a channel")
        channel_style.fields[F_CHANNEL_HASH] = bytes.fromhex(foreign)
        mallory.router.send(channel_style)

        time.sleep(0.4)
        assert bob.storage.get_messages(foreign) == []

    def test_a_forged_sender_cannot_deliver_a_direct_message(self, peer_factory):
        """
        Mallory claims Alice's delivery hash so sender resolution yields Alice,
        who Bob really is friends with. LXMF marks the signature invalid and
        the router must drop it before the gate is ever consulted.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        mallory = peer_factory("mallory")
        alice.friends_mgr.add_friend(bob.identity.hash_hex)
        bob.friends_mgr.add_friend(alice.identity.hash_hex)

        conversation = dm_hash_for(alice.identity.hash_hex, bob.identity.hash_hex)
        lxm, msg_id = self._dm_lxm(alice, bob, conversation, "trust me")
        lxm.source_hash = alice.router.delivery_destination.hash
        mallory.router.send(forge(lxm))

        time.sleep(0.4)
        assert not bob.storage.message_exists(msg_id)

    def test_an_unsolicited_accept_creates_no_friendship(self, peer_factory):
        """
        An accept we never asked for must change nothing -- otherwise the gate
        is one unsolicited control message away from anybody.
        """
        mallory = peer_factory("mallory")
        bob = peer_factory("bob")

        mallory.router.send(self._control_lxm(mallory, bob, MT_FRIEND_ACCEPT))

        time.sleep(0.4)
        assert bob.friends_mgr.is_friend(mallory.identity.hash_hex) is False
        assert bob.storage.get_friend_state(mallory.identity.hash_hex) is None

    def test_an_accept_cannot_promote_a_request_we_received(self, peer_factory):
        """
        Mallory asked us and then sent their own accept. Only our answer moves
        an incoming request; theirs must leave it exactly where it was.
        """
        mallory = peer_factory("mallory")
        bob = peer_factory("bob")

        mallory.friends_mgr.send_friend_request(bob.identity.hash_hex)
        assert wait_for(lambda: bob.storage.get_friend_state(
            mallory.identity.hash_hex) == FRIEND_PENDING_IN)

        mallory.router.send(self._control_lxm(mallory, bob, MT_FRIEND_ACCEPT))

        time.sleep(0.4)
        assert bob.storage.get_friend_state(
            mallory.identity.hash_hex) == FRIEND_PENDING_IN
        assert bob.friends_mgr.is_friend(mallory.identity.hash_hex) is False

    def test_a_reaction_into_a_conversation_from_an_outsider_is_dropped(
            self, peer_factory):
        """
        Mallory reacts inside the Bob-Carol conversation. Only its two halves
        may, and Mallory is neither.
        """
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        mallory = peer_factory("mallory")
        bob.friends_mgr.add_friend(carol.identity.hash_hex)
        carol.friends_mgr.add_friend(bob.identity.hash_hex)
        bob.friends_mgr.add_friend(mallory.identity.hash_hex)
        mallory.friends_mgr.add_friend(bob.identity.hash_hex)

        sent = carol.messaging.send_direct(bob.identity.hash_hex, "just us")
        assert wait_for(lambda: bob.storage.message_exists(sent))
        conversation = dm_hash_for(bob.identity.hash_hex, carol.identity.hash_hex)

        dest = RNS.Destination(
            bob.identity.rns_identity, RNS.Destination.OUT,
            RNS.Destination.SINGLE, "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(dest, mallory.router.delivery_destination, "",
                             desired_method=LXMF.LXMessage.DIRECT)
        lxm.fields = {
            F_MSG_TYPE:         MT_REACTION,
            F_CHANNEL_HASH:     bytes.fromhex(conversation),
            F_REACTION_MSG_ID:  sent,
            F_REACTION_UNICODE: "👎",
        }
        mallory.router.send(lxm)

        time.sleep(0.4)
        assert bob.storage.get_reactions(sent) == []

    def test_an_unfriended_peer_stops_getting_our_queued_messages(self, peer_factory):
        """
        A message queued for an unreachable friend must not be pushed at them
        once they reappear, if by then they are no longer a friend -- the same
        rule a kick applies to a channel queue.
        """
        alice = peer_factory("alice")
        stranger = "cc" * 16
        alice.friends_mgr.add_friend(stranger)
        alice.messaging.send_direct(stranger, "queued while you were away")

        conversation = dm_hash_for(alice.identity.hash_hex, stranger)
        alice.friends_mgr.remove_friend(stranger)
        assert alice.messaging._may_receive(conversation, stranger) is False


class TestAdversarialTrustAnchorUnion:
    """The creator and the admin whose invite we accepted are both anchors for
    a first document. Unioning them fixed a real rejection, and must not have
    widened what counts as a trusted signer."""

    def test_a_third_party_still_cannot_sign_a_first_document(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        mallory = peer_factory("mallory")
        ch_hash = alice.channel_mgr.create_channel("private", "", "invite")

        # Bob accepted an invite from alice, so both anchors exist for him.
        bob.storage.record_accepted_invite(
            ch_hash, alice.identity.hash_hex, time.time() + 3600)
        bob.storage.upsert_channel(
            hash=ch_hash, name="private", description="",
            creator_hash=alice.identity.hash_hex, permissions="invite",
            created_at=time.time(),
        )

        doc = _build_crafted_doc(
            mallory.identity.rns_identity, ch_hash, 1,
            [alice.identity.hash, bob.identity.hash, mallory.identity.hash],
            [mallory.identity.hash], [mallory.identity.hash],
        )

        assert bob.invite_mgr._accept_document(doc, ch_hash) is False, \
            "a signer who is neither the creator nor the inviting admin was trusted"
        assert bob.storage.is_member(ch_hash, mallory.identity.hash_hex) is False

    def test_the_inviting_admin_is_trusted_even_beside_a_creator(self, peer_factory):
        """The anchors used to be a fallback chain, so a stored creator hid the
        invite entirely and any other admin's document was rejected."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        ch_hash = alice.channel_mgr.create_channel("private", "", "invite")

        bob.storage.upsert_channel(
            hash=ch_hash, name="private", description="",
            creator_hash=alice.identity.hash_hex, permissions="invite",
            created_at=time.time(),
        )
        bob.storage.record_accepted_invite(
            ch_hash, carol.identity.hash_hex, time.time() + 3600)

        doc = _build_crafted_doc(
            carol.identity.rns_identity, ch_hash, 1,
            [alice.identity.hash, carol.identity.hash, bob.identity.hash],
            [alice.identity.hash, carol.identity.hash], [alice.identity.hash],
        )

        assert bob.invite_mgr._accept_document(doc, ch_hash) is True
        assert bob.storage.is_member(ch_hash, bob.identity.hash_hex) is True


class TestAdversarialMessageRequests:
    """Holding a stranger's message is a queue anyone can write to.

    A direct message carries no F_MSG_TYPE, which deliberately keeps it out of
    the router's per-sender control throttle -- so nothing upstream paces this
    path and every bound has to hold here.
    """

    @staticmethod
    def _stranger_says(sender, recipient, text):
        """Drive a message straight at the receiving core, gate and all."""
        recipient.messaging._on_direct_message(
            SimpleNamespace(content=text, timestamp=time.time()),
            {}, sender.identity.hash_hex,
        )

    def test_a_held_message_grants_nothing(self, peer_factory):
        alice = peer_factory("alice")
        mallory = peer_factory("mallory")

        self._stranger_says(mallory, alice, "let me in")

        mallory_hex = mallory.identity.hash_hex
        assert alice.friends_mgr.is_friend(mallory_hex) is False
        assert alice.direct_mgr.may_dm(mallory_hex) is False
        assert alice.direct_mgr.open_conversation(mallory_hex) is None
        assert alice.direct_mgr.conversation_hash(mallory_hex) is not None
        assert not alice.storage.is_dm(
            alice.direct_mgr.conversation_hash(mallory_hex))

    def test_one_sender_cannot_fill_the_queue(self, peer_factory):
        alice = peer_factory("alice")
        mallory = peer_factory("mallory")

        for i in range(MAX_HELD_PER_SENDER * 5):
            self._stranger_says(mallory, alice, f"spam {i}")

        held = alice.storage.get_message_requests(mallory.identity.hash_hex)
        assert len(held) == MAX_HELD_PER_SENDER
        # Oldest-first eviction: the newest are what survive.
        assert held[-1]["body"] == f"spam {MAX_HELD_PER_SENDER * 5 - 1}"

    def test_a_long_body_is_trimmed_rather_than_stored_whole(self, peer_factory):
        alice = peer_factory("alice")
        mallory = peer_factory("mallory")

        self._stranger_says(mallory, alice, "x" * (MAX_REQUEST_BODY_CHARS * 4))

        held = alice.storage.get_message_requests(mallory.identity.hash_hex)
        assert len(held[0]["body"]) == MAX_REQUEST_BODY_CHARS

    def test_rotating_identities_cannot_grow_the_queue(self, peer_factory):
        """Identities are free to mint, so the pending queue is the bound that
        actually holds -- the per-sender one only paces a single peer."""
        alice = peer_factory("alice")
        senders = [SimpleNamespace(identity=SimpleNamespace(
            hash_hex=f"{i:032x}")) for i in range(MAX_PENDING_FRIEND_REQUESTS + 20)]

        for s in senders:
            self._stranger_says(s, alice, "hello")

        assert alice.storage.count_friends_in_state(FRIEND_PENDING_IN) \
            <= MAX_PENDING_FRIEND_REQUESTS
        assert len(alice.storage.get_message_requests()) <= MAX_HELD_MESSAGES

    def test_an_evicted_sender_leaves_no_orphaned_messages(self, peer_factory):
        alice = peer_factory("alice")
        first = SimpleNamespace(identity=SimpleNamespace(hash_hex="ab" * 16))
        self._stranger_says(first, alice, "the oldest")

        for i in range(MAX_PENDING_FRIEND_REQUESTS + 5):
            self._stranger_says(
                SimpleNamespace(identity=SimpleNamespace(hash_hex=f"{i:032x}")),
                alice, "later")

        assert alice.storage.get_friend_state(first.identity.hash_hex) is None
        assert alice.storage.get_message_requests(first.identity.hash_hex) == []

    def test_an_accepted_friend_is_never_diverted_into_the_queue(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        alice.friends_mgr.add_friend(bob.identity.hash_hex)

        self._stranger_says(bob, alice, "a real message")

        assert alice.storage.get_message_requests(bob.identity.hash_hex) == []
        conversation = alice.direct_mgr.conversation_hash(bob.identity.hash_hex)
        assert [m["content"] for m in alice.storage.get_messages(conversation)] \
            == ["a real message"]

    def test_a_peer_we_asked_cannot_move_the_handshake_by_talking(
            self, peer_factory):
        """Their words used to be dropped outright while a request of ours was
        outstanding, which also threw away the only reply a bot can make (see
        test_direct_messages). They are held now -- but holding them must still
        move nothing: not our request's state, not the gate, not a
        conversation. Only the user accepting does that."""
        alice = peer_factory("alice")
        mallory = peer_factory("mallory")
        mallory_hex = mallory.identity.hash_hex
        alice.storage.upsert_friend(mallory_hex, "", "", FRIEND_PENDING_OUT)

        self._stranger_says(mallory, alice, "just accept already")

        assert [h["body"] for h in
                alice.storage.get_message_requests(mallory_hex)] \
            == ["just accept already"]
        # Our request is still ours: holding what they said is not us
        # deciding the handshake went the other way.
        assert alice.storage.get_friend_state(mallory_hex) == FRIEND_PENDING_OUT
        assert alice.friends_mgr.is_friend(mallory_hex) is False
        assert alice.direct_mgr.may_dm(mallory_hex) is False
        assert alice.direct_mgr.open_conversation(mallory_hex) is None

    def test_a_peer_we_asked_is_bounded_like_any_other_sender(
            self, peer_factory):
        """Holding their words opens a queue they can write to, so the
        per-sender bound has to cover them too."""
        alice = peer_factory("alice")
        mallory = peer_factory("mallory")
        mallory_hex = mallory.identity.hash_hex
        alice.storage.upsert_friend(mallory_hex, "", "", FRIEND_PENDING_OUT)

        for i in range(MAX_HELD_PER_SENDER + 5):
            self._stranger_says(mallory, alice, f"message {i}")

        held = alice.storage.get_message_requests(mallory_hex)
        assert len(held) <= MAX_HELD_PER_SENDER
