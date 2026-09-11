"""
Per-message author signatures.

A synced message reaches you from a peer who usually did not write it. LXMF
authenticates the peer that handed it over and nothing else, so on that path
the author, the text, the attachment and the threading fields are all just
claims in the relay's payload. These helpers bind a message to its author's
key at send time so any relayed copy can be checked independently of whoever
relayed it.

Verification needs the author's public key, not their identity hash -- a hash
is one-way. Keys are cached locally as they are learned, and every cached key
is checked to hash back to the identity claiming it, which is what makes a key
safe to accept from any source at all. A router, where callers have one, is
one more source: it asks the path whether it already knows the key.
"""

import RNS

from trenchchat.core.protocol import author_digest
from trenchchat.core.storage import Storage


def sign_message(rns_identity, channel_hash_hex: str, message_id: str,
                 timestamp: float, content: str, reply_to: str | None,
                 last_seen_id: str | None, image_data: bytes | None,
                 manifest: dict | None = None) -> bytes:
    """Sign a message we are authoring."""
    return rns_identity.sign(author_digest(
        channel_hash_hex, message_id, timestamp, content,
        reply_to, last_seen_id, image_data, manifest,
    ))


def remember_identity(storage: Storage, rns_identity) -> None:
    """Cache a peer's public key, if it really is theirs.

    An identity hash is derived from the public key, so a key that does not
    hash back to the identity it claims is simply not that identity's key.
    """
    try:
        public_key = rns_identity.get_public_key()
        identity_hash_hex = rns_identity.hash.hex()
    except Exception as e:
        RNS.log(f"TrenchChat [authorship]: could not read peer key: {e}", RNS.LOG_DEBUG)
        return
    if not public_key or not _key_matches(public_key, identity_hash_hex):
        RNS.log(
            f"TrenchChat [authorship]: refused key not matching identity "
            f"{identity_hash_hex[:12]}…",
            RNS.LOG_WARNING,
        )
        return
    storage.remember_identity_key(identity_hash_hex, public_key)


def public_key_for(storage: Storage, author_hex: str, *,
                   router=None) -> bytes | None:
    """The author's public key, for relaying alongside their messages.

    A relayed message is unverifiable to a receiver who has never met its
    author, and an author who has left the mesh will never announce again --
    so without this the history of everyone who leaves becomes unreadable to
    everyone who arrives later. The key is public, and _key_matches proves it
    belongs to the identity claiming it, so passing it through a relay adds
    no trust in the relay.
    """
    identity = resolve_author(storage, author_hex, router=router)
    if identity is None:
        return None
    try:
        return identity.get_public_key()
    except Exception:
        return None


def remember_relayed_key(storage: Storage, author_hex: str, public_key) -> bool:
    """Cache a public key handed over by a relay. False if it isn't the author's.

    The check is the whole point: an identity hash is derived from its public
    key, so a key that does not hash back to author_hex simply is not their
    key, whoever passed it along.
    """
    if not author_hex or not isinstance(public_key, bytes) or not public_key:
        return False
    if not _key_matches(public_key, author_hex):
        RNS.log(
            f"TrenchChat [authorship]: relayed key does not match author "
            f"{author_hex[:12]}… — ignored",
            RNS.LOG_WARNING,
        )
        return False
    storage.remember_identity_key(author_hex, public_key)
    return True


def resolve_author(storage: Storage, author_hex: str, *, router=None):
    """The verifying identity for an author, from cache or from the transport.

    Falls back to the key the path already holds for that peer and caches it,
    so a peer only has to be reachable once for their history to stay
    checkable after they go quiet. Without a router there is only the cache.
    """
    if not author_hex:
        return None

    cached = storage.get_identity_key(author_hex)
    if cached:
        identity = _identity_from_key(cached, author_hex)
        if identity is not None:
            return identity

    if router is None:
        return None
    public_key = router.public_key_for(author_hex)
    if not remember_relayed_key(storage, author_hex, public_key):
        return None
    return _identity_from_key(public_key, author_hex)


def verify_message(storage: Storage, author_hex: str, signature: bytes,
                   channel_hash_hex: str, message_id: str, timestamp: float,
                   content: str, reply_to: str | None,
                   last_seen_id: str | None,
                   image_data: bytes | None,
                   manifest: dict | None = None, *, router=None) -> bool:
    """True if this message really was authored by author_hex as presented.

    False also covers "we cannot check yet" -- an author whose key we have
    never learned. Callers treat that as unverified rather than as forgery.
    """
    if not signature or not isinstance(signature, bytes):
        return False
    identity = resolve_author(storage, author_hex, router=router)
    if identity is None:
        return False
    digest = author_digest(
        channel_hash_hex, message_id, timestamp, content,
        reply_to, last_seen_id, image_data, manifest,
    )
    try:
        return bool(identity.validate(signature, digest))
    except Exception:
        return False


def _identity_from_key(public_key: bytes, expected_hash_hex: str):
    """Rebuild a verify-only identity, re-checking the key against the hash."""
    try:
        identity = RNS.Identity(create_keys=False)
        identity.load_public_key(public_key)
    except Exception:
        return None
    if identity.hash is None or identity.hash.hex() != expected_hash_hex:
        return None
    return identity


def _key_matches(public_key: bytes, identity_hash_hex: str) -> bool:
    return _identity_from_key(public_key, identity_hash_hex) is not None
