"""
Tests for the role-based permission system.

Covers:
- Permission checking via permissions module helpers
- Storage.has_permission / get_role / get_channel_permissions
- Owner immutability (always has all permissions)
- Preset permission configurations
- Member invite permission enables invite flow
- ChannelPermissionsDialog UI: initial state and updated permissions property
"""

import os
import time

import pytest

from trenchchat.core.storage import Storage
from trenchchat.core.permissions import (
    ALL_PERMISSIONS, FLAG_DISCOVERABLE, FLAG_OPEN_JOIN,
    INVITE, KICK, MANAGE_CHANNEL, MANAGE_ROLES,
    PRESET_OPEN, PRESET_PRIVATE, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER,
    SEND_MESSAGE, has_permission, is_discoverable, is_open_join,
    permissions_from_json, permissions_to_json, role_rank,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


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


# ---------------------------------------------------------------------------
# broadcast_permissions — owner role preserved after permissions update
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

        # Promote Bob to admin — this is the operation that triggered the bug
        alice.invite_mgr.publish_member_list(ch_hash, add_admins=[bob.identity.hash])

        assert alice.storage.get_role(ch_hash, alice.identity.hash_hex) == ROLE_OWNER, \
            "Owner was demoted after promoting a member to admin"
        assert alice.storage.has_permission(ch_hash, alice.identity.hash_hex, MANAGE_CHANNEL), \
            "Owner lost MANAGE_CHANNEL after promoting a member to admin"
        assert alice.storage.get_role(ch_hash, bob.identity.hash_hex) == ROLE_ADMIN, \
            "Bob was not promoted to admin"


# ---------------------------------------------------------------------------
# ChannelPermissionsDialog GUI unit tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qt_app():
    """Module-scoped QApplication for headless GUI tests."""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class TestChannelPermissionsDialog:
    def test_initial_state_reflects_preset(self, qt_app):
        """Dialog checkboxes match the permissions dict passed in."""
        from trenchchat.gui.invite_dialogs import ChannelPermissionsDialog
        dlg = ChannelPermissionsDialog("test", dict(PRESET_PRIVATE))
        perms = dlg.permissions
        assert perms[FLAG_OPEN_JOIN] is False
        assert perms[FLAG_DISCOVERABLE] is False
        assert SEND_MESSAGE in perms[ROLE_ADMIN]
        assert SEND_MESSAGE in perms[ROLE_MEMBER]
        assert INVITE not in perms[ROLE_MEMBER]

    def test_initial_state_open_preset(self, qt_app):
        """Open preset flags and member invite permission are reflected."""
        from trenchchat.gui.invite_dialogs import ChannelPermissionsDialog
        dlg = ChannelPermissionsDialog("open", dict(PRESET_OPEN))
        perms = dlg.permissions
        assert perms[FLAG_OPEN_JOIN] is True
        assert perms[FLAG_DISCOVERABLE] is True
        assert INVITE in perms[ROLE_MEMBER]

    def test_toggling_checkbox_updates_permissions(self, qt_app):
        """Changing a checkbox is reflected in the permissions property."""
        from trenchchat.gui.invite_dialogs import ChannelPermissionsDialog
        dlg = ChannelPermissionsDialog("test", dict(PRESET_PRIVATE))
        # Grant INVITE to members by checking the box
        invite_cb = dlg._role_checks[ROLE_MEMBER][INVITE]
        invite_cb.setChecked(True)
        perms = dlg.permissions
        assert INVITE in perms[ROLE_MEMBER]

    def test_toggling_flag_updates_permissions(self, qt_app):
        """Toggling the open_join flag is reflected in the permissions property."""
        from trenchchat.gui.invite_dialogs import ChannelPermissionsDialog
        dlg = ChannelPermissionsDialog("test", dict(PRESET_PRIVATE))
        dlg._open_join_cb.setChecked(True)
        dlg._discoverable_cb.setChecked(True)
        perms = dlg.permissions
        assert perms[FLAG_OPEN_JOIN] is True
        assert perms[FLAG_DISCOVERABLE] is True

    def test_owner_checkboxes_are_disabled(self, qt_app):
        """Owner permission checkboxes are always checked and disabled."""
        from PyQt6.QtWidgets import QGroupBox, QCheckBox
        from trenchchat.gui.invite_dialogs import ChannelPermissionsDialog
        dlg = ChannelPermissionsDialog("test", dict(PRESET_PRIVATE))
        # Find the owner group box
        owner_group = next(
            w for w in dlg.findChildren(QGroupBox)
            if "Owner" in w.title()
        )
        for cb in owner_group.findChildren(QCheckBox):
            assert cb.isChecked()
            assert not cb.isEnabled()

    def test_revoking_permission_removes_from_list(self, qt_app):
        """Unchecking a permission removes it from the returned list."""
        from trenchchat.gui.invite_dialogs import ChannelPermissionsDialog
        dlg = ChannelPermissionsDialog("test", dict(PRESET_PRIVATE))
        send_cb = dlg._role_checks[ROLE_ADMIN][SEND_MESSAGE]
        send_cb.setChecked(False)
        perms = dlg.permissions
        assert SEND_MESSAGE not in perms[ROLE_ADMIN]
