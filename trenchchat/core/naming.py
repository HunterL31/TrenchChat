"""
Derivation of channel and server hashes from a creator identity and a name.

A channel or server hash is the hash of an RNS.Destination whose aspect path is
``trenchchat.<kind>.<sanitised_name>``. Because RNS.Destination.hash() accepts a
raw identity hash as well as an RNS.Identity, these are computable offline for
any peer -- which is what lets a receiver check that a hash it was handed really
was minted by the creator claimed alongside it.

This module has no local imports beyond the package aspect constants so it can be
imported by any layer without creating circular dependencies.
"""

import RNS

from trenchchat import APP_NAME, APP_ASPECT_CHANNEL, APP_ASPECT_SERVER


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
