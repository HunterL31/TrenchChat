"""
Tests for trenchchat.core.actions -- the shared entry points both
main_window.py and devtools/testenv/api.py call, so a caller with no other
feedback loop (an HTTP response, a scripted test) can tell success apart
from a silently-filtered request.
"""

import time

import pytest

from tests.helpers import wait_for_member
from trenchchat.config import Config
from trenchchat.core import actions
from trenchchat.core.permissions import (
    FLAG_DISCOVERABLE, FLAG_OPEN_JOIN, PRESET_OPEN, PRESET_PRIVATE, ROLE_MEMBER, ROLE_OWNER,
)
from trenchchat.core.protocol import F_CHANNEL_HASH


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


class TestSettings:
    def test_read_settings_matches_config_defaults(self, peer_factory):
        alice = peer_factory("alice")

        settings = actions.read_settings(alice.config)

        assert settings == {
            "propagation_enabled": False,
            "propagation_node_name": "",
            "propagation_storage_limit_mb": 256,
            "channel_filter_mode": "allowlist",
            "channel_filter_hashes": [],
            "outbound_propagation_node": None,
        }

    def test_apply_settings_writes_simple_fields(self, peer_factory):
        alice = peer_factory("alice")

        actions.apply_settings(alice.config, alice.router, {
            "propagation_node_name": "my-node",
            "propagation_storage_limit_mb": 512,
            "channel_filter_mode": "all",
            "channel_filter_hashes": ["aa", "bb"],
        })

        assert alice.config.propagation_node_name == "my-node"
        assert alice.config.propagation_storage_limit_mb == 512
        assert alice.config.channel_filter_mode == "all"
        assert alice.config.channel_filter_hashes == ["aa", "bb"]

    def test_apply_settings_only_touches_provided_keys(self, peer_factory):
        alice = peer_factory("alice")
        alice.config.propagation_node_name = "keep-me"

        actions.apply_settings(alice.config, alice.router, {
            "propagation_storage_limit_mb": 128,
        })

        assert alice.config.propagation_node_name == "keep-me"
        assert alice.config.propagation_storage_limit_mb == 128

    def test_apply_settings_enables_propagation_via_router(self, peer_factory):
        alice = peer_factory("alice")

        actions.apply_settings(alice.config, alice.router, {"propagation_enabled": True})

        assert alice.config.propagation_enabled is True

    def test_apply_settings_disables_propagation_via_router(self, peer_factory):
        alice = peer_factory("alice")
        alice.router.enable_propagation()
        assert alice.config.propagation_enabled is True

        actions.apply_settings(alice.config, alice.router, {"propagation_enabled": False})

        assert alice.config.propagation_enabled is False

    def test_apply_settings_enable_is_a_noop_when_already_enabled(self, peer_factory):
        """A redundant enable=True must not re-trigger enable_propagation's
        side effects when nothing changed."""
        alice = peer_factory("alice")
        alice.router.enable_propagation()

        actions.apply_settings(alice.config, alice.router, {"propagation_enabled": True})

        assert alice.config.propagation_enabled is True

    def test_apply_settings_sets_outbound_propagation_node(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        actions.apply_settings(alice.config, alice.router, {
            "outbound_propagation_node": bob.identity.hash_hex,
        })

        assert alice.config.outbound_propagation_node == bob.identity.hash_hex

    def test_apply_settings_clears_outbound_propagation_node(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        alice.router.set_outbound_propagation_node(bob.identity.hash_hex)

        actions.apply_settings(alice.config, alice.router, {
            "outbound_propagation_node": None,
        })

        assert alice.config.outbound_propagation_node is None

    def test_apply_settings_rejects_invalid_channel_filter_mode(self, peer_factory):
        alice = peer_factory("alice")

        with pytest.raises(ValueError):
            actions.apply_settings(alice.config, alice.router, {
                "channel_filter_mode": "bogus",
            })


class TestUiTheme:
    def test_theme_defaults_to_empty(self, peer_factory):
        alice = peer_factory("alice")

        assert actions.read_ui_theme(alice.config) == {}

    def test_set_then_read_returns_the_same_object(self, peer_factory):
        alice = peer_factory("alice")
        theme = {"sidebar": {"bg": "#101010"}, "accent": "#ff8800"}

        actions.set_ui_theme(alice.config, theme)

        assert actions.read_ui_theme(alice.config) == theme

    def test_set_replaces_wholesale(self, peer_factory):
        alice = peer_factory("alice")
        actions.set_ui_theme(alice.config, {"sidebar": {"bg": "#101010"}})

        actions.set_ui_theme(alice.config, {"accent": "#ff8800"})

        assert actions.read_ui_theme(alice.config) == {"accent": "#ff8800"}

    def test_theme_persists_across_a_fresh_config(self, peer_factory):
        alice = peer_factory("alice")
        theme = {"message_list": {"bg": "#202020", "text": "#eeeeee"}}
        actions.set_ui_theme(alice.config, theme)

        reloaded = Config(data_dir=alice.data_dir)

        assert actions.read_ui_theme(reloaded) == theme


class TestVoiceActions:
    def test_authorized_join_returns_true(self, peer_factory):
        alice, bob, ch_hash = _setup_channel_with_member(peer_factory)
        joined = actions.join_voice_channel(
            bob.storage, bob.voice_mgr, ch_hash, bob.identity.hash_hex,
        )
        assert joined is True
        assert bob.voice_mgr.current_channel == ch_hash

    def test_unauthorized_join_returns_false_and_is_a_noop(self, peer_factory):
        alice, bob, ch_hash = _setup_channel_with_member(
            peer_factory, member_perms=[])  # no voice_chat
        joined = actions.join_voice_channel(
            bob.storage, bob.voice_mgr, ch_hash, bob.identity.hash_hex,
        )
        assert joined is False
        assert bob.voice_mgr.current_channel is None

    def test_unknown_channel_join_returns_false(self, peer_factory):
        bob = peer_factory("bob")
        assert actions.join_voice_channel(
            bob.storage, bob.voice_mgr, "ab" * 16, bob.identity.hash_hex,
        ) is False

    def test_leave_reports_whether_in_session(self, peer_factory):
        alice, bob, ch_hash = _setup_channel_with_member(peer_factory)
        assert actions.leave_voice_channel(bob.voice_mgr) is False
        actions.join_voice_channel(
            bob.storage, bob.voice_mgr, ch_hash, bob.identity.hash_hex,
        )
        assert actions.leave_voice_channel(bob.voice_mgr) is True
        assert bob.voice_mgr.current_channel is None

    def test_set_voice_muted_passthrough(self, peer_factory):
        alice, bob, ch_hash = _setup_channel_with_member(peer_factory)
        actions.join_voice_channel(
            bob.storage, bob.voice_mgr, ch_hash, bob.identity.hash_hex,
        )
        actions.set_voice_muted(bob.voice_mgr, True)
        assert bob.voice_mgr.is_muted is True


# ---------------------------------------------------------------------------
# The channel filter has to act where messages enter the propagation store
# ---------------------------------------------------------------------------

class TestPropagationFilterIsApplied:
    """Settings presents this as controlling what the node relays for other
    people. Its verdict used to be returned from the delivery callback, whose
    return value LXMF ignores, so it controlled nothing.
    """

    def _filter(self, mode, hashes):
        from trenchchat.network.prop_filter import PropagationFilter

        class _Cfg:
            channel_filter_mode = mode
            channel_filter_hashes = hashes

        return PropagationFilter(_Cfg())

    def test_allow_all_mode_passes_anything(self):
        assert self._filter("all", []).allows_packed(b"not even a message")

    def test_allowlist_refuses_unreadable_bytes(self):
        """Allowlist means "relay these channels"; bytes we cannot read are
        not one of them."""
        assert not self._filter("allowlist", ["aa" * 16]).allows_packed(b"\x00" * 8)

    def test_allowlist_admits_a_named_channel(self):
        allowed = "aa" * 16

        class _Msg:
            fields = {F_CHANNEL_HASH: bytes.fromhex(allowed)}

        assert self._filter("allowlist", [allowed]).allows(_Msg())

    def test_allowlist_refuses_an_unnamed_channel(self):
        class _Msg:
            fields = {F_CHANNEL_HASH: bytes.fromhex("bb" * 16)}

        assert not self._filter("allowlist", ["aa" * 16]).allows(_Msg())

    def test_the_filter_is_wired_into_the_propagation_ingest(self, peer_factory):
        """The point of the fix: refusing has to keep a message out of the
        store, which only the ingest LXMF actually calls can do."""
        alice = peer_factory("alice")
        alice.config.channel_filter_mode = "allowlist"
        alice.config.set_channel_filter_hashes([])

        calls = []
        alice.router._router.lxmf_propagation = lambda data, *a, **k: calls.append(data)
        alice.router._install_propagation_filter()

        alice.router._router.lxmf_propagation(b"\x00" * 8)
        assert calls == [], "a refused message still reached the propagation store"

        alice.config.channel_filter_mode = "all"
        alice.router._router.lxmf_propagation(b"\x00" * 8)
        assert len(calls) == 1, "an allowed message was not stored"
