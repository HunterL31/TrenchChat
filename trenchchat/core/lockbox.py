"""
PIN-based lock for TrenchChat sensitive data.

Provides key derivation from a PIN and symmetric encryption helpers used to
protect the identity file and SQLite database at rest.  All cryptographic
material (salt, verification token) is stored in the TrenchChat data directory.

Usage pattern
-------------
First launch (no PIN set)::

    is_locked()  # -> False
    # user chooses to set a PIN via the Settings dialog
    key = create_lock(pin)
    # caller must then re-encrypt identity and re-key the database

Subsequent launches::

    is_locked()  # -> True
    key = unlock(pin)   # raises WrongPinError on bad PIN
    # caller passes key to Identity and Storage constructors

Removing a PIN::

    key = unlock(current_pin)
    remove_lock(current_pin)
    # caller must decrypt identity and export the database to plaintext
"""

import base64
import hashlib
import os
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
import RNS

from trenchchat.config import DATA_DIR
from trenchchat.core.fileutils import atomic_write_bytes, secure_file

# Files managed by this module.
_SALT_PATH = DATA_DIR / "lock.salt"
_VERIFY_PATH = DATA_DIR / "lock.verify"

# Minimum accepted PIN length. Enforced here so headless/non-Qt callers get the
# same floor the Qt pin dialog applies.
MIN_PIN_LENGTH = 4

# Consecutive wrong guesses before a cooldown, and the cooldown length. Mirrors
# the Qt dialog's limits so the core layer protects every caller, not just it.
MAX_UNLOCK_ATTEMPTS = 5
LOCKOUT_SECS = 30

# Throttle state, guarded by _throttle_lock. A monotonic clock is used so
# moving the wall clock back cannot shorten a lockout.
_throttle_lock = threading.Lock()
_failed_attempts = 0
_lockout_until = 0.0

# PBKDF2 iteration count.  NIST SP 800-132 recommends ≥ 210 000 for SHA-256
# in 2023; 600 000 provides comfortable headroom on modern hardware while
# remaining fast enough for a human-initiated unlock (< 1 s on a typical PC).
PBKDF2_ITERATIONS = 600_000

# Known sentinel encrypted to prove PIN correctness without touching the
# identity file.  Value is intentionally generic.
_VERIFY_SENTINEL = b"trenchchat-lock-verify-v1"


class WrongPinError(Exception):
    """Raised when an incorrect PIN is supplied to unlock()."""


class LockedOutError(WrongPinError):
    """Raised when unlock() is called during a post-failure cooldown.

    Subclasses WrongPinError so existing ``except WrongPinError`` handlers
    still treat a locked-out attempt as a failed unlock.
    """


def _register_failure() -> None:
    """Count a failed unlock and start a cooldown once the limit is hit."""
    global _failed_attempts, _lockout_until
    with _throttle_lock:
        _failed_attempts += 1
        if _failed_attempts >= MAX_UNLOCK_ATTEMPTS:
            _lockout_until = time.monotonic() + LOCKOUT_SECS
            _failed_attempts = 0
            RNS.log(
                f"TrenchChat [lockbox]: too many failed unlock attempts; "
                f"locked out for {LOCKOUT_SECS}s",
                RNS.LOG_WARNING,
            )


def _reset_throttle() -> None:
    """Clear failed-attempt and lockout state (successful unlock or new lock)."""
    global _failed_attempts, _lockout_until
    with _throttle_lock:
        _failed_attempts = 0
        _lockout_until = 0.0


def derive_key(pin: str, salt: bytes) -> bytes:
    """Derive a 32-byte key from a PIN and salt using PBKDF2-HMAC-SHA256.

    The returned bytes are suitable for use as a Fernet key after URL-safe
    base64 encoding, or as a raw hex key for SQLCipher.
    """
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PBKDF2_ITERATIONS)


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

    Raises ValueError if a lock is already set — call remove_lock first — or
    if the PIN is shorter than MIN_PIN_LENGTH.
    """
    if len(pin) < MIN_PIN_LENGTH:
        raise ValueError(f"PIN must be at least {MIN_PIN_LENGTH} characters")
    if is_locked():
        raise ValueError("A PIN lock is already set; remove it before creating a new one")

    _SALT_PATH.parent.mkdir(parents=True, exist_ok=True)

    salt = os.urandom(16)
    raw_key = derive_key(pin, salt)

    atomic_write_bytes(_SALT_PATH, salt)

    token = _make_fernet(raw_key).encrypt(_VERIFY_SENTINEL)
    atomic_write_bytes(_VERIFY_PATH, token)

    _reset_throttle()
    RNS.log("TrenchChat [lockbox]: PIN lock created", RNS.LOG_NOTICE)
    return raw_key


def unlock(pin: str) -> bytes:
    """Derive the key from a PIN and verify it against the stored token.

    Returns the 32-byte raw key on success.
    Raises WrongPinError if the PIN is incorrect, too short, the verify token
    is missing (a partial-lock state), or an unlock cooldown is in effect
    (LockedOutError, a WrongPinError subclass).
    Raises FileNotFoundError if no lock has been set at all.
    """
    if len(pin) < MIN_PIN_LENGTH:
        raise WrongPinError(f"PIN must be at least {MIN_PIN_LENGTH} characters")

    with _throttle_lock:
        remaining = _lockout_until - time.monotonic()
    if remaining > 0:
        raise LockedOutError(
            f"Too many attempts; wait {int(remaining) + 1}s"
        )

    salt = _SALT_PATH.read_bytes()
    raw_key = derive_key(pin, salt)

    try:
        token = _VERIFY_PATH.read_bytes()
    except FileNotFoundError as exc:
        # Salt present but verify token missing: a partial-lock state, not a
        # "never locked" one. Treat it as a failed unlock instead of letting
        # FileNotFoundError escape and crash the caller.
        _register_failure()
        raise WrongPinError(
            "Lock state incomplete: verification token missing"
        ) from exc

    try:
        _make_fernet(raw_key).decrypt(token)
    except InvalidToken as exc:
        _register_failure()
        raise WrongPinError("Incorrect PIN") from exc

    _reset_throttle()
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
    _reset_throttle()
    RNS.log("TrenchChat [lockbox]: PIN lock removed", RNS.LOG_NOTICE)
