"""
Author signatures: the canonical digest and the key cache behind it.

The digest is a compatibility contract -- if it drifts, every peer silently
disagrees about what a valid signature looks like -- so it is pinned against a
committed vector here rather than only exercised end to end.
"""

import RNS

from trenchchat.core import authorship
from trenchchat.core.protocol import author_digest
from trenchchat.core.storage import Storage

CH = "ab" * 16
ARGS = (CH, "mid-1", 1000.0, "hello", None, None, None)


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
