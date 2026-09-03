"""
Author signatures: the canonical digest and the key cache behind it.

The digest is a compatibility contract -- if it drifts, every peer silently
disagrees about what a valid signature looks like -- so it is pinned against a
committed vector here rather than only exercised end to end.
"""

import RNS

from trenchchat.core import authorship
from trenchchat.core.protocol import author_digest, file_manifest
from trenchchat.core.storage import Storage

CH = "ab" * 16
ARGS = (CH, "mid-1", 1000.0, "hello", None, None, None)
MANIFEST = file_manifest("notes.txt", 1024, b"\x11" * 32, b"\x22" * 32)


class TestAuthorDigest:
    def test_is_stable_against_a_committed_vector(self):
        """Pinned. A change here is a wire-format change for every peer.

        If this fails, peers on either side of the change disagree about what
        a valid signature covers and reject each other's history -- so update
        it only alongside a deliberate protocol version bump.
        """
        assert author_digest(*ARGS).hex() == (
            "ee6ec20772d56b0c89819b566576fe6cd1a0ebd91c8f9c36a2f43bfc4c8a1fd9"
        )

    def test_every_covered_field_changes_it(self):
        base = author_digest(*ARGS)
        assert author_digest(CH, "mid-1", 1000.0, "hello!", None, None, None) != base
        assert author_digest(CH, "mid-2", 1000.0, "hello", None, None, None) != base
        assert author_digest(CH, "mid-1", 1001.0, "hello", None, None, None) != base
        assert author_digest(CH, "mid-1", 1000.0, "hello", "r", None, None) != base
        assert author_digest(CH, "mid-1", 1000.0, "hello", None, "s", None) != base
        assert author_digest(CH, "mid-1", 1000.0, "hello", None, None, b"x") != base
        assert author_digest("cd" * 16, "mid-1", 1000.0, "hello", None, None, None) != base

    def test_field_boundaries_cannot_be_shifted(self):
        """Length prefixes, not separators: content can contain any byte."""
        a = author_digest(CH, "mid", 1000.0, "ab", "c", None, None)
        b = author_digest(CH, "mid", 1000.0, "a", "bc", None, None)
        assert a != b


class TestFileManifestInTheDigest:
    """A shared file is signed through its manifest, never its bytes."""

    def test_a_message_without_a_manifest_hashes_as_it_always_did(self):
        """Older peers keep verifying every text and image message."""
        assert author_digest(*ARGS, None) == author_digest(*ARGS)

    def test_a_manifest_changes_the_digest(self):
        assert author_digest(*ARGS, MANIFEST) != author_digest(*ARGS)

    def test_every_manifest_field_changes_it(self):
        base = author_digest(*ARGS, MANIFEST)
        variants = [
            file_manifest("other.txt", 1024, b"\x11" * 32, b"\x22" * 32),
            file_manifest("notes.txt", 1025, b"\x11" * 32, b"\x22" * 32),
            file_manifest("notes.txt", 1024, b"\x33" * 32, b"\x22" * 32),
            file_manifest("notes.txt", 1024, b"\x11" * 32, b"\x44" * 32),
        ]
        for variant in variants:
            assert author_digest(*ARGS, variant) != base

    def test_manifest_boundaries_cannot_be_shifted(self):
        """The name and the size are length-prefixed, not run together."""
        a = author_digest(*ARGS, dict(MANIFEST, name="ab", size=1))
        b = author_digest(*ARGS, dict(MANIFEST, name="a", size=11))
        assert a != b

    def test_a_tampered_manifest_fails_verification(self, tmp_path):
        st = Storage(db_path=tmp_path / "keys.db")
        alice = RNS.Identity()
        authorship.remember_identity(st, alice)
        sig = authorship.sign_message(alice, *ARGS, MANIFEST)

        assert authorship.verify_message(st, alice.hash.hex(), sig, *ARGS, MANIFEST)
        for field, value in [("name", "invoice.pdf"), ("size", 2048),
                             ("hash", b"\x99" * 32), ("chunk_root", b"\x99" * 32)]:
            tampered = dict(MANIFEST, **{field: value})
            assert not authorship.verify_message(
                st, alice.hash.hex(), sig, *ARGS, tampered), field
        st.close()

    def test_a_manifest_cannot_be_stripped_from_a_signed_message(self, tmp_path):
        st = Storage(db_path=tmp_path / "keys.db")
        alice = RNS.Identity()
        authorship.remember_identity(st, alice)
        sig = authorship.sign_message(alice, *ARGS, MANIFEST)
        assert not authorship.verify_message(st, alice.hash.hex(), sig, *ARGS)
        st.close()

    def test_a_manifest_cannot_be_added_to_a_signed_message(self, tmp_path):
        st = Storage(db_path=tmp_path / "keys.db")
        alice = RNS.Identity()
        authorship.remember_identity(st, alice)
        sig = authorship.sign_message(alice, *ARGS)
        assert not authorship.verify_message(st, alice.hash.hex(), sig, *ARGS, MANIFEST)
        st.close()


class TestKeyCache:
    def _storage(self, tmp_path):
        return Storage(db_path=tmp_path / "keys.db")

    def test_a_cached_key_verifies_after_the_author_goes_quiet(self, tmp_path):
        st = self._storage(tmp_path)
        alice = RNS.Identity()
        sig = authorship.sign_message(alice, *ARGS)

        authorship.remember_identity(st, alice)
        assert authorship.verify_message(st, alice.hash.hex(), sig, *ARGS)
        st.close()

    def test_a_key_that_does_not_hash_to_its_identity_is_refused(self, tmp_path):
        """Self-certification: this is what makes a key safe from any source."""
        st = self._storage(tmp_path)
        alice, mallory = RNS.Identity(), RNS.Identity()

        st.remember_identity_key(alice.hash.hex(), mallory.get_public_key())
        # The cached key is rejected on read, so a poisoned entry cannot make
        # Mallory's signature verify as Alice's.
        sig = authorship.sign_message(mallory, *ARGS)
        assert not authorship.verify_message(st, alice.hash.hex(), sig, *ARGS)
        st.close()

    def test_an_unknown_author_does_not_verify(self, tmp_path):
        st = self._storage(tmp_path)
        alice = RNS.Identity()
        sig = authorship.sign_message(alice, *ARGS)
        # Never cached and not recallable by hash alone.
        assert not authorship.verify_message(st, "ff" * 16, sig, *ARGS)
        st.close()

    def test_missing_signature_is_not_verified(self, tmp_path):
        st = self._storage(tmp_path)
        alice = RNS.Identity()
        authorship.remember_identity(st, alice)
        assert not authorship.verify_message(st, alice.hash.hex(), None, *ARGS)
        assert not authorship.verify_message(st, alice.hash.hex(), b"", *ARGS)
        st.close()
