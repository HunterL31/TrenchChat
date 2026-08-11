"""
Integration tests for servers — collections of channels sharing one membership.

The contract under test:
  - one invite to a server admits a peer to every channel in it
  - a peer has exactly one role, applying across all the server's channels
  - a channel created *after* a peer joined still reaches them
  - permission changes mirror down to every child channel
  - standalone channels are entirely unaffected
"""

import time

import pytest

from tests.helpers import wait_for, wait_for_member
from trenchchat.core import actions
from trenchchat.core.naming import server_hash_for
from trenchchat.core.permissions import (
    CREATE_CHANNEL, PRESET_PRIVATE, PRESET_SERVER, ROLE_ADMIN, ROLE_MEMBER,
    ROLE_OWNER, SEND_MESSAGE, is_open_join, permissions_from_json,
)


def _invite_and_join(inviter, invitee, scope_hash: str):
    """Drive the real invite -> join_request -> member_list_update handshake."""
    def on_invite(scope_hex, name, token, expiry, admin_hex):
        invitee.invite_mgr.send_join_request(scope_hex, token, expiry, admin_hex)

    invitee.invite_mgr.add_invite_callback(on_invite)
    inviter.invite_mgr.send_invite(scope_hash, invitee.identity.hash_hex)
    assert wait_for_member(inviter.storage, scope_hash,
                           invitee.identity.hash_hex, timeout=5), \
        "invitee never appeared in the inviter's member list"


class TestServerCreation:
    def test_server_hash_is_deterministic(self, peer_factory):
        alice = peer_factory("alice")
        h = alice.server_mgr.create_server("My Server")
        assert h == server_hash_for(alice.identity.hash, "My Server")

    def test_different_identities_mint_different_hashes(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        assert alice.server_mgr.create_server("Same Name") != \
            bob.server_mgr.create_server("Same Name")

    def test_creator_is_owner_with_tenure(self, peer_factory):
        alice = peer_factory("alice")
        h = alice.server_mgr.create_server("S")
        assert alice.storage.get_role(h, alice.identity.hash_hex) == ROLE_OWNER
        assert alice.storage.has_any_tenure(h) is True

    def test_server_is_never_open_join(self, peer_factory):
        alice = peer_factory("alice")
        h = alice.server_mgr.create_server("S")
        assert is_open_join(alice.storage.get_server_permissions(h)) is False

    def test_server_does_not_appear_in_channel_list(self, peer_factory):
        alice = peer_factory("alice")
        h = alice.server_mgr.create_server("S")
        assert h not in {r["hash"] for r in alice.storage.get_all_channels()}


class TestChannelsInServer:
    def test_create_channel_in_server_sets_parent_and_inherits_permissions(self, peer_factory):
        alice = peer_factory("alice")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "S")
        ch = actions.create_channel_in_server(
            alice.storage, alice.channel_mgr, alice.invite_mgr,
            s, alice.identity.hash_hex, "general",
        )
        assert ch is not None
        assert alice.storage.get_channel(ch)["server_hash"] == s
        assert alice.storage.scope_for(ch) == s
        perms = permissions_from_json(alice.storage.get_channel(ch)["permissions"])
        assert is_open_join(perms) is False

    def test_channel_in_server_has_no_member_rows_of_its_own(self, peer_factory):
        alice = peer_factory("alice")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "S")
        ch = actions.create_channel_in_server(
            alice.storage, alice.channel_mgr, alice.invite_mgr,
            s, alice.identity.hash_hex, "general",
        )
        rows = alice.storage._conn.execute(
            "SELECT 1 FROM members WHERE channel_hash = ?", (ch,)
        ).fetchall()
        assert rows == []
        # But the resolving read still reports the server's owner.
        assert alice.storage.get_role(ch, alice.identity.hash_hex) == ROLE_OWNER

    def test_unknown_server_returns_none(self, peer_factory):
        alice = peer_factory("alice")
        assert actions.create_channel_in_server(
            alice.storage, alice.channel_mgr, alice.invite_mgr,
            "ff" * 16, alice.identity.hash_hex, "nope",
        ) is None


class TestOneInviteGrantsEveryChannel:
    def test_invitee_joins_all_existing_channels(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "S")
        general = actions.create_channel_in_server(
            alice.storage, alice.channel_mgr, alice.invite_mgr,
            s, alice.identity.hash_hex, "general")
        random_ch = actions.create_channel_in_server(
            alice.storage, alice.channel_mgr, alice.invite_mgr,
            s, alice.identity.hash_hex, "random")

        _invite_and_join(alice, bob, s)

        assert wait_for(lambda: bob.storage.get_server(s) is not None, timeout=5), \
            "bob never materialised the server"
        for ch in (general, random_ch):
            assert wait_for(lambda c=ch: bob.storage.get_channel(c) is not None,
                            timeout=5), f"bob never received channel {ch[:12]}"
            assert bob.storage.get_channel(ch)["server_hash"] == s
            assert bob.storage.is_subscribed(ch) is True

    def test_one_role_applies_to_every_channel(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "S")
        channels = [
            actions.create_channel_in_server(
                alice.storage, alice.channel_mgr, alice.invite_mgr,
                s, alice.identity.hash_hex, n)
            for n in ("general", "random")
        ]
        _invite_and_join(alice, bob, s)

        bob_hex = bob.identity.hash_hex
        for ch in channels:
            assert alice.storage.get_role(ch, bob_hex) == ROLE_MEMBER
            assert alice.storage.has_permission(ch, bob_hex, SEND_MESSAGE) is True
            assert alice.storage.has_permission(ch, bob_hex, CREATE_CHANNEL) is False

    def test_channel_created_after_join_reaches_existing_member(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "S")
        actions.create_channel_in_server(
            alice.storage, alice.channel_mgr, alice.invite_mgr,
            s, alice.identity.hash_hex, "general")
        _invite_and_join(alice, bob, s)
        assert wait_for(lambda: bob.storage.get_server(s) is not None, timeout=5)

        late = actions.create_channel_in_server(
            alice.storage, alice.channel_mgr, alice.invite_mgr,
            s, alice.identity.hash_hex, "later")

        assert wait_for(lambda: bob.storage.get_channel(late) is not None, timeout=5), \
            "a channel created after bob joined never reached him"
        assert bob.storage.is_subscribed(late) is True

    def test_message_in_server_channel_reaches_all_members(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "S")
        ch = actions.create_channel_in_server(
            alice.storage, alice.channel_mgr, alice.invite_mgr,
            s, alice.identity.hash_hex, "general")
        _invite_and_join(alice, bob, s)
        assert wait_for(lambda: bob.storage.get_channel(ch) is not None, timeout=5)

        assert actions.send_message(
            alice.storage, alice.subscription_mgr, alice.messaging,
            ch, alice.identity.hash_hex, "hello server",
        ) is True
        assert wait_for(
            lambda: any(m["content"] == "hello server"
                        for m in bob.storage.get_messages(ch)),
            timeout=5,
        ), "message never reached bob"

    def test_recipients_for_server_channel_are_the_server_membership(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "S")
        ch = actions.create_channel_in_server(
            alice.storage, alice.channel_mgr, alice.invite_mgr,
            s, alice.identity.hash_hex, "general")
        _invite_and_join(alice, bob, s)

        recipients = actions.compute_channel_recipients(
            alice.storage, alice.subscription_mgr, ch, alice.identity.hash_hex)
        assert set(recipients) == {alice.identity.hash_hex, bob.identity.hash_hex}


class TestServerPermissions:
    def test_permission_change_mirrors_into_every_channel(self, peer_factory):
        alice = peer_factory("alice")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "S")
        channels = [
            actions.create_channel_in_server(
                alice.storage, alice.channel_mgr, alice.invite_mgr,
                s, alice.identity.hash_hex, n)
            for n in ("general", "random")
        ]
        new_perms = dict(PRESET_SERVER)
        new_perms[ROLE_MEMBER] = []
        assert actions.edit_server_permissions(
            alice.storage, alice.invite_mgr, s, alice.identity.hash_hex, new_perms,
        ) is True

        for ch in channels:
            mirrored = permissions_from_json(alice.storage.get_channel(ch)["permissions"])
            assert mirrored[ROLE_MEMBER] == []

    def test_edit_denied_without_manage_channel(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "S")
        _invite_and_join(alice, bob, s)
        assert wait_for(lambda: bob.storage.get_server(s) is not None, timeout=5)

        assert actions.edit_server_permissions(
            bob.storage, bob.invite_mgr, s, bob.identity.hash_hex, dict(PRESET_SERVER),
        ) is False


class TestStandaloneChannelsUnaffected:
    def test_standalone_channel_keeps_its_own_scope_and_members(self, peer_factory):
        alice = peer_factory("alice")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "S")
        actions.create_channel_in_server(
            alice.storage, alice.channel_mgr, alice.invite_mgr,
            s, alice.identity.hash_hex, "general")
        solo = actions.create_channel(
            alice.channel_mgr, alice.invite_mgr, "solo", "", dict(PRESET_PRIVATE))

        assert alice.storage.get_channel(solo)["server_hash"] is None
        assert alice.storage.scope_for(solo) == solo
        assert alice.storage.get_role(solo, alice.identity.hash_hex) == ROLE_OWNER

    def test_server_membership_does_not_leak_into_standalone_channel(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "S")
        solo = actions.create_channel(
            alice.channel_mgr, alice.invite_mgr, "solo", "", dict(PRESET_PRIVATE))
        _invite_and_join(alice, bob, s)

        assert alice.storage.is_member(solo, bob.identity.hash_hex) is False
        assert alice.storage.get_role(solo, bob.identity.hash_hex) is None


# ---------------------------------------------------------------------------
# Qt GUI port
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qt_app():
    """Module-scoped QApplication for headless GUI tests."""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class TestNewServerDialog:
    def test_exposes_name_and_description(self, qt_app):
        from trenchchat.gui.main_window import NewServerDialog
        dlg = NewServerDialog()
        dlg._name.setText("  Trench HQ  ")
        dlg._desc.setText(" the place ")
        assert dlg.server_name == "Trench HQ"
        assert dlg.description == "the place"

    def test_no_preset_picker_for_a_channel_in_a_server(self, qt_app):
        """Server channels inherit the server's permissions, so offering a
        preset would imply an override that doesn't exist."""
        from trenchchat.gui.main_window import NewChannelDialog
        assert NewChannelDialog(in_server=True)._preset is None
        assert NewChannelDialog(in_server=False)._preset is not None


class _FakeWindow:
    """Minimal stand-in so _scope_for can be exercised without building a
    MainWindow (which needs the full manager graph and a running Reticulum)."""

    def __init__(self, storage):
        self._storage = storage


class TestGuiPermissionScope:
    """Regression: editing permissions from a channel inside a server must write
    the *server*, not the channel. Writing the channel row leaves the server
    unchanged and is silently clobbered by the next accepted document."""

    def test_scope_resolves_server_channel_to_its_server(self, peer_factory):
        alice = peer_factory("alice")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "HQ")
        ch = actions.create_channel_in_server(
            alice.storage, alice.channel_mgr, alice.invite_mgr,
            s, alice.identity.hash_hex, "general")
        solo = actions.create_channel(
            alice.channel_mgr, alice.invite_mgr, "solo", "", dict(PRESET_PRIVATE))

        from trenchchat.gui.main_window import MainWindow
        scope_for = MainWindow._scope_for.__get__(_FakeWindow(alice.storage))
        assert scope_for(ch) == ("server", s, "HQ")
        assert scope_for(solo)[:2] == ("channel", solo)

    def test_editing_from_a_server_channel_writes_the_server(self, peer_factory):
        alice = peer_factory("alice")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "HQ")
        ch = actions.create_channel_in_server(
            alice.storage, alice.channel_mgr, alice.invite_mgr,
            s, alice.identity.hash_hex, "general")

        new_perms = dict(PRESET_SERVER)
        new_perms[ROLE_MEMBER] = [SEND_MESSAGE, "kick"]
        assert actions.edit_server_permissions(
            alice.storage, alice.invite_mgr, s, alice.identity.hash_hex, new_perms,
        ) is True

        assert alice.storage.get_server_permissions(s)[ROLE_MEMBER] == [SEND_MESSAGE, "kick"]
        mirrored = permissions_from_json(alice.storage.get_channel(ch)["permissions"])
        assert mirrored[ROLE_MEMBER] == [SEND_MESSAGE, "kick"]
        assert is_open_join(mirrored) is False


class TestServerCollapse:
    """Collapsing a server hides its channels without disturbing anything else."""

    def _window_stub(self, storage, server_mgr, collapsed=()):
        from trenchchat.gui.main_window import MainWindow

        class _Stub:
            pass

        w = _Stub()
        w._storage = storage
        w._server_mgr = server_mgr
        w._collapsed_servers = set(collapsed)
        w._rows = []
        # Exercise the real grouping logic against a list-widget stand-in.
        w._add_channel_item = lambda row, indented=False: w._rows.append(
            ("channel", row["hash"], indented))
        w._add_server_header = lambda server, n: w._rows.append(
            ("server", server["hash"], n))
        w._current_channel = None
        w._highlight_channel_in_list = lambda h: None

        class _LW:
            def blockSignals(self, _): pass
            def clear(self): w._rows.clear()
        w._channel_list_widget = _LW()
        w._refresh_channel_list = MainWindow._refresh_channel_list.__get__(w)
        return w

    def _server_with_channels(self, peer, names):
        s = actions.create_server(peer.server_mgr, peer.invite_mgr, "HQ")
        for n in names:
            actions.create_channel_in_server(
                peer.storage, peer.channel_mgr, peer.invite_mgr,
                s, peer.identity.hash_hex, n)
        return s

    def test_expanded_lists_every_channel(self, peer_factory):
        alice = peer_factory("alice")
        s = self._server_with_channels(alice, ("general", "random"))
        w = self._window_stub(alice.storage, alice.server_mgr)
        w._refresh_channel_list()
        assert w._rows[0] == ("server", s, 2)
        assert [r for r in w._rows if r[0] == "channel"] != []

    def test_collapsed_hides_children_but_keeps_the_header(self, peer_factory):
        alice = peer_factory("alice")
        s = self._server_with_channels(alice, ("general", "random"))
        w = self._window_stub(alice.storage, alice.server_mgr, collapsed={s})
        w._refresh_channel_list()
        assert w._rows == [("server", s, 2)], \
            "collapsing must hide the channels and keep the header, with a count"

    def test_collapsing_one_server_does_not_hide_standalone_channels(self, peer_factory):
        alice = peer_factory("alice")
        s = self._server_with_channels(alice, ("general",))
        solo = actions.create_channel(
            alice.channel_mgr, alice.invite_mgr, "solo", "", dict(PRESET_PRIVATE))
        w = self._window_stub(alice.storage, alice.server_mgr, collapsed={s})
        w._refresh_channel_list()
        assert ("channel", solo, False) in w._rows

    def test_header_count_reflects_subscribed_channels(self, peer_factory):
        alice = peer_factory("alice")
        s = self._server_with_channels(alice, ("a", "b", "c"))
        w = self._window_stub(alice.storage, alice.server_mgr, collapsed={s})
        w._refresh_channel_list()
        assert w._rows[0][2] == 3
