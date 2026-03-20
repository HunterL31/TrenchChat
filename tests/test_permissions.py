"""
Tests for the role-based permission system.

Covers:
- Permission checking via permissions module helpers
- Storage.has_permission / get_role / get_channel_permissions
- Owner immutability (always has all permissions)
- Preset permission configurations
- Member invite permission enables invite flow
"""

import time

import pytest

from trenchchat.core.storage import Storage
from trenchchat.core.permissions import (
    ALL_PERMISSIONS, INVITE, KICK, MANAGE_CHANNEL, MANAGE_ROLES,
    PRESET_OPEN, PRESET_PRIVATE, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER,
    SEND_MESSAGE, has_permission, is_discoverable, is_open_join,
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
