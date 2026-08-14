"""
Tests for trenchchat.service.backend's pure logic.

ServiceBackend.__init__ touches real global state (a real RNS.Reticulum
instance, the user's actual ~/.trenchchat and ~/.reticulum) by design --
that's the point of Task 4 in the service-api-extraction plan, mirroring
main.py rather than the dev harness's isolated per-tester config. It is
deliberately not constructed here; only its standalone helper is covered.
"""

from unittest.mock import patch

import pytest

from trenchchat.core import lockbox
from trenchchat.service.backend import PIN_ENV_VAR, _resolve_encryption_key


class TestResolveEncryptionKey:
    def test_explicit_key_wins_even_when_unlocked(self, monkeypatch):
        monkeypatch.setattr(lockbox, "is_locked", lambda: False)
        key = _resolve_encryption_key(b"x" * 32)
        assert key == b"x" * 32

    def test_explicit_key_wins_even_when_locked(self, monkeypatch):
        monkeypatch.setattr(lockbox, "is_locked", lambda: True)
        key = _resolve_encryption_key(b"x" * 32)
        assert key == b"x" * 32

    def test_no_key_no_lock_returns_none(self, monkeypatch):
        monkeypatch.setattr(lockbox, "is_locked", lambda: False)
        monkeypatch.delenv(PIN_ENV_VAR, raising=False)
        assert _resolve_encryption_key(None) is None

    def test_locked_with_no_pin_env_var_raises(self, monkeypatch):
        monkeypatch.setattr(lockbox, "is_locked", lambda: True)
        monkeypatch.delenv(PIN_ENV_VAR, raising=False)
        with pytest.raises(RuntimeError):
            _resolve_encryption_key(None)

    def test_locked_with_pin_env_var_derives_key_via_lockbox_unlock(self, monkeypatch):
        monkeypatch.setattr(lockbox, "is_locked", lambda: True)
        monkeypatch.setenv(PIN_ENV_VAR, "1234")
        with patch.object(lockbox, "unlock", return_value=b"y" * 32) as mock_unlock:
            key = _resolve_encryption_key(None)
        mock_unlock.assert_called_once_with("1234")
        assert key == b"y" * 32

    def test_wrong_pin_propagates(self, monkeypatch):
        monkeypatch.setattr(lockbox, "is_locked", lambda: True)
        monkeypatch.setenv(PIN_ENV_VAR, "0000")
        with patch.object(lockbox, "unlock", side_effect=lockbox.WrongPinError()):
            with pytest.raises(lockbox.WrongPinError):
                _resolve_encryption_key(None)
