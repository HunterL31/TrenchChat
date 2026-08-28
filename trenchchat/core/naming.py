"""
Derivation of channel, server and direct-message hashes.

A channel or server hash is the hash of an RNS.Destination whose aspect path is
``trenchchat.<kind>.<sanitised_name>``. Because RNS.Destination.hash() accepts a
raw identity hash as well as an RNS.Identity, these are computable offline for
any peer -- which is what lets a receiver check that a hash it was handed really
was minted by the creator claimed alongside it.

A direct-message conversation has no creator: its address is derived from the two
identities in it, so both sides compute the same value with nothing to negotiate,
and a receiver can recompute it from the sender it just authenticated. An address
that does not match is not a conversation this node is part of.

This module has no local imports beyond the package aspect constants so it can be
imported by any layer without creating circular dependencies.
"""

import hashlib

import RNS

from trenchchat import APP_NAME, APP_ASPECT_CHANNEL, APP_ASPECT_SERVER


class NameInUseError(ValueError):
    """Raised when a name derives to an address this identity already owns.

    A hash comes from the creator's identity plus the sanitised name alone, so
    two channels (or two servers) of one name are one address. Reusing it would
    re-register a live RNS destination -- a hard error -- and overwrite the
    existing record.
    """


def sanitise_name(name: str) -> str:
    """Lower-case, alphanumeric + hyphens only, max 32 chars."""
    sanitised = "".join(c if c.isalnum() or c == "-" else "-" for c in name.lower())
    return sanitised[:32].strip("-")


def channel_hash_for(creator_identity_hash: bytes, name: str) -> str:
    """The channel hash a given creator would mint for *name*."""
    return RNS.Destination.hash(
        creator_identity_hash, APP_NAME, APP_ASPECT_CHANNEL, sanitise_name(name)
    ).hex()


def server_hash_for(creator_identity_hash: bytes, name: str) -> str:
    """The server hash a given creator would mint for *name*."""
    return RNS.Destination.hash(
        creator_identity_hash, APP_NAME, APP_ASPECT_SERVER, sanitise_name(name)
    ).hex()


# Domain tag, so a conversation address can never collide with a channel or
# server address derived from the same identities.
DM_HASH_DOMAIN = b"trenchchat-dm-v1"

# Width of a channel/server hash, which a conversation hash matches so it can be
# carried in F_CHANNEL_HASH and stored in messages.channel_hash unchanged.
DM_HASH_BYTES = RNS.Reticulum.TRUNCATED_HASHLENGTH // 8


def dm_hash_for(a_hash_hex: str, b_hash_hex: str) -> str:
    """The conversation address shared by two identities.

    Order-independent: both peers derive the same value from their own pair.
    """
    a = bytes.fromhex(a_hash_hex)
    b = bytes.fromhex(b_hash_hex)
    lo, hi = (a, b) if a <= b else (b, a)
    return hashlib.sha256(DM_HASH_DOMAIN + lo + hi).digest()[:DM_HASH_BYTES].hex()
