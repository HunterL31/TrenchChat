"""
Tests for identity file permission enforcement and PIN encryption.

These tests verify that _secure_identity_file sets owner-only permissions
on both new and pre-existing identity files.  The tests are POSIX-only
for the 0o600 assertion; on Windows the chmod semantics differ and the
test is skipped.
"""

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
import RNS

from trenchchat.config import Config
from trenchchat.core.fileutils import OWNER_RW_MODE, secure_file
from trenchchat.core.identity import Identity
from trenchchat.core.lockbox import WrongPinError, decrypt_bytes, encrypt_bytes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _posix_mode(path: Path) -> int:
    """Return the permission bits (lower 9 bits) of a file."""
    return stat.S_IMODE(os.stat(path).st_mode)


# ---------------------------------------------------------------------------
# _secure_identity_file unit tests (no RNS required)
# ---------------------------------------------------------------------------

class TestSecureIdentityFile:
    def test_sets_owner_only_on_posix(self, tmp_path):
        """On POSIX, the file mode must be exactly 0o600 after securing."""
        if os.name == "nt":
            pytest.skip("POSIX permission test not applicable on Windows")

        f = tmp_path / "identity"
        f.write_bytes(b"\x00" * 64)
        # Start with permissive mode to confirm it gets tightened.
        os.chmod(f, 0o644)

        secure_file(f)

        assert _posix_mode(f) == OWNER_RW_MODE

    def test_tightens_world_readable_existing_file(self, tmp_path):
        """An existing world-readable identity file is locked down."""
        if os.name == "nt":
            pytest.skip("POSIX permission test not applicable on Windows")

        f = tmp_path / "identity"
        f.write_bytes(b"\x00" * 64)
        os.chmod(f, 0o755)

        secure_file(f)

        assert _posix_mode(f) == OWNER_RW_MODE

    def test_already_correct_mode_is_idempotent(self, tmp_path):
        """Calling secure_file on a file already at 0o600 is a no-op."""
        if os.name == "nt":
            pytest.skip("POSIX permission test not applicable on Windows")

        f = tmp_path / "identity"
        f.write_bytes(b"\x00" * 64)
        os.chmod(f, 0o600)

        secure_file(f)

        assert _posix_mode(f) == OWNER_RW_MODE

    def test_oserror_is_logged_not_raised(self, tmp_path):
        """A permission failure must not propagate — it is logged as a warning."""
        f = tmp_path / "identity"
        f.write_bytes(b"\x00" * 64)

        with patch("os.chmod", side_effect=OSError("permission denied")):
            # Must not raise.
            secure_file(f)


# ---------------------------------------------------------------------------
# Identity integration tests (require RNS session fixture)
# ---------------------------------------------------------------------------

class TestIdentityFilePermissions:
    def test_new_identity_file_is_secured(self, rns_instance, tmp_path):
        """A freshly created identity file must have owner-only permissions."""
        if os.name == "nt":
            pytest.skip("POSIX permission test not applicable on Windows")

        identity_path = tmp_path / "identity"
        assert not identity_path.exists()

        config = Config(data_dir=tmp_path)
        Identity(config, identity_path=identity_path)

        assert identity_path.exists()
        assert _posix_mode(identity_path) == OWNER_RW_MODE

    def test_existing_permissive_identity_file_is_hardened(self, rns_instance, tmp_path):
        """An existing identity file with loose permissions is tightened on load.

        We test this by writing a valid raw key file directly (bypassing the
        RNS destination registration that would conflict with the session-scoped
        RNS instance) and then calling _secure_identity_file, which is exactly
        what Identity.__init__ calls on the load path.
        """
        if os.name == "nt":
            pytest.skip("POSIX permission test not applicable on Windows")

        identity_path = tmp_path / "identity"

        # Write a plausible 64-byte key file (the format RNS.Identity.to_file uses).
        identity_path.write_bytes(bytes(64))

        # Simulate a pre-existing install with loose permissions.
        os.chmod(identity_path, 0o644)
        assert _posix_mode(identity_path) == 0o644

        # secure_file is the function called by Identity.__init__ on load.
        secure_file(identity_path)
        assert _posix_mode(identity_path) == OWNER_RW_MODE


# ---------------------------------------------------------------------------
# Identity PIN encryption tests
# ---------------------------------------------------------------------------

class TestIdentityEncryption:
    """Tests for Identity encryption_key support."""

    def test_new_encrypted_identity_file_is_not_plaintext_key(self, rns_instance, tmp_path):
        """When a key is set, the on-disk file must not contain the raw 64-byte key."""
        import os as _os
        key = _os.urandom(32)
        identity_path = tmp_path / "identity_enc"

        config = Config(data_dir=tmp_path)
        identity = Identity(config, identity_path=identity_path, encryption_key=key)

        raw_on_disk = identity_path.read_bytes()
        # Fernet ciphertext is longer than 64 bytes and starts with a version byte.
        assert len(raw_on_disk) > 64
        # The on-disk bytes must not equal the plaintext private key.
        assert raw_on_disk != identity.rns_identity.get_private_key()

    def test_encrypted_identity_survives_round_trip(self, rns_instance, tmp_path):
        """An encrypted identity file decrypts back to the original private key bytes."""
        import os as _os
        key = _os.urandom(32)
        identity_path = tmp_path / "identity_enc_rt"

        config = Config(data_dir=tmp_path)
        id1 = Identity(config, identity_path=identity_path, encryption_key=key)
        original_private_key = id1.rns_identity.get_private_key()

        # Decrypt the on-disk file manually and confirm the bytes match.
        ciphertext = identity_path.read_bytes()
        recovered = decrypt_bytes(ciphertext, key)
        assert recovered == original_private_key

    def test_wrong_key_raises_on_load(self, rns_instance, tmp_path):
        """Loading an encrypted identity with the wrong key raises WrongPinError."""
        import os as _os
        key = _os.urandom(32)
        wrong_key = _os.urandom(32)
        identity_path = tmp_path / "identity_enc"

        config = Config(data_dir=tmp_path)
        Identity(config, identity_path=identity_path, encryption_key=key)

        with pytest.raises(WrongPinError):
            Identity(config, identity_path=identity_path, encryption_key=wrong_key)

    def test_reencrypt_changes_on_disk_bytes(self, rns_instance, tmp_path):
        """reencrypt() with a new key produces different ciphertext on disk."""
        import os as _os
        old_key = _os.urandom(32)
        new_key = _os.urandom(32)
        identity_path = tmp_path / "identity_enc"

        config = Config(data_dir=tmp_path)
        identity = Identity(config, identity_path=identity_path, encryption_key=old_key)
        before = identity_path.read_bytes()

        identity.reencrypt(identity_path, old_key=old_key, new_key=new_key)
        after = identity_path.read_bytes()

        assert before != after

    def test_reencrypt_to_none_produces_plaintext_key(self, rns_instance, tmp_path):
        """reencrypt(new_key=None) strips encryption; the file contains the raw key."""
        import os as _os
        key = _os.urandom(32)
        identity_path = tmp_path / "identity_enc"

        config = Config(data_dir=tmp_path)
        identity = Identity(config, identity_path=identity_path, encryption_key=key)
        private_key_bytes = identity.rns_identity.get_private_key()

        identity.reencrypt(identity_path, old_key=key, new_key=None)

        assert identity_path.read_bytes() == private_key_bytes

    def test_no_key_plain_file_loads_correctly(self, rns_instance, tmp_path):
        """Without an encryption key the file is stored as raw 64-byte key material."""
        identity_path = tmp_path / "identity_plain_nc"
        config = Config(data_dir=tmp_path)

        id1 = Identity(config, identity_path=identity_path)
        # In unencrypted mode the on-disk bytes must equal the raw private key.
        assert identity_path.read_bytes() == id1.rns_identity.get_private_key()
