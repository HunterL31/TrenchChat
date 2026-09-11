"""
Where a shared file's bytes live: on disk beside the database, one file each.

A share is up to MAX_SHARED_FILE_BYTES, and that ceiling is now large enough
that holding the bytes as database rows is the wrong shape: every chunk written
goes through the journal, a prune rewrites pages rather than unlinking, and a
vacuum has to move megabytes to give the space back. On disk a file is one
sparse file, a chunk is a seek and a read of exactly that chunk, and dropping
one is an unlink.

What the database keeps is the bookkeeping: which files exist, which chunks of
each are held, what they cost against the store budgets. Nothing here reads or
writes any of that; storage.py owns it and calls in here for the bytes.

Encryption follows the database. A profile with no PIN keeps its database in
the clear, so the bytes beside it are in the clear too. A profile with a PIN
keeps its database under SQLCipher, so every chunk here is sealed with
AES-256-GCM under a key derived from the same lockbox key, with the file hash
and the chunk index as associated data: a sealed chunk cannot be moved to
another index or another file, and a store carried off without the PIN is
noise. Setting or removing the PIN re-seals the store the way it re-encrypts
the identity file and re-keys the database.
"""

import hashlib
import os
import struct
from pathlib import Path

import RNS
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from trenchchat.core.fileutils import secure_file
from trenchchat.core.protocol import FILE_CHUNK_BYTES

# Nonce and tag around one sealed chunk, which is what makes a sealed record
# longer than the chunk in it and fixes the stride a seek counts in.
NONCE_BYTES = 12
TAG_BYTES = 16
SEAL_OVERHEAD = NONCE_BYTES + TAG_BYTES

# The lockbox key already keys SQLCipher, so the store derives its own rather
# than using the same bytes for a second cipher.
_KEY_DOMAIN = b"trenchchat-file-store-v1"

# Two hex characters of the hash, so a store holding thousands of files is not
# one directory holding thousands of entries.
_FANOUT_CHARS = 2


def derive_store_key(raw_key: bytes) -> bytes:
    """The AEAD key for a store, from the lockbox key that also keys the database."""
    return hashlib.sha256(_KEY_DOMAIN + raw_key).digest()


class FileStoreError(OSError):
    """A stored chunk could not be read back as one."""


class FileStore:
    """The bytes of every shared file this node holds."""

    def __init__(self, root: Path, raw_key: bytes | None = None):
        """
        root: the directory the files live under, created on first write.
        raw_key: the lockbox key when the profile has a PIN, else None, which
        stores chunks in the clear exactly as an unlocked database does.
        """
        self._root = Path(root)
        self._aead = (AESGCM(derive_store_key(raw_key))
                      if raw_key is not None else None)

    @property
    def root(self) -> Path:
        """The directory the store writes under."""
        return self._root

    @property
    def sealed(self) -> bool:
        """Whether chunks are encrypted at rest."""
        return self._aead is not None

    def path_for(self, hash_hex: str) -> Path:
        """Where one file's bytes live."""
        return self._root / hash_hex[:_FANOUT_CHARS] / hash_hex

    @property
    def stride(self) -> int:
        """The distance between one chunk's record and the next."""
        return FILE_CHUNK_BYTES + (SEAL_OVERHEAD if self.sealed else 0)

    def seal(self, hash_hex: str, idx: int, content: bytes) -> bytes:
        """One chunk as it is written: sealed, or the bytes themselves."""
        if self._aead is None:
            return content
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, content, self._aad(hash_hex, idx))

    @staticmethod
    def _aad(hash_hex: str, idx: int) -> bytes:
        """What a sealed chunk is bound to: its file and its place in it."""
        return hash_hex.encode("ascii") + struct.pack("!I", idx)

    def put(self, hash_hex: str, idx: int, content: bytes) -> None:
        """Write one chunk in its own slot, sealing it when the store is sealed.

        Every chunk gets a slot a whole chunk wide whatever it holds, so where
        one lives is its index and nothing else, and a download that arrives
        out of order writes each chunk exactly where it belongs. A short chunk
        leaves the rest of its slot a hole, which costs no disk.

        Raises OSError when the disk will not take it, which is the caller's
        cue to stop the download and keep what it already holds.
        """
        record = self.seal(hash_hex, idx, content)
        path = self.path_for(hash_hex)
        path.parent.mkdir(parents=True, exist_ok=True)
        created = not path.exists()
        with open(path, "r+b" if not created else "w+b") as handle:
            handle.seek(idx * self.stride)
            handle.write(record)
        if created:
            secure_file(path)

    def get(self, hash_hex: str, idx: int, length: int) -> bytes | None:
        """One chunk, read from its own offset. None when it is not there.

        *length* is the plaintext length of that chunk, which the bookkeeping
        records when it arrives: a hole reads as zeros, so the store cannot
        work out on its own where a chunk ends.
        """
        record_length = length + (SEAL_OVERHEAD if self.sealed else 0)
        try:
            with open(self.path_for(hash_hex), "rb") as handle:
                handle.seek(idx * self.stride)
                record = handle.read(record_length)
        except FileNotFoundError:
            return None
        if len(record) != record_length:
            return None
        if self._aead is None:
            return record
        try:
            return self._aead.decrypt(record[:NONCE_BYTES], record[NONCE_BYTES:],
                                      self._aad(hash_hex, idx))
        except InvalidTag as e:
            raise FileStoreError(
                f"chunk {idx} of {hash_hex[:12]} does not decrypt") from e

    def delete(self, hash_hex: str) -> None:
        """Drop one file's bytes. Silent when there are none."""
        path = self.path_for(hash_hex)
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            RNS.log(f"TrenchChat [filestore]: could not remove "
                    f"{hash_hex[:12]}…: {e}", RNS.LOG_WARNING)

    def hashes(self) -> list[str]:
        """Every file the store holds bytes for."""
        if not self._root.is_dir():
            return []
        found: list[str] = []
        for bucket in self._root.iterdir():
            if not bucket.is_dir():
                continue
            found.extend(entry.name for entry in bucket.iterdir()
                         if entry.is_file())
        return found

    def purge_except(self, keep: set[str]) -> int:
        """Remove bytes no bookkeeping row accounts for. Returns how many.

        A crash between writing a chunk and committing its row leaves a file
        nothing will ever read or evict; this is what collects it.
        """
        dropped = 0
        for hash_hex in self.hashes():
            if hash_hex in keep:
                continue
            self.delete(hash_hex)
            dropped += 1
        return dropped

    def rekey(self, new_raw_key: bytes | None, entries) -> None:
        """Re-seal every chunk under a new key, or unseal them all.

        *entries* is (hash_hex, [(index, length), ...]) per stored file,
        because only the bookkeeping knows which chunks are there and how long
        each is; a hole reads as zeros and is indistinguishable from bytes.

        Each file is rewritten beside itself and moved into place, so a file is
        either wholly under the old key or wholly under the new one and an
        interrupted change costs at most the file it was working on.
        """
        target = FileStore(self._root, new_raw_key)
        for hash_hex, chunks in entries:
            self._rewrite(hash_hex, chunks, target)
        self._aead = target._aead

    def _rewrite(self, hash_hex: str, chunks, target: "FileStore") -> None:
        """Read one file under this key and write it back under the target's."""
        path = self.path_for(hash_hex)
        if not path.exists():
            return
        temporary = path.with_name(path.name + ".rekey")
        try:
            with open(temporary, "w+b") as out:
                for idx, length in chunks:
                    chunk = self.get(hash_hex, idx, length)
                    if chunk is None:
                        continue
                    out.seek(idx * target.stride)
                    out.write(target.seal(hash_hex, idx, chunk))
            secure_file(temporary)
            os.replace(temporary, path)
        except OSError as e:
            RNS.log(f"TrenchChat [filestore]: could not re-key "
                    f"{hash_hex[:12]}…: {e}", RNS.LOG_ERROR)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
