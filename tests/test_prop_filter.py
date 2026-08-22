"""Tests for the propagation filter that decides what a node relays."""

from trenchchat.config import Config
from trenchchat.core.protocol import F_CHANNEL_HASH
from trenchchat.network.prop_filter import PropagationFilter


class _Msg:
    def __init__(self, fields):
        self.fields = fields


class TestRelaysNothing:
    def test_default_allowlist_with_no_hashes_relays_nothing(self, tmp_path):
        # allowlist is the default mode; an empty allowlist is the silent
        # no-op state bug 30 warns about.
        config = Config(data_dir=tmp_path)
        assert config.channel_filter_mode == "allowlist"
        assert PropagationFilter(config).relays_nothing() is True

    def test_allowlist_with_a_hash_relays_something(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.add_channel_filter_hash("ab" * 16)
        assert PropagationFilter(config).relays_nothing() is False

    def test_all_mode_never_relays_nothing(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.channel_filter_mode = "all"
        assert PropagationFilter(config).relays_nothing() is False


class TestAllows:
    def test_all_mode_allows_any_message(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.channel_filter_mode = "all"
        assert PropagationFilter(config).allows(_Msg({})) is True

    def test_allowlist_allows_only_listed_channels(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.add_channel_filter_hash("ab" * 16)
        flt = PropagationFilter(config)
        assert flt.allows(_Msg({F_CHANNEL_HASH: bytes.fromhex("ab" * 16)})) is True
        assert flt.allows(_Msg({F_CHANNEL_HASH: bytes.fromhex("cd" * 16)})) is False

    def test_allowlist_rejects_a_message_with_no_channel_hash(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.add_channel_filter_hash("ab" * 16)
        assert PropagationFilter(config).allows(_Msg({})) is False

    def test_allowlist_packed_fails_closed_on_unreadable_bytes(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.add_channel_filter_hash("ab" * 16)
        assert PropagationFilter(config).allows_packed(b"not a real lxmf frame") is False
