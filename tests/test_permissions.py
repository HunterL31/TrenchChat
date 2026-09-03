"""
Tests for the role-based permission system.

Covers:
- Permission checking via permissions module helpers
- Storage.has_permission / get_role / get_channel_permissions
- Owner immutability (always has all permissions)
- Preset permission configurations
- Member invite permission enables invite flow
"""

import json
import time

import pytest

from trenchchat.core.storage import Storage
from trenchchat.core.permissions import (
    ALL_PERMISSIONS,
    FULL_SYNC, INVITE, KICK, MANAGE_CHANNEL, MANAGE_ROLES,
    PRESET_OPEN, PRESET_PRIVATE, PRESET_SERVER, PRESETS, ROLE_ADMIN,
    ROLE_MEMBER, ROLE_OWNER, SEND_MESSAGE, SHARE_FILES, has_permission,
    is_discoverable, is_open_join, mentions_permission, offered_permissions,
    permissions_from_json, permissions_to_json, role_rank,
)


@pytest.fixture
def db(tmp_path) -> Storage:
    s = Storage(db_path=tmp_path / "test.db")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Pure permission helpers (no DB)
# ---------------------------------------------------------------------------

class TestPermissionHelpers:
    def test_owner_has_all_permissions(self):
        for perm in ALL_PERMISSIONS:
            assert has_permission(PRESET_PRIVATE, ROLE_OWNER, perm) is True

    def test_admin_private_preset(self):
        assert has_permission(PRESET_PRIVATE, ROLE_ADMIN, SEND_MESSAGE)
        assert has_permission(PRESET_PRIVATE, ROLE_ADMIN, INVITE)
        assert has_permission(PRESET_PRIVATE, ROLE_ADMIN, KICK)
        assert has_permission(PRESET_PRIVATE, ROLE_ADMIN, MANAGE_ROLES)
        assert not has_permission(PRESET_PRIVATE, ROLE_ADMIN, MANAGE_CHANNEL)

    def test_member_private_preset(self):
        assert has_permission(PRESET_PRIVATE, ROLE_MEMBER, SEND_MESSAGE)
        assert not has_permission(PRESET_PRIVATE, ROLE_MEMBER, INVITE)
        assert not has_permission(PRESET_PRIVATE, ROLE_MEMBER, KICK)

    def test_member_open_preset_can_invite(self):
        assert has_permission(PRESET_OPEN, ROLE_MEMBER, INVITE)

    def test_role_rank_ordering(self):
        assert role_rank(ROLE_OWNER) > role_rank(ROLE_ADMIN) > role_rank(ROLE_MEMBER)

    def test_is_open_join(self):
        assert is_open_join(PRESET_OPEN) is True
        assert is_open_join(PRESET_PRIVATE) is False

    def test_is_discoverable(self):
        assert is_discoverable(PRESET_OPEN) is True
        assert is_discoverable(PRESET_PRIVATE) is False

    def test_full_sync_off_by_default_for_every_role(self):
        """full_sync is a per-role permission, same shape as send_message/
        invite/etc -- off for both roles under the default presets, exactly
        like every other permission not explicitly granted."""
        for perms in (PRESET_OPEN, PRESET_PRIVATE):
            assert not has_permission(perms, ROLE_ADMIN, FULL_SYNC)
            assert not has_permission(perms, ROLE_MEMBER, FULL_SYNC)

    def test_full_sync_missing_from_role_list_defaults_false(self):
        """A channel bootstrapped before this permission existed has no
        full_sync entry in its role lists at all -- must default to the
        restrictive behavior, not open up full history sync silently."""
        legacy_perms = dict(PRESET_PRIVATE)
        assert FULL_SYNC not in legacy_perms.get(ROLE_ADMIN, [])
        assert not has_permission(legacy_perms, ROLE_ADMIN, FULL_SYNC)

    def test_full_sync_can_be_granted_to_admin_but_not_member(self):
        """The exact scenario this permission exists for: an admin can be
        trusted with full history backfill while ordinary members remain
        bounded to their own join time."""
        perms = dict(PRESET_PRIVATE)
        perms[ROLE_ADMIN] = [SEND_MESSAGE, INVITE, KICK, MANAGE_ROLES, FULL_SYNC]
        assert has_permission(perms, ROLE_ADMIN, FULL_SYNC)
        assert not has_permission(perms, ROLE_MEMBER, FULL_SYNC)

    def test_owner_always_has_full_sync(self):
        assert has_permission(PRESET_PRIVATE, ROLE_OWNER, FULL_SYNC)

    def test_json_roundtrip(self):
        blob = permissions_to_json(PRESET_PRIVATE)
        assert isinstance(blob, str)
        restored = permissions_from_json(blob)
        assert restored["open_join"] == PRESET_PRIVATE["open_join"]
        assert set(restored["admin"]) == set(PRESET_PRIVATE["admin"])


# ---------------------------------------------------------------------------
# Storage permission methods
# ---------------------------------------------------------------------------

class TestStoragePermissions:
    def _seed(self, db):
        db.upsert_channel("ch01", "Test", "", "creator", PRESET_PRIVATE, time.time())
        db.upsert_member("ch01", "owner_id", "Owner", role=ROLE_OWNER)
        db.upsert_member("ch01", "admin_id", "Admin", role=ROLE_ADMIN)
        db.upsert_member("ch01", "member_id", "Member", role=ROLE_MEMBER)

    def test_get_role(self, db):
        self._seed(db)
        assert db.get_role("ch01", "owner_id") == ROLE_OWNER
        assert db.get_role("ch01", "admin_id") == ROLE_ADMIN
        assert db.get_role("ch01", "member_id") == ROLE_MEMBER
        assert db.get_role("ch01", "stranger") is None

    def test_has_permission_owner(self, db):
        self._seed(db)
        for perm in ALL_PERMISSIONS:
            assert db.has_permission("ch01", "owner_id", perm) is True

    def test_has_permission_admin(self, db):
        self._seed(db)
        assert db.has_permission("ch01", "admin_id", SEND_MESSAGE)
        assert db.has_permission("ch01", "admin_id", INVITE)
        assert db.has_permission("ch01", "admin_id", KICK)
        assert not db.has_permission("ch01", "admin_id", MANAGE_CHANNEL)

    def test_has_permission_member(self, db):
        self._seed(db)
        assert db.has_permission("ch01", "member_id", SEND_MESSAGE)
        assert not db.has_permission("ch01", "member_id", INVITE)

    def test_has_permission_non_member(self, db):
        self._seed(db)
        assert not db.has_permission("ch01", "stranger", SEND_MESSAGE)

    def test_get_channel_permissions(self, db):
        self._seed(db)
        perms = db.get_channel_permissions("ch01")
        assert perms["open_join"] is False
        assert SEND_MESSAGE in perms["member"]

    def test_set_channel_permissions(self, db):
        self._seed(db)
        custom = dict(PRESET_PRIVATE)
        custom["member"] = [SEND_MESSAGE, INVITE]
        db.set_channel_permissions("ch01", custom)
        assert db.has_permission("ch01", "member_id", INVITE)

    def test_open_preset_member_can_invite(self, db):
        db.upsert_channel("ch02", "Open", "", "creator", PRESET_OPEN, time.time())
        db.upsert_member("ch02", "member_id", "Member", role=ROLE_MEMBER)
        assert db.has_permission("ch02", "member_id", INVITE)


# ---------------------------------------------------------------------------
# Role-based channel creation
# ---------------------------------------------------------------------------

class TestRoleBasedCreation:
    def test_creator_gets_owner_role(self, peer_factory):
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("owner-test", "", "invite")
        assert alice.storage.get_role(ch_hash, alice.identity.hash_hex) == ROLE_OWNER

    def test_creator_has_all_permissions(self, peer_factory):
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("perms-test", "", "invite")
        for perm in ALL_PERMISSIONS:
            assert alice.storage.has_permission(ch_hash, alice.identity.hash_hex, perm)


# ---------------------------------------------------------------------------
# Invite with member permission
# ---------------------------------------------------------------------------

class TestMemberInvitePermission:
    def test_member_with_invite_can_approve_join(self, peer_factory):
        """When a channel grants INVITE to members, a member can approve joins."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        custom_perms = dict(PRESET_PRIVATE)
        custom_perms["member"] = [SEND_MESSAGE, INVITE]
        ch_hash = alice.channel_mgr.create_channel(
            "open-invite", "", permissions=custom_perms,
        )
        alice.invite_mgr.publish_member_list(ch_hash)

        alice.invite_mgr.publish_member_list(
            ch_hash, add_members=[bob.identity.hash],
        )

        from tests.helpers import wait_for_member
        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)
        assert alice.storage.has_permission(ch_hash, bob.identity.hash_hex, INVITE)


# ---------------------------------------------------------------------------
# broadcast_permissions: owner role preserved after permissions update
# ---------------------------------------------------------------------------

class TestBroadcastPermissions:
    def test_owner_role_preserved_after_broadcast(self, peer_factory):
        """broadcast_permissions must not demote the owner in the local members table."""
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("perm-broadcast", "", "invite")
        alice.invite_mgr.publish_member_list(ch_hash)

        # Remove send_message from members (the scenario that triggered the bug)
        custom = dict(PRESET_PRIVATE)
        custom["member"] = []
        alice.storage.set_channel_permissions(ch_hash, custom)
        alice.invite_mgr.broadcast_permissions(ch_hash)

        assert alice.storage.get_role(ch_hash, alice.identity.hash_hex) == ROLE_OWNER
        assert alice.storage.has_permission(ch_hash, alice.identity.hash_hex, MANAGE_CHANNEL)

    def test_permissions_updated_in_db_after_broadcast(self, peer_factory):
        """The new permissions dict is persisted before broadcast_permissions is called."""
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("perm-db", "", "invite")

        custom = dict(PRESET_PRIVATE)
        custom["member"] = [SEND_MESSAGE, INVITE]
        alice.storage.set_channel_permissions(ch_hash, custom)
        alice.invite_mgr.broadcast_permissions(ch_hash)

        assert alice.storage.has_permission(ch_hash, alice.identity.hash_hex, MANAGE_CHANNEL)
        perms = alice.storage.get_channel_permissions(ch_hash)
        assert INVITE in perms.get(ROLE_MEMBER, [])

    def test_version_incremented_after_broadcast(self, peer_factory):
        """broadcast_permissions increments the member list version."""
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("perm-ver", "", "invite")
        alice.invite_mgr.publish_member_list(ch_hash)

        before = alice.storage.get_member_list_version(ch_hash)
        alice.invite_mgr.broadcast_permissions(ch_hash)
        after = alice.storage.get_member_list_version(ch_hash)

        assert after["version"] == before["version"] + 1

    def test_owner_role_preserved_after_promoting_member_to_admin(self, peer_factory):
        """
        Regression: promoting a member to admin via publish_member_list must not
        demote the owner.  v1 docs lack an 'owners' key; the fix recovers the
        owner from the channel creator_hash so the next publish does not lose it.
        """
        alice = peer_factory("alice")
        bob   = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("promote-regression", "", "invite")
        alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])

        # Promote Bob to admin; this is the operation that triggered the bug
        alice.invite_mgr.publish_member_list(ch_hash, add_admins=[bob.identity.hash])

        assert alice.storage.get_role(ch_hash, alice.identity.hash_hex) == ROLE_OWNER, \
            "Owner was demoted after promoting a member to admin"
        assert alice.storage.has_permission(ch_hash, alice.identity.hash_hex, MANAGE_CHANNEL), \
            "Owner lost MANAGE_CHANNEL after promoting a member to admin"
        assert alice.storage.get_role(ch_hash, bob.identity.hash_hex) == ROLE_ADMIN, \
            "Bob was not promoted to admin"


class TestVoiceChatPermission:
    def test_voice_chat_in_all_permissions(self):
        from trenchchat.core.permissions import VOICE_CHAT
        assert VOICE_CHAT in ALL_PERMISSIONS

    def test_presets_grant_voice_chat_to_member_and_admin(self):
        from trenchchat.core.permissions import PRESET_SERVER, VOICE_CHAT
        for perms in (PRESET_PRIVATE, PRESET_OPEN, PRESET_SERVER):
            assert has_permission(perms, ROLE_MEMBER, VOICE_CHAT)
            assert has_permission(perms, ROLE_ADMIN, VOICE_CHAT)
            assert has_permission(perms, ROLE_OWNER, VOICE_CHAT)

    def test_voice_chat_missing_from_legacy_blob_fails_closed(self):
        """Channels created before this permission existed have no
        voice_chat entry in their stored role lists -- members there must
        be denied voice until an owner re-publishes permissions."""
        from trenchchat.core.permissions import VOICE_CHAT
        legacy = {ROLE_ADMIN: [SEND_MESSAGE], ROLE_MEMBER: [SEND_MESSAGE]}
        assert not has_permission(legacy, ROLE_MEMBER, VOICE_CHAT)
        assert not has_permission(legacy, ROLE_ADMIN, VOICE_CHAT)
        assert has_permission(legacy, ROLE_OWNER, VOICE_CHAT)


# ---------------------------------------------------------------------------
# Some permissions are not the base role's to hold
# ---------------------------------------------------------------------------

class TestAdminOnlyPermissions:
    """Removing someone from the member list strips every permission they had,
    so kick is the authority to unmake other people's. manage_roles is
    restricted with it because it is the route to granting yourself kick.
    """

    def test_a_member_grant_is_dropped_on_read(self):
        blob = json.dumps({ROLE_MEMBER: [SEND_MESSAGE, KICK, MANAGE_ROLES]})
        perms = permissions_from_json(blob)
        assert perms[ROLE_MEMBER] == [SEND_MESSAGE]

    def test_a_member_grant_is_dropped_on_write(self):
        blob = permissions_to_json({ROLE_MEMBER: [SEND_MESSAGE, KICK]})
        assert KICK not in json.loads(blob)[ROLE_MEMBER]

    def test_admins_keep_both(self):
        perms = permissions_from_json(
            json.dumps({ROLE_ADMIN: [KICK, MANAGE_ROLES]}))
        assert perms[ROLE_ADMIN] == [KICK, MANAGE_ROLES]

    def test_has_permission_refuses_a_smuggled_member_grant(self):
        """The check every core enforcement point runs."""
        perms = permissions_from_json(json.dumps({ROLE_MEMBER: [KICK]}))
        assert has_permission(perms, ROLE_MEMBER, KICK) is False
        assert has_permission(perms, ROLE_OWNER, KICK) is True

    def test_a_signed_document_cannot_grant_it_either(self, peer_factory):
        """A signature proves who wrote a blob, not that what it says is
        allowed -- so the drop has to happen on the read path, not only where
        permissions are edited locally."""
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("c", "", permissions=PRESET_PRIVATE)

        smuggled = dict(PRESET_PRIVATE)
        smuggled[ROLE_MEMBER] = [SEND_MESSAGE, KICK]
        alice.storage.set_channel_permissions(ch_hash, smuggled)

        stored = permissions_from_json(
            alice.storage.get_channel(ch_hash)["permissions"])
        assert KICK not in stored[ROLE_MEMBER]

    def test_full_sync_is_not_offered_on_an_open_channel(self):
        """It decides how much history a member may pull, and an open-join
        channel serves history to any subscriber -- so the toggle would be a
        privacy control that is not one."""
        assert FULL_SYNC not in offered_permissions(PRESET_OPEN, ROLE_MEMBER)
        assert FULL_SYNC in offered_permissions(PRESET_PRIVATE, ROLE_MEMBER)

    def test_kick_is_never_offered_to_a_member(self):
        for preset in (PRESET_OPEN, PRESET_PRIVATE, PRESET_SERVER):
            offered = offered_permissions(preset, ROLE_MEMBER)
            assert KICK not in offered
            assert MANAGE_ROLES not in offered
            assert SEND_MESSAGE in offered


# ---------------------------------------------------------------------------
# SHARE_FILES: a file is a message, so send_message is the floor
# ---------------------------------------------------------------------------

class TestShareFilesPermission:
    """share_files lets an admin keep a channel text-only for a role.

    It is not admin-only, and it is the one permission with a compatibility
    rule: a blob written before it existed names it nowhere, and that reads as
    "granted wherever send_message is". The rule lives in permissions.py and
    is asserted here, at the storage layer, and adversarially.
    """

    def test_share_files_in_all_permissions(self):
        assert SHARE_FILES in ALL_PERMISSIONS

    def test_presets_grant_it_to_member_and_admin(self):
        for perms in (PRESET_PRIVATE, PRESET_OPEN, PRESET_SERVER):
            assert has_permission(perms, ROLE_MEMBER, SHARE_FILES)
            assert has_permission(perms, ROLE_ADMIN, SHARE_FILES)
            assert has_permission(perms, ROLE_OWNER, SHARE_FILES)

    def test_every_preset_role_that_may_send_may_share(self):
        for perms in PRESETS.values():
            for role in (ROLE_ADMIN, ROLE_MEMBER):
                if has_permission(perms, role, SEND_MESSAGE):
                    assert has_permission(perms, role, SHARE_FILES), \
                        f"{role} may send but not share under {perms}"

    def test_a_blob_predating_it_shares_wherever_it_sends(self):
        """A channel bootstrapped before the permission existed keeps working.

        Its role lists name share_files nowhere, which cannot be told apart
        from a blob that dropped it on purpose, so the reading is the one that
        does not silently take file sharing away from existing channels.
        """
        legacy = {ROLE_ADMIN: [SEND_MESSAGE, INVITE], ROLE_MEMBER: [SEND_MESSAGE]}
        assert has_permission(legacy, ROLE_ADMIN, SHARE_FILES)
        assert has_permission(legacy, ROLE_MEMBER, SHARE_FILES)

    def test_a_legacy_role_that_cannot_send_cannot_share(self):
        legacy = {ROLE_ADMIN: [SEND_MESSAGE], ROLE_MEMBER: []}
        assert not has_permission(legacy, ROLE_MEMBER, SHARE_FILES)

    def test_one_mention_makes_every_role_list_explicit(self):
        """The scenario the permission exists for: admins share, members do not."""
        perms = {ROLE_ADMIN: [SEND_MESSAGE, SHARE_FILES],
                 ROLE_MEMBER: [SEND_MESSAGE]}
        assert has_permission(perms, ROLE_ADMIN, SHARE_FILES)
        assert not has_permission(perms, ROLE_MEMBER, SHARE_FILES)

    def test_the_owner_always_shares(self):
        assert has_permission({ROLE_MEMBER: []}, ROLE_OWNER, SHARE_FILES)

    def test_send_message_implies_nothing_else(self):
        legacy = {ROLE_MEMBER: [SEND_MESSAGE]}
        for perm in ALL_PERMISSIONS:
            if perm in (SEND_MESSAGE, SHARE_FILES):
                continue
            assert not has_permission(legacy, ROLE_MEMBER, perm), \
                f"{perm} was implied by send_message"

    def test_mentions_permission_reads_role_lists_only(self):
        assert mentions_permission(PRESET_PRIVATE, SHARE_FILES)
        assert not mentions_permission({ROLE_MEMBER: [SEND_MESSAGE]}, SHARE_FILES)
        assert not mentions_permission({"open_join": False}, SHARE_FILES)

    def test_it_is_offered_to_both_roles(self):
        assert SHARE_FILES in offered_permissions(PRESET_PRIVATE, ROLE_MEMBER)
        assert SHARE_FILES in offered_permissions(PRESET_PRIVATE, ROLE_ADMIN)

    def test_storage_applies_the_rule_to_a_stored_legacy_blob(self, db):
        legacy = {"open_join": False, ROLE_ADMIN: [SEND_MESSAGE],
                  ROLE_MEMBER: [SEND_MESSAGE]}
        db.upsert_channel("ch_legacy", "Legacy", "", "creator", legacy, time.time())
        db.upsert_member("ch_legacy", "member_id", "Member", role=ROLE_MEMBER)
        assert db.has_permission("ch_legacy", "member_id", SHARE_FILES)

    def test_storage_denies_a_member_an_admin_kept_to_text(self, db):
        perms = dict(PRESET_PRIVATE)
        perms[ROLE_MEMBER] = [SEND_MESSAGE]
        db.upsert_channel("ch_text", "Text only", "", "creator", perms, time.time())
        db.upsert_member("ch_text", "member_id", "Member", role=ROLE_MEMBER)
        db.upsert_member("ch_text", "admin_id", "Admin", role=ROLE_ADMIN)
        assert db.has_permission("ch_text", "member_id", SEND_MESSAGE)
        assert not db.has_permission("ch_text", "member_id", SHARE_FILES)
        assert db.has_permission("ch_text", "admin_id", SHARE_FILES)

    def test_a_json_round_trip_keeps_the_grant(self, db):
        restored = permissions_from_json(permissions_to_json(PRESET_PRIVATE))
        assert has_permission(restored, ROLE_MEMBER, SHARE_FILES)
