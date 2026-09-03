"""
The file manifest: chunk hashing, the chunk root, and what counts as a valid
manifest.

A manifest is the whole of what a peer learns about a shared file until it
asks for the bytes, and every part of it is asserted by the sender, so the
shapes here are a wire contract: the chunk root is what the author's
signature reaches every chunk through, and the validator is what stands
between an inbound field dict and the store.
"""

import hashlib

from trenchchat.core.fileutils import clean_filename
from trenchchat.core.protocol import (
    FILE_CHUNK_BYTES, MAX_FILE_NAME_CHARS, MAX_SHARED_FILE_BYTES,
    chunk_hashes, chunk_root, file_manifest,
)

HASH = b"\x11" * 32
ROOT = b"\x22" * 32


class TestChunkHashes:
    def test_a_file_smaller_than_a_chunk_is_one_chunk(self):
        assert chunk_hashes(b"trenchchat") == [hashlib.sha256(b"trenchchat").digest()]

    def test_an_empty_file_has_no_chunks(self):
        assert chunk_hashes(b"") == []

    def test_an_exact_multiple_has_no_trailing_empty_chunk(self):
        data = b"x" * (2 * FILE_CHUNK_BYTES)
        hashes = chunk_hashes(data)
        assert len(hashes) == 2
        assert hashes[0] == hashlib.sha256(data[:FILE_CHUNK_BYTES]).digest()
        assert hashes[1] == hashlib.sha256(data[FILE_CHUNK_BYTES:]).digest()

    def test_one_byte_past_a_multiple_adds_a_short_chunk(self):
        data = b"x" * (2 * FILE_CHUNK_BYTES) + b"y"
        hashes = chunk_hashes(data)
        assert len(hashes) == 3
        assert hashes[2] == hashlib.sha256(b"y").digest()


class TestChunkRoot:
    def test_is_stable_against_a_committed_vector(self):
        """Pinned. The root is signed, so peers must agree on it exactly."""
        assert chunk_root(chunk_hashes(b"trenchchat")).hex() == (
            "ba4861592da7d08b87cb6b2e5ae2b9bc726684ab580fb2445675d12ab4f1fc20"
        )

    def test_an_empty_list_roots_to_the_empty_digest(self):
        assert chunk_root([]) == hashlib.sha256(b"").digest()

    def test_a_changed_chunk_changes_the_root(self):
        data = b"x" * (2 * FILE_CHUNK_BYTES)
        spoiled = b"y" + data[1:]
        assert chunk_root(chunk_hashes(spoiled)) != chunk_root(chunk_hashes(data))

    def test_chunk_order_is_covered(self):
        hashes = chunk_hashes(b"x" * (2 * FILE_CHUNK_BYTES) + b"y")
        assert chunk_root(list(reversed(hashes))) != chunk_root(hashes)


class TestFileManifest:
    def test_a_well_formed_manifest_is_normalised(self):
        assert file_manifest("notes.txt", 10, HASH, ROOT) == {
            "name": "notes.txt", "size": 10, "hash": HASH, "chunk_root": ROOT,
        }

    def test_a_file_at_the_ceiling_is_accepted(self):
        assert file_manifest("big.bin", MAX_SHARED_FILE_BYTES, HASH, ROOT) is not None

    def test_an_oversized_file_is_refused(self):
        assert file_manifest("big.bin", MAX_SHARED_FILE_BYTES + 1, HASH, ROOT) is None

    def test_an_empty_file_is_refused(self):
        assert file_manifest("empty.bin", 0, HASH, ROOT) is None
        assert file_manifest("negative.bin", -1, HASH, ROOT) is None

    def test_a_size_that_is_not_an_integer_is_refused(self):
        assert file_manifest("notes.txt", 10.0, HASH, ROOT) is None
        assert file_manifest("notes.txt", "10", HASH, ROOT) is None
        assert file_manifest("notes.txt", True, HASH, ROOT) is None

    def test_a_digest_of_the_wrong_length_is_refused(self):
        assert file_manifest("notes.txt", 10, b"\x11" * 31, ROOT) is None
        assert file_manifest("notes.txt", 10, HASH, b"\x22" * 33) is None
        assert file_manifest("notes.txt", 10, HASH.hex(), ROOT) is None
        assert file_manifest("notes.txt", 10, HASH, None) is None

    def test_a_name_that_is_not_a_string_is_refused(self):
        """Wire fields may arrive as bytes; the caller coerces, not this."""
        assert file_manifest(b"notes.txt", 10, HASH, ROOT) is None
        assert file_manifest(None, 10, HASH, ROOT) is None
        assert file_manifest(12345, 10, HASH, ROOT) is None

    def test_a_name_that_needs_cleaning_is_refused(self):
        for name in ["../../etc/passwd", "a/b.txt", "back\\slash.txt",
                     're"port.txt', "trailing ", " leading", "dotted.",
                     "line\nbreak.txt", "", "...",
                     "x" * (MAX_FILE_NAME_CHARS + 1)]:
            assert file_manifest(name, 10, HASH, ROOT) is None, name

    def test_a_cleaned_name_is_accepted(self):
        """What clean_filename produces is what the validator lets through."""
        for given in [b"notes.txt", "../../etc/passwd", 're"port.txt',
                      "x" * (MAX_FILE_NAME_CHARS + 40)]:
            cleaned = clean_filename(given)
            assert file_manifest(cleaned, 10, HASH, ROOT) is not None, given
