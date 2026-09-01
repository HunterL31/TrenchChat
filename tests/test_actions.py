"""
Tests for trenchchat.core.actions -- the shared entry points
devtools/testenv/api.py calls, so a caller with no other feedback loop
(an HTTP response, a scripted test) can tell success apart from a
silently-filtered request.
"""

import time

import LXMF
import pytest
import RNS

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
            "outbound_propagation_node": "",
        }

    def test_apply_settings_writes_simple_fields(self, peer_factory):
        alice = peer_factory("alice")

        actions.apply_settings(alice.config, alice.router, {
            "propagation_node_name": "my-node",
            "propagation_storage_limit_mb": 512,
        })

        assert alice.config.propagation_node_name == "my-node"
        assert alice.config.propagation_storage_limit_mb == 512

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

    def test_apply_settings_does_not_choose_the_outbound_node(self, peer_factory):
        """The node offline direct messages go through is read here and
        written elsewhere: PropagationNodes.pin() owns it, because choosing one
        means telling the live router, not only storing a string. A client
        sending the key anyway must be ignored without breaking the rest of the
        update."""
        alice = peer_factory("alice")

        actions.apply_settings(alice.config, alice.router, {
            "outbound_propagation_node": "ab" * 16,
            "propagation_node_name": "still-applied",
        })

        assert alice.config.propagation_node_name == "still-applied"
        assert alice.config.outbound_propagation_node == ""
        assert actions.read_settings(alice.config)["outbound_propagation_node"] == ""

    def test_read_settings_reports_a_pinned_outbound_node(self, peer_factory):
        """What pin() stored is what a client reads back."""
        alice = peer_factory("alice")
        alice.config.outbound_propagation_node = "AB" * 16

        assert (actions.read_settings(alice.config)["outbound_propagation_node"]
                == "ab" * 16)


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


class TestUiThemeLibrary:
    def test_library_defaults_to_empty(self, peer_factory):
        alice = peer_factory("alice")

        assert actions.read_ui_theme_library(alice.config) == {}

    def test_save_then_read_returns_the_theme_under_its_name(self, peer_factory):
        alice = peer_factory("alice")
        theme = {"sidebar": {"bg": "#101010"}, "accent": "#ff8800"}

        actions.save_ui_theme_to_library(alice.config, "midnight", theme)

        assert actions.read_ui_theme_library(alice.config) == {"midnight": theme}

    def test_saving_an_existing_name_overwrites_only_that_entry(self, peer_factory):
        alice = peer_factory("alice")
        actions.save_ui_theme_to_library(alice.config, "midnight", {"accent": "#111111"})
        actions.save_ui_theme_to_library(alice.config, "daylight", {"accent": "#ffffff"})

        actions.save_ui_theme_to_library(alice.config, "midnight", {"accent": "#222222"})

        assert actions.read_ui_theme_library(alice.config) == {
            "midnight": {"accent": "#222222"},
            "daylight": {"accent": "#ffffff"},
        }

    def test_delete_removes_the_theme_and_reports_it(self, peer_factory):
        alice = peer_factory("alice")
        actions.save_ui_theme_to_library(alice.config, "midnight", {"accent": "#111111"})

        assert actions.delete_ui_theme_from_library(alice.config, "midnight") is True
        assert actions.read_ui_theme_library(alice.config) == {}

    def test_deleting_an_unknown_name_returns_false(self, peer_factory):
        alice = peer_factory("alice")

        assert actions.delete_ui_theme_from_library(alice.config, "nothing") is False

    def test_deleting_twice_returns_false_the_second_time(self, peer_factory):
        alice = peer_factory("alice")
        actions.save_ui_theme_to_library(alice.config, "midnight", {"accent": "#111111"})

        assert actions.delete_ui_theme_from_library(alice.config, "midnight") is True
        assert actions.delete_ui_theme_from_library(alice.config, "midnight") is False

    def test_an_empty_name_is_rejected(self, peer_factory):
        alice = peer_factory("alice")

        with pytest.raises(ValueError):
            actions.save_ui_theme_to_library(alice.config, "   ", {"accent": "#111111"})

        assert actions.read_ui_theme_library(alice.config) == {}

    def test_an_oversized_name_is_rejected(self, peer_factory):
        alice = peer_factory("alice")
        too_long = "x" * (actions.MAX_THEME_NAME_LEN + 1)

        with pytest.raises(ValueError):
            actions.save_ui_theme_to_library(alice.config, too_long, {"accent": "#111111"})

        assert actions.read_ui_theme_library(alice.config) == {}

    def test_names_are_stored_stripped(self, peer_factory):
        alice = peer_factory("alice")

        actions.save_ui_theme_to_library(alice.config, "  midnight  ", {"accent": "#111111"})

        assert list(actions.read_ui_theme_library(alice.config)) == ["midnight"]
        assert actions.delete_ui_theme_from_library(alice.config, " midnight ") is True

    def test_library_persists_across_a_fresh_config(self, peer_factory):
        alice = peer_factory("alice")
        theme = {"message_list": {"bg": "#202020", "text": "#eeeeee"}}
        actions.save_ui_theme_to_library(alice.config, "midnight", theme)

        reloaded = Config(data_dir=alice.data_dir)

        assert actions.read_ui_theme_library(reloaded) == {"midnight": theme}

    def test_a_delete_persists_across_a_fresh_config(self, peer_factory):
        alice = peer_factory("alice")
        actions.save_ui_theme_to_library(alice.config, "midnight", {"accent": "#111111"})
        actions.delete_ui_theme_from_library(alice.config, "midnight")

        reloaded = Config(data_dir=alice.data_dir)

        assert actions.read_ui_theme_library(reloaded) == {}


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
# A propagation node cannot select what it relays
# ---------------------------------------------------------------------------

def _propagated_wire_bytes(sender, recipient, channel_hash_hex: str) -> bytes:
    """The exact bytes a propagation node ingests for a message.

    LXMF hands a node `destination_hash + destination.encrypt(rest)`; only the
    first field is in the clear.
    """
    dest = RNS.Destination(
        recipient.identity.rns_identity, RNS.Destination.OUT,
        RNS.Destination.SINGLE, "lxmf", "delivery",
    )
    lxm = LXMF.LXMessage(
        dest, sender.router.delivery_destination, "hello",
        desired_method=LXMF.LXMessage.PROPAGATED,
    )
    lxm.fields = {F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex)}
    lxm.pack()
    header = LXMF.LXMessage.DESTINATION_LENGTH
    return lxm.packed[:header] + dest.encrypt(lxm.packed[header:])


class TestPropagationRelayCannotBeFiltered:
    """A per-channel relay allowlist was offered in Settings for a while. It
    could never have worked, and this is why: the channel field a filter would
    read is inside the part only the recipient can decrypt.
    """

    def test_the_channel_field_is_unreadable_from_the_wire(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        channel_hex = "ab" * 16

        wire = _propagated_wire_bytes(alice, bob, channel_hex)

        assert bytes.fromhex(channel_hex) not in wire
        with pytest.raises(Exception):
            LXMF.LXMessage.unpack_from_bytes(wire)

    def test_enabling_a_node_leaves_the_ingest_unwrapped(self, peer_factory):
        """Nothing may sit in front of lxmf_propagation. It is also the path
        for our own mail arriving from an outbound node, and a refusal there
        drops the message while the node still deletes its copy."""
        alice = peer_factory("alice")

        alice.router.enable_propagation()

        ingest = alice.router.lxmf_router.lxmf_propagation
        assert getattr(ingest, "__func__", None) is LXMF.LXMRouter.lxmf_propagation, \
            f"something is wrapping the propagation ingest: {ingest!r}"


# ---------------------------------------------------------------------------
# set_display_name (BUG 26): a name change must re-announce both destinations
# ---------------------------------------------------------------------------

class _RecordingRouter:
    """Records the calls set_display_name drives, in order."""

    def __init__(self):
        self.calls: list = []

    def set_display_name(self, name):
        self.calls.append(("set_display_name", name))

    def announce(self, attached_interface=None):
        self.calls.append(("announce",))

    def announce_user(self, attached_interface=None):
        self.calls.append(("announce_user",))


def test_set_display_name_reannounces_both_destinations():
    router = _RecordingRouter()
    actions.set_display_name(router, "Zephyr")
    assert router.calls == [
        ("set_display_name", "Zephyr"),
        ("announce",),
        ("announce_user",),
    ]


class _RecordingNodeBrowser:
    """Records which fetch the browse action dispatched."""

    my_node_hash = "ff" * 16

    def __init__(self, fresh_pages=()):
        self.calls = []
        self.fresh = set(fresh_pages)

    def has_fresh_page(self, node_hex, path):
        return (node_hex, path) in self.fresh

    def fetch_page(self, node_hex, path, data=None):
        self.calls.append(("page", node_hex, path, data))
        return "fid1"

    def fetch_file(self, node_hex, path, data=None):
        self.calls.append(("file", node_hex, path, data))
        return "fid2"


def test_browse_nomad_url_dispatches_page_fetch():
    browser = _RecordingNodeBrowser()
    node = "ab" * 16
    result = actions.browse_nomad_url(browser, f"{node}:/page/x.mu")
    assert result == {"fetch_id": "fid1", "node_hash": node,
                      "path": "/page/x.mu", "kind": "page", "cached": False}
    assert browser.calls == [("page", node, "/page/x.mu", None)]


def test_browse_nomad_url_dispatches_file_fetch():
    browser = _RecordingNodeBrowser()
    node = "ab" * 16
    result = actions.browse_nomad_url(browser, f"{node}:/file/data.bin")
    assert result["kind"] == "file"
    assert browser.calls == [("file", node, "/file/data.bin", None)]


def test_browse_nomad_url_resolves_relative_against_current_node():
    browser = _RecordingNodeBrowser()
    node = "cd" * 16
    result = actions.browse_nomad_url(browser, ":/page/x.mu",
                                      current_node_hex=node)
    assert result["node_hash"] == node


def test_browse_nomad_url_serves_a_page_inside_its_declared_lifetime():
    """A page that declared #!c= is answered from cache without asking the
    node again, the way nomadnet's browser does."""
    node = "ab" * 16
    browser = _RecordingNodeBrowser(fresh_pages={(node, "/page/x.mu")})
    result = actions.browse_nomad_url(browser, f"{node}:/page/x.mu")
    assert result["cached"] is True
    assert result["fetch_id"] is None
    assert browser.calls == []


def test_browse_nomad_url_refresh_always_asks_the_node():
    node = "ab" * 16
    browser = _RecordingNodeBrowser(fresh_pages={(node, "/page/x.mu")})
    result = actions.browse_nomad_url(browser, f"{node}:/page/x.mu",
                                      refresh=True)
    assert result["cached"] is False
    assert browser.calls == [("page", node, "/page/x.mu", None)]


def test_browse_nomad_url_submitted_fields_always_ask_the_node():
    """Submitted data is a request, not a re-read: a cached copy cannot be
    the answer to it."""
    node = "ab" * 16
    browser = _RecordingNodeBrowser(fresh_pages={(node, "/page/x.mu")})
    actions.browse_nomad_url(browser, f"{node}:/page/x.mu",
                             request_data={"field_a": "1"})
    assert browser.calls == [("page", node, "/page/x.mu", {"field_a": "1"})]


def test_browse_nomad_url_passes_request_data_through():
    browser = _RecordingNodeBrowser()
    node = "ab" * 16
    actions.browse_nomad_url(browser, f"{node}:/page/x.mu",
                             request_data={"field_a": "1"})
    assert browser.calls == [("page", node, "/page/x.mu", {"field_a": "1"})]


def test_browse_nomad_url_relative_without_current_node_raises():
    with pytest.raises(ValueError):
        actions.browse_nomad_url(_RecordingNodeBrowser(), ":/page/x.mu")


def test_browse_nomad_url_rejects_malformed():
    with pytest.raises(ValueError):
        actions.browse_nomad_url(_RecordingNodeBrowser(), "https://example.com")


def test_set_node_hosting_rejects_blank_name_on_enable():
    with pytest.raises(ValueError):
        actions.set_node_hosting(_RecordingNodeBrowser(), enabled=True,
                                 node_name="   ")


def test_friends_with_pages_decorates_hosting_friends():
    from trenchchat.core.node_browser import nomad_node_hash_for_identity

    hosting_hex = "ab" * 16
    quiet_hex = "cd" * 16
    node_hex = nomad_node_hash_for_identity(hosting_hex)

    class _Friends:
        def get_friends(self):
            return [{"identity_hash": hosting_hex},
                    {"identity_hash": quiet_hex}]

    class _Browser:
        def node_for_identity(self, identity_hex):
            if identity_hex == hosting_hex:
                return {"node_hash": node_hex}
            return None

    decorated = actions.friends_with_pages(_Friends(), _Browser())
    assert decorated[0]["nomad_node_hash"] == node_hex
    assert decorated[1]["nomad_node_hash"] is None


class TestAudioDevices:
    class _Config:
        def __init__(self):
            self.voice_input_device = None
            self.voice_output_device = None

    class _VoiceMgr:
        def __init__(self):
            self.restarts = 0

        def restart_audio(self):
            self.restarts += 1

    def test_list_audio_devices_carries_the_selection(self, monkeypatch):
        from trenchchat.core.audio import devices
        monkeypatch.setattr(devices, "list_devices", lambda: {
            "available": True, "reason": "",
            "input": ["Mic A"], "output": ["Out B"],
        })
        config = self._Config()
        config.voice_input_device = "Mic A"

        info = actions.list_audio_devices(config)
        assert info["input"] == ["Mic A"]
        assert info["selected"] == {"input": "Mic A", "output": None}

    def test_set_audio_devices_persists_and_restarts(self):
        config, voice_mgr = self._Config(), self._VoiceMgr()

        changed = actions.set_audio_devices(config, voice_mgr,
                                            "Mic A", None)
        assert changed is True
        assert config.voice_input_device == "Mic A"
        assert config.voice_output_device is None
        assert voice_mgr.restarts == 1

    def test_set_audio_devices_same_choice_is_a_noop(self):
        config, voice_mgr = self._Config(), self._VoiceMgr()
        config.voice_input_device = "Mic A"

        changed = actions.set_audio_devices(config, voice_mgr,
                                            "Mic A", None)
        assert changed is False
        assert voice_mgr.restarts == 0, \
            "an unchanged selection rebuilt a live pipeline"


class TestSharedChannelPeers:
    """actions.shared_channel_peers backs GET /directory?scope=shared."""

    @pytest.fixture
    def db(self, tmp_path):
        from trenchchat.core.storage import Storage
        s = Storage(db_path=tmp_path / "shared.db")
        yield s
        s.close()

    SELF = "aa" * 16
    MEMBER = "bb" * 16
    SUBSCRIBER = "cc" * 16
    STRANGER = "dd" * 16

    def _channel(self, db, hash_hex, perms=PRESET_PRIVATE, subscribe=True):
        db.upsert_channel(hash_hex, "ch", "", self.SELF, dict(perms), time.time())
        if subscribe:
            db.subscribe(hash_hex)

    def test_unions_members_and_durable_subscribers(self, db):
        self._channel(db, "11" * 16)
        self._channel(db, "22" * 16, perms=PRESET_OPEN)
        db.upsert_member("11" * 16, self.MEMBER, "Bob")
        db.upsert_member("11" * 16, self.SELF, "Me")
        db.add_channel_subscriber("22" * 16, self.SUBSCRIBER)

        assert actions.shared_channel_peers(db, self.SELF) == {
            self.MEMBER, self.SUBSCRIBER,
        }

    def test_excludes_self_and_unshared_channels(self, db):
        self._channel(db, "11" * 16)
        db.upsert_member("11" * 16, self.SELF, "Me")
        self._channel(db, "33" * 16, subscribe=False)
        db.upsert_member("33" * 16, self.STRANGER, "Nope")
        db.add_channel_subscriber("33" * 16, self.STRANGER)

        assert actions.shared_channel_peers(db, self.SELF) == set()

    def test_no_channels_is_empty(self, db):
        assert actions.shared_channel_peers(db, self.SELF) == set()


class TestFilterDirectoryScope:
    RESULTS = [
        {"identity_hash": "aa" * 16, "display_name": "Friend"},
        {"identity_hash": "bb" * 16, "display_name": "Shared"},
        {"identity_hash": "cc" * 16, "display_name": "Stranger"},
    ]
    FRIENDS = {"aa" * 16}
    SHARED = {"bb" * 16}

    def _filter(self, scope):
        return actions.filter_directory_scope(
            list(self.RESULTS), scope,
            friend_hashes=self.FRIENDS, shared_hashes=self.SHARED,
        )

    def test_friends_scope_keeps_only_friends(self):
        assert [r["display_name"] for r in self._filter("friends")] == ["Friend"]

    def test_shared_scope_keeps_only_shared_channel_peers(self):
        assert [r["display_name"] for r in self._filter("shared")] == ["Shared"]

    def test_all_and_unknown_scopes_pass_everything_through(self):
        assert len(self._filter("all")) == 3
        assert len(self._filter("bogus")) == 3
