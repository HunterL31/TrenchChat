"""
Passphrase-based lock for TrenchChat sensitive data.

Provides key derivation from a passphrase and symmetric encryption helpers
used to protect the identity file and SQLite database at rest.  All
cryptographic material (salt, verification token) is stored in the TrenchChat
data directory.

What this protects, and what it does not
----------------------------------------
The lock protects data at rest on this computer.  Anyone who copies the files
in the data directory can guess passphrases offline at their own pace, with no
rate limit -- the stored verification token and the salt are enough on their
own.  Passphrase length is what makes that guessing infeasible, which is why
the dialogs require a real passphrase rather than a short numeric PIN.

Usage pattern
-------------
First launch (no passphrase set)::

    is_locked()  # -> False
    # user chooses to set a passphrase via the Settings dialog
    key = create_lock(passphrase)
    # caller must then re-encrypt identity and re-key the database

Subsequent launches::

    is_locked()  # -> True
    key = unlock(passphrase)   # raises WrongPinError on a bad passphrase
    # caller passes key to Identity and Storage constructors

Removing a passphrase::

    key = unlock(current_passphrase)
    remove_lock()
    # caller must decrypt identity and export the database to plaintext
"""

import base64
import hashlib
import os
import unicodedata
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
import RNS

from trenchchat.config import DATA_DIR
from trenchchat.core.fileutils import atomic_write_bytes, secure_file

# Files managed by this module.
_SALT_PATH = DATA_DIR / "lock.salt"
_VERIFY_PATH = DATA_DIR / "lock.verify"

# PBKDF2 iteration count.  NIST SP 800-132 recommends ≥ 210 000 for SHA-256
# in 2023; 600 000 provides comfortable headroom on modern hardware while
# remaining fast enough for a human-initiated unlock (< 1 s on a typical PC).
PBKDF2_ITERATIONS = 600_000

# Known sentinel encrypted to prove PIN correctness without touching the
# identity file.  Value is intentionally generic.
_VERIFY_SENTINEL = b"trenchchat-lock-verify-v1"


class WrongPinError(Exception):
    """Raised when an incorrect PIN is supplied to unlock()."""


def derive_key(pin: str, salt: bytes) -> bytes:
    """Derive a 32-byte key from a passphrase and salt using PBKDF2-HMAC-SHA256.

    The returned bytes are suitable for use as a Fernet key after URL-safe
    base64 encoding, or as a raw hex key for SQLCipher.

    The passphrase is NFKC-normalised so two Unicode spellings of the same
    text derive the same key. ASCII is unaffected, so locks created before
    passphrases were allowed still derive identically.
    """
    normalised = unicodedata.normalize("NFKC", pin)
    return hashlib.pbkdf2_hmac("sha256", normalised.encode(), salt,
                               PBKDF2_ITERATIONS)


def _make_fernet(raw_key: bytes) -> Fernet:
    """Build a Fernet cipher from a 32-byte raw key."""
    return Fernet(base64.urlsafe_b64encode(raw_key))


def encrypt_bytes(plaintext: bytes, raw_key: bytes) -> bytes:
    """Fernet-encrypt arbitrary bytes with the given 32-byte raw key."""
    return _make_fernet(raw_key).encrypt(plaintext)


def decrypt_bytes(ciphertext: bytes, raw_key: bytes) -> bytes:
    """Fernet-decrypt bytes.

    Raises WrongPinError if the key is incorrect or the ciphertext is
    corrupt.
    """
    try:
        return _make_fernet(raw_key).decrypt(ciphertext)
    except InvalidToken as exc:
        raise WrongPinError("Incorrect PIN or corrupt ciphertext") from exc


def sqlcipher_hex_key(raw_key: bytes) -> str:
    """Return the hex-encoded key string expected by SQLCipher's PRAGMA key.

    SQLCipher accepts ``PRAGMA key = "x'<64-hex-chars>'"`` when the raw key
    is exactly 32 bytes.
    """
    return raw_key.hex()


def is_locked() -> bool:
    """Return True if a PIN lock has been set (salt file is present)."""
    return _SALT_PATH.exists()


def create_lock(pin: str) -> bytes:
    """Set a new PIN lock.

    Generates a fresh random salt, derives the encryption key, and writes
    the salt and a verification token to disk.  Returns the 32-byte raw key
    so the caller can immediately use it without prompting again.

    Raises ValueError if a lock is already set — call remove_lock first.
    """
    if is_locked():
        raise ValueError("A PIN lock is already set; remove it before creating a new one")

    _SALT_PATH.parent.mkdir(parents=True, exist_ok=True)

    salt = os.urandom(16)
    raw_key = derive_key(pin, salt)

    atomic_write_bytes(_SALT_PATH, salt)

    token = _make_fernet(raw_key).encrypt(_VERIFY_SENTINEL)
    atomic_write_bytes(_VERIFY_PATH, token)

    RNS.log("TrenchChat [lockbox]: PIN lock created", RNS.LOG_NOTICE)
    return raw_key


def unlock(pin: str) -> bytes:
    """Derive the key from a PIN and verify it against the stored token.

    Returns the 32-byte raw key on success.
    Raises WrongPinError if the PIN is incorrect.
    Raises FileNotFoundError if no lock has been set.
    """
    salt = _SALT_PATH.read_bytes()
    raw_key = derive_key(pin, salt)

    token = _VERIFY_PATH.read_bytes()
    try:
        _make_fernet(raw_key).decrypt(token)
    except InvalidToken as exc:
        raise WrongPinError("Incorrect PIN") from exc

    RNS.log("TrenchChat [lockbox]: unlocked successfully", RNS.LOG_DEBUG)
    return raw_key


def remove_lock() -> None:
    """Delete the salt and verification token, disabling the PIN lock.

    The caller is responsible for decrypting the identity file and exporting
    the database to plaintext **before** calling this, so that the files
    remain accessible.
    """
    for path in (_SALT_PATH, _VERIFY_PATH):
        if path.exists():
            path.unlink()
    RNS.log("TrenchChat [lockbox]: PIN lock removed", RNS.LOG_NOTICE)
