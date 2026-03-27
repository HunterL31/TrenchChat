"""
Tests for trenchchat.core.lockbox.

Covers PIN derivation, symmetric encryption/decryption, salt/verify file
management, and wrong-PIN detection.  All tests use tmp_path to avoid
touching the real ~/.trenchchat directory.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from trenchchat.core.lockbox import (
    WrongPinError,
    create_lock,
    decrypt_bytes,
    derive_key,
    encrypt_bytes,
    is_locked,
    remove_lock,
    sqlcipher_hex_key,
    unlock,
    _SALT_PATH,
    _VERIFY_PATH,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_paths(tmp_path: Path):
    """Context manager that redirects lock files to a temp directory."""
    salt = tmp_path / "lock.salt"
    verify = tmp_path / "lock.verify"
    return patch.multiple(
        "trenchchat.core.lockbox",
        _SALT_PATH=salt,
        _VERIFY_PATH=verify,
    )


# ---------------------------------------------------------------------------
# derive_key
# ---------------------------------------------------------------------------

class TestDeriveKey:
    def test_returns_32_bytes(self):
        salt = os.urandom(16)
        key = derive_key("1234", salt)
        assert len(key) == 32

    def test_same_pin_and_salt_gives_same_key(self):
        salt = os.urandom(16)
        k1 = derive_key("1234", salt)
        k2 = derive_key("1234", salt)
        assert k1 == k2

    def test_different_pin_gives_different_key(self):
        salt = os.urandom(16)
        assert derive_key("1234", salt) != derive_key("5678", salt)

    def test_different_salt_gives_different_key(self):
        key1 = derive_key("1234", os.urandom(16))
        key2 = derive_key("1234", os.urandom(16))
        assert key1 != key2


# ---------------------------------------------------------------------------
# encrypt_bytes / decrypt_bytes
# ---------------------------------------------------------------------------

class TestEncryptDecrypt:
    def test_round_trip(self):
        key = os.urandom(32)
        plaintext = b"hello world"
        assert decrypt_bytes(encrypt_bytes(plaintext, key), key) == plaintext

    def test_wrong_key_raises_wrong_pin_error(self):
        key = os.urandom(32)
        ciphertext = encrypt_bytes(b"secret", key)
        wrong_key = os.urandom(32)
        with pytest.raises(WrongPinError):
            decrypt_bytes(ciphertext, wrong_key)

    def test_ciphertext_differs_from_plaintext(self):
        key = os.urandom(32)
        plaintext = b"\x00" * 64
        ciphertext = encrypt_bytes(plaintext, key)
        assert ciphertext != plaintext

    def test_encryption_is_non_deterministic(self):
        """Fernet uses a random IV so encrypting the same plaintext twice differs."""
        key = os.urandom(32)
        plaintext = b"same input"
        assert encrypt_bytes(plaintext, key) != encrypt_bytes(plaintext, key)

    def test_identity_key_round_trip(self):
        """64-byte RNS private key material survives encrypt/decrypt."""
        key = os.urandom(32)
        identity_bytes = os.urandom(64)
        assert decrypt_bytes(encrypt_bytes(identity_bytes, key), key) == identity_bytes


# ---------------------------------------------------------------------------
# sqlcipher_hex_key
# ---------------------------------------------------------------------------

class TestSqlcipherHexKey:
    def test_returns_64_hex_chars_for_32_byte_key(self):
        key = os.urandom(32)
        hex_key = sqlcipher_hex_key(key)
        assert len(hex_key) == 64
        assert all(c in "0123456789abcdef" for c in hex_key)

    def test_deterministic(self):
        key = bytes(range(32))
        assert sqlcipher_hex_key(key) == sqlcipher_hex_key(key)


# ---------------------------------------------------------------------------
# is_locked / create_lock / unlock / remove_lock
# ---------------------------------------------------------------------------

class TestLockLifecycle:
    def test_not_locked_initially(self, tmp_path):
        with _patch_paths(tmp_path):
            assert not is_locked()

    def test_locked_after_create_lock(self, tmp_path):
        with _patch_paths(tmp_path):
            create_lock("1234")
            assert is_locked()

    def test_create_lock_returns_32_byte_key(self, tmp_path):
        with _patch_paths(tmp_path):
            key = create_lock("1234")
            assert len(key) == 32

    def test_unlock_with_correct_pin(self, tmp_path):
        with _patch_paths(tmp_path):
            expected_key = create_lock("5678")
            actual_key = unlock("5678")
            assert actual_key == expected_key

    def test_unlock_with_wrong_pin_raises(self, tmp_path):
        with _patch_paths(tmp_path):
            create_lock("1234")
            with pytest.raises(WrongPinError):
                unlock("9999")

    def test_remove_lock_clears_files(self, tmp_path):
        salt_path = tmp_path / "lock.salt"
        verify_path = tmp_path / "lock.verify"
        with patch.multiple(
            "trenchchat.core.lockbox",
            _SALT_PATH=salt_path,
            _VERIFY_PATH=verify_path,
        ):
            create_lock("1234")
            assert salt_path.exists()
            assert verify_path.exists()
            remove_lock()
            assert not salt_path.exists()
            assert not verify_path.exists()
            assert not is_locked()

    def test_create_lock_raises_if_already_locked(self, tmp_path):
        with _patch_paths(tmp_path):
            create_lock("1234")
            with pytest.raises(ValueError):
                create_lock("5678")

    def test_salt_file_is_owner_only(self, tmp_path):
        """Salt file must be created with 0o600 permissions on POSIX."""
        if os.name == "nt":
            pytest.skip("POSIX permission test not applicable on Windows")
        import stat

        with _patch_paths(tmp_path):
            create_lock("1234")
            salt_path = tmp_path / "lock.salt"
            mode = stat.S_IMODE(os.stat(salt_path).st_mode)
            assert mode == 0o600

    def test_derived_key_matches_after_different_unlock_calls(self, tmp_path):
        """Multiple unlock() calls with the same PIN must always return the same key."""
        with _patch_paths(tmp_path):
            create_lock("4321")
            k1 = unlock("4321")
            k2 = unlock("4321")
            assert k1 == k2
