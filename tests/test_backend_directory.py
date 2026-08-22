"""
Backend directory seeding from inbound messages (BUG 26b).

devtools/testenv/backend_core.py's _on_inbound_message must record a peer's
display name straight from a chat message's F_DISPLAY_NAME field, and fall
back to seeding a confirmed peer's entry for control messages. These exercise
the method directly with stubs so no Reticulum instance is needed.
"""

import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_TESTENV_DIR = Path(__file__).resolve().parents[1] / "devtools" / "testenv"
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import backend_core
    _HAVE_BACKEND_DEPS = True
except ImportError:  # pragma: no cover - depends on the local install
    _HAVE_BACKEND_DEPS = False

needs_backend = pytest.mark.skipif(
    not _HAVE_BACKEND_DEPS,
    reason="install devtools/testenv/requirements.txt to exercise the backend",
)

SELF_HEX = "a" * 32
PEER = "b" * 32


def _stub_backend(sender_hex=PEER):
    """A minimal stand-in carrying just what the two methods touch."""
    stub = SimpleNamespace()
    stub.presence_mgr = MagicMock()
    stub.presence_mgr.record_inbound.return_value = sender_hex
    stub.user_directory = MagicMock()
    stub.user_directory.contains.return_value = False
    stub.storage = MagicMock()
    stub.storage.get_trenchchat_peer_identities.return_value = set()
    stub.identity = SimpleNamespace(hash_hex=SELF_HEX)
    stub.config = SimpleNamespace(display_name="Tester")
    # _on_inbound_message delegates to this for control/no-name messages; the
    # seeding logic itself is exercised separately against the real method.
    stub._seed_user_directory = MagicMock()
    return stub


def _chat_message(name):
    from trenchchat.core.protocol import F_CHANNEL_HASH, F_DISPLAY_NAME
    return SimpleNamespace(
        fields={F_CHANNEL_HASH: bytes.fromhex("cc" * 16), F_DISPLAY_NAME: name},
        source_hash=b"\x00" * 16,
    )


@needs_backend
class TestInboundNameRecording:
    def test_chat_message_records_display_name(self):
        stub = _stub_backend()
        backend_core.Backend._on_inbound_message(stub, _chat_message("Alice"))
        stub.user_directory.record_user.assert_called_once_with(PEER, "Alice")

    def test_bytes_display_name_is_decoded(self):
        stub = _stub_backend()
        backend_core.Backend._on_inbound_message(stub, _chat_message(b"Bob"))
        stub.user_directory.record_user.assert_called_once_with(PEER, "Bob")

    def test_unresolved_sender_is_ignored(self):
        stub = _stub_backend(sender_hex=None)
        backend_core.Backend._on_inbound_message(stub, _chat_message("Alice"))
        stub.user_directory.record_user.assert_not_called()

    def test_control_message_does_not_record_a_name(self):
        from trenchchat.core.protocol import F_MSG_TYPE, MT_SUBSCRIBE
        stub = _stub_backend()
        msg = SimpleNamespace(
            fields={F_MSG_TYPE: MT_SUBSCRIBE}, source_hash=b"\x00" * 16)
        backend_core.Backend._on_inbound_message(stub, msg)
        # Control messages seed only a confirmed peer; unknown peer records nothing.
        stub.user_directory.record_user.assert_not_called()


@needs_backend
class TestSeedUserDirectory:
    def test_seeds_a_known_directory_peer(self):
        stub = _stub_backend()
        stub.user_directory.contains.return_value = True
        backend_core.Backend._seed_user_directory(stub, PEER)
        stub.user_directory.record_user.assert_called_once()
        assert stub.user_directory.record_user.call_args[0][0] == PEER

    def test_seeds_a_member_table_peer(self):
        stub = _stub_backend()
        stub.storage.get_trenchchat_peer_identities.return_value = {PEER}
        backend_core.Backend._seed_user_directory(stub, PEER)
        stub.user_directory.record_user.assert_called_once()

    def test_unconfirmed_peer_is_not_seeded(self):
        stub = _stub_backend()
        backend_core.Backend._seed_user_directory(stub, PEER)
        stub.user_directory.record_user.assert_not_called()
