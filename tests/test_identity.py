"""
Tests for identity file permission enforcement.

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
from trenchchat.core.identity import Identity, _IDENTITY_FILE_MODE, _secure_identity_file


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

        _secure_identity_file(f)

        assert _posix_mode(f) == _IDENTITY_FILE_MODE

    def test_tightens_world_readable_existing_file(self, tmp_path):
        """An existing world-readable identity file is locked down."""
        if os.name == "nt":
            pytest.skip("POSIX permission test not applicable on Windows")

        f = tmp_path / "identity"
        f.write_bytes(b"\x00" * 64)
        os.chmod(f, 0o755)

        _secure_identity_file(f)

        assert _posix_mode(f) == _IDENTITY_FILE_MODE

    def test_already_correct_mode_is_idempotent(self, tmp_path):
        """Calling _secure_identity_file on a file already at 0o600 is a no-op."""
        if os.name == "nt":
            pytest.skip("POSIX permission test not applicable on Windows")

        f = tmp_path / "identity"
        f.write_bytes(b"\x00" * 64)
        os.chmod(f, 0o600)

        _secure_identity_file(f)

        assert _posix_mode(f) == _IDENTITY_FILE_MODE

    def test_oserror_is_logged_not_raised(self, tmp_path):
        """A permission failure must not propagate — it is logged as a warning."""
        f = tmp_path / "identity"
        f.write_bytes(b"\x00" * 64)

        with patch("os.chmod", side_effect=OSError("permission denied")):
            # Must not raise.
            _secure_identity_file(f)


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
        assert _posix_mode(identity_path) == _IDENTITY_FILE_MODE

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

        # _secure_identity_file is the function called by Identity.__init__ on load.
        _secure_identity_file(identity_path)
        assert _posix_mode(identity_path) == _IDENTITY_FILE_MODE
