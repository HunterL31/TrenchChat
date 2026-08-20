"""
Invite-only channel membership management.

Member list document v2 (msgpack):
{
    "channel_hash":  bytes,        # 16 bytes
    "version":       int,
    "published_at":  float,
    "members":       [bytes, ...], # all member identity hashes
    "admins":        [bytes, ...], # subset of members
    "owners":        [bytes],      # exactly one — the channel creator
    "permissions":   bytes,        # msgpack-encoded permissions dict
    "signatures":    {bytes: bytes} # admin/owner hash -> Ed25519 signature
}

v2 signed payload = msgpack of:
    [channel_hash, version, published_at, sorted(members), sorted(admins),
     sorted(owners), permissions_blob]

v1 signed payload (legacy, no "owners" key in doc) = msgpack of:
    [channel_hash, version, published_at, sorted(members), sorted(admins)]

Invite token = Ed25519 signature over:
    invitee_identity_hash (16 bytes)
    + channel_hash (16 bytes)
    + expiry_timestamp (8 bytes, big-endian double)
"""

import hashlib
import struct
import threading
import time
import RNS
import LXMF
import msgpack

from trenchchat.core.control_retry import ControlRetryQueue
from trenchchat.core.identity import Identity
from trenchchat.core.naming import channel_hash_for, server_hash_for
from trenchchat.core.permissions import (
    CREATE_CHANNEL, INVITE, KICK, MANAGE_CHANNEL, MANAGE_ROLES,
    ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER,
    has_permission as _check_permission,
    is_valid_permissions, permissions_from_json, permissions_to_json,
)
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MSG_TYPE,
    F_INVITE_TOKEN, F_INVITEE_HASH, F_EXPIRY_TS, F_ADMIN_HASH,
    F_INVITE_ISSUED_TS,
    F_MEMBER_LIST_DOC, F_CHANNEL_NAME, F_CHANNEL_DESC,
    F_CHANNEL_CREATOR, F_CHANNEL_ACCESS, F_CHANNEL_CREATED_AT,
    F_CHANNEL_PERMISSIONS, F_SCOPE_KIND,
    MT_GOODBYE, MT_JOIN_REQUEST, MT_MEMBER_LIST_UPDATE, MT_INVITE, MT_PRESENCE,
    SYNC_WINDOW_SECS,
    unpack_wire, wire_timestamp,
)
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router

DEFAULT_TOKEN_TTL = 7 * 24 * 3600  # 7 days

# Upper bound on a member list version. The counter increments by one per
# publish, so nothing legitimate approaches this; the bound exists because
# version is signed wire data that drives an ordering comparison, and a
# non-finite value compares greater than every later document forever.
MAX_MEMBER_LIST_VERSION = 2 ** 31 - 1


def _recover_owners(owners: list[bytes], admins: list[bytes],
                    channel: object | None) -> list[bytes]:
    """Return a non-empty owners list, recovering from v1 docs that lack one.

    v1 member list documents have no 'owners' key.  When upgrading such a doc
    to v2, fall back to the channel creator hash so the owner is not silently
    demoted to admin on the next publish.
    """
    if owners:
        return owners
    if channel and channel["creator_hash"]:
        try:
            return [bytes.fromhex(channel["creator_hash"])]
        except ValueError:
            pass
    return list(admins)


def _token_payload(invitee_hash: bytes, channel_hash_hex: str, expiry: float,
                   issued_at: float | None) -> bytes:
    """Bytes an invite token signs over.

    issued_at is appended only when present, so a token from a peer that
    predates the field reproduces its original payload exactly.
    """
    payload = (invitee_hash
               + bytes.fromhex(channel_hash_hex)
               + struct.pack(">d", expiry))
    if issued_at is not None:
        payload += struct.pack(">d", issued_at)
    return payload


def _signed_payload(channel_hash: bytes, version: int, published_at: float,
                    members: list[bytes], admins: list[bytes],
                    owners: list[bytes] | None = None,
                    permissions_blob: bytes = b"",
                    joined_at: dict[bytes, float] | None = None,
                    departed: dict[bytes, tuple[float, float]] | None = None,
                    channels_blob: bytes | None = None) -> bytes:
    """Build the payload that gets signed.

    If *owners* is provided the v2 format is used (includes owners and
    permissions_blob).  Otherwise the v1 format is used for backward compat.
    If *joined_at* is also provided (v3, requires v2), each member's true
    historical join time is bound into the signature too, so a recipient
    can trust it came from the signing admin and wasn't added or altered
    by whoever relayed the document.
    If *departed* is also provided (v5, requires v3), each entry binds a
    former member's (joined_at, left_at) interval into the signature, so a
    brand-new joiner -- who has no prior local state to diff a removal
    against -- can still validate messages sent by someone who left before
    they joined.
    If *channels_blob* is also provided (v4, requires v3) the document scopes
    a server and the blob is its channel roster.

    *departed* and *channels_blob* are independent siblings once *joined_at*
    is present -- a legacy v4 server document (channels_blob, no departed,
    from a peer that predates this field) must still reproduce its original
    signed bytes, so channels_blob's inclusion must not be nested inside
    departed's presence check.

    The nesting means None is "absent", so a standalone channel's payload is
    byte-identical to what the pre-server code produced.
    """
    items: list = [channel_hash, version, published_at,
                   sorted(members), sorted(admins)]
    if owners is not None:
        items.extend([sorted(owners), permissions_blob])
        if joined_at is not None:
            items.append(sorted(joined_at.items()))
            if departed is not None:
                items.append(sorted(
                    (m, jt, lt) for m, (jt, lt) in departed.items()
                ))
            if channels_blob is not None:
                items.append(channels_blob)
    return msgpack.packb(items, use_bin_type=True)


def encode_roster(rows) -> bytes:
    """Canonical msgpack encoding of a server's channel roster.

    creator_hash rides along because sync and leave paths need it, and because
    it is what binds each roster entry's hash to a creator that could have
    minted it.
    """
    entries = []
    for r in rows:
        try:
            entries.append([
                bytes.fromhex(r["hash"]), r["name"], r["description"] or "",
                bytes.fromhex(r["creator_hash"]), float(r["created_at"]),
            ])
        except (ValueError, TypeError):
            continue
    return msgpack.packb(sorted(entries), use_bin_type=True)


def _decode(value) -> str:
    """LXMF may deliver string fields as bytes depending on msgpack encoding."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def _sign(identity: RNS.Identity, data: bytes) -> bytes:
    return identity.sign(data)


def _verify(identity: RNS.Identity, data: bytes, signature: bytes) -> bool:
    try:
        return identity.validate(signature, data)
    except Exception:
        return False


class InviteManager:
    def __init__(self, identity: Identity, storage: Storage, router: Router):
        self._identity = identity
        self._storage = storage
        self._router = router
        self._invite_callbacks: list = []
        self._channel_callbacks: list = []
        self._member_list_callbacks: list = []
        # Serialises the read-compare-write in _accept_document; LXMF
        # delivers on background threads.
        self._accept_lock = threading.Lock()
        # An invite or join request sent before the recipient's path
        # resolved used to be dropped outright, and nothing re-sent it.
        self._retry = ControlRetryQueue("invite")
        # scope_hex -> "server" | "channel", learned from an inbound invite.
        # Presentation only: trust anchoring is the accepted_invites table.
        self._invite_scope_kinds: dict[str, str] = {}
        self._storage.purge_expired_pending_invites()
        router.add_delivery_callback(self._on_lxmf_message)

    def invite_scope_kind(self, scope_hash_hex: str) -> str:
        """Whether a pending invite targets a "server" or a "channel"."""
        if self._storage.is_server(scope_hash_hex):
            return "server"
        return self._invite_scope_kinds.get(scope_hash_hex, "channel")

    def add_invite_callback(self, callback):
        """callback(channel_hash_hex, channel_name, token, expiry, admin_hash_hex)

        Whether the invite targets a server or a single channel is not a
        callback argument -- consumers that care ask invite_scope_kind(),
        so adding servers didn't change this signature.
        """
        if callback not in self._invite_callbacks:
            self._invite_callbacks.append(callback)

    def remove_invite_callback(self, callback):
        if callback in self._invite_callbacks:
            self._invite_callbacks.remove(callback)

    def add_channel_joined_callback(self, callback):
        """callback(channel_hash_hex, channel_name) — fired when auto-joined via invite."""
        if callback not in self._channel_callbacks:
            self._channel_callbacks.append(callback)

    def remove_channel_joined_callback(self, callback):
        if callback in self._channel_callbacks:
            self._channel_callbacks.remove(callback)

    def add_member_list_callback(self, callback):
        """callback(channel_hash_hex) — fired whenever a member list update is accepted."""
        if callback not in self._member_list_callbacks:
            self._member_list_callbacks.append(callback)

    def remove_member_list_callback(self, callback):
        if callback in self._member_list_callbacks:
            self._member_list_callbacks.remove(callback)

    # --- member list document ---

    def _build_document(self, channel_hash_hex: str,
                        members: list[bytes], admins: list[bytes],
                        version: int, published_at: float,
                        owners: list[bytes] | None = None,
                        permissions: dict | None = None,
                        joined_at: dict[bytes, float] | None = None,
                        departed: dict[bytes, tuple[float, float]] | None = None,
                        channels_blob: bytes | None = None) -> dict:
        if owners is None:
            owners = []
        if joined_at is None:
            joined_at = {}
        if departed is None:
            departed = {}
        permissions_blob = (msgpack.packb(permissions, use_bin_type=True)
                            if permissions else b"")
        payload = _signed_payload(
            bytes.fromhex(channel_hash_hex), version, published_at,
            members, admins, owners=owners, permissions_blob=permissions_blob,
            joined_at=joined_at, departed=departed, channels_blob=channels_blob,
        )
        sig = _sign(self._identity.rns_identity, payload)
        doc = {
            "channel_hash": bytes.fromhex(channel_hash_hex),
            "version":      version,
            "published_at": published_at,
            "members":      members,
            "admins":       admins,
            "owners":       owners,
            "permissions":  permissions_blob,
            "joined_at":    joined_at,
            "departed":     departed,
            "signatures":   {self._identity.hash: sig},
        }
        if channels_blob is not None:
            doc["channels"] = channels_blob
        return doc

    def _can_anchor(self, channel_hash_hex: str) -> bool:
        """True if we hold something to check a document's signer against.

        Uses the scope-aware creator lookup so a server we already know anchors
        the same way a channel does. It must only ever read state stored before
        this message arrived -- see the caller.
        """
        if self._storage.get_member_list_version(channel_hash_hex):
            return True
        if self._storage.get_scope_creator_hash(channel_hash_hex):
            return True
        return self._storage.get_accepted_invite_admin(channel_hash_hex) is not None

    def _hold_for_confirmation(self, doc: dict, channel_hash_hex: str,
                               fields: dict) -> None:
        """Hold an unanchorable document until the user confirms it.

        Nothing is applied and the channel is not created or subscribed to.
        A document naming someone else is dropped outright.
        """
        if self._identity.hash not in doc.get("members", []):
            RNS.log(
                f"TrenchChat [invite]: dropping unanchored member list doc for "
                f"{channel_hash_hex[:12]}… — does not name us",
                RNS.LOG_WARNING,
            )
            return

        signers = list(doc.get("owners") or []) + list(doc.get("admins") or [])
        if not signers:
            return
        admin_hex = signers[0].hex() if isinstance(signers[0], bytes) else ""
        if not admin_hex:
            return

        channel_name = fields.get(F_CHANNEL_NAME, b"")
        if isinstance(channel_name, bytes):
            channel_name = channel_name.decode("utf-8", errors="replace")
        channel_name = str(channel_name) or channel_hash_hex[:12]

        meta = {
            k: fields.get(k) for k in (
                F_CHANNEL_NAME, F_CHANNEL_DESC, F_CHANNEL_CREATOR,
                F_CHANNEL_PERMISSIONS, F_CHANNEL_ACCESS, F_CHANNEL_CREATED_AT,
            ) if fields.get(k) is not None
        }
        self._storage.record_pending_member_doc(
            channel_hash_hex, channel_name, admin_hex,
            msgpack.packb(doc, use_bin_type=True),
            msgpack.packb(meta, use_bin_type=True),
        )
        RNS.log(
            f"TrenchChat [invite]: holding unanchored member list doc for "
            f"{channel_name!r} ({channel_hash_hex[:12]}…) pending confirmation",
            RNS.LOG_NOTICE,
        )
        for cb in self._invite_callbacks:
            try:
                cb(channel_hash_hex, channel_name, None, 0.0, admin_hex)
            except Exception as e:
                RNS.log(f"TrenchChat: invite callback error: {e}", RNS.LOG_ERROR)

    def list_pending_memberships(self) -> list[dict]:
        """Channels we have been added to but have not confirmed."""
        return self._storage.get_pending_member_docs()

    def decline_pending_membership(self, channel_hash_hex: str) -> None:
        self._storage.clear_pending_member_doc(channel_hash_hex)

    # --- pending invite tokens ---

    def list_pending_invites(self) -> list[dict]:
        """Received invite tokens not yet accepted or declined, unexpired."""
        return [
            {
                "channel_hash_hex": row["channel_hash"],
                "channel_name":     row["channel_name"],
                "token":            bytes(row["token"]),
                "expiry":           row["expiry"],
                "admin_hash_hex":   row["admin_hash"],
            }
            for row in self._storage.get_pending_invites()
        ]

    def decline_invite(self, channel_hash_hex: str) -> None:
        """Drop a received invite token without acting on it."""
        self._storage.clear_pending_invite(channel_hash_hex)

    def accept_pending_membership(self, channel_hash_hex: str) -> bool:
        """Confirm a held document: anchor it to its signer, then apply it."""
        row = self._storage.get_pending_member_doc(channel_hash_hex)
        if row is None:
            return False

        self._storage.record_accepted_invite(
            channel_hash_hex, row["admin_hash"], time.time() + DEFAULT_TOKEN_TTL
        )
        try:
            doc = unpack_wire(bytes(row["doc_blob"]), raw=False)
            # meta is keyed by the integer field constants and was written by
            # us, so it needs neither strict map keys nor the wire limits.
            meta = msgpack.unpackb(bytes(row["meta_blob"]), raw=False,
                                   strict_map_key=False)
        except Exception as e:
            RNS.log(f"TrenchChat [invite]: pending doc unpack error: {e}",
                    RNS.LOG_WARNING)
            self._storage.clear_pending_member_doc(channel_hash_hex)
            return False

        accepted = self._accept_document(doc, channel_hash_hex)
        self._storage.clear_pending_member_doc(channel_hash_hex)
        if not accepted:
            self._storage.clear_accepted_invite(channel_hash_hex)
            return False

        self._create_channel_from_metadata(channel_hash_hex, meta,
                                           row["channel_name"])
        return True

    def _create_channel_from_metadata(self, channel_hash_hex: str, meta: dict,
                                      fallback_name: str) -> None:
        if self._storage.get_channel(channel_hash_hex):
            return

        def _text(key, default=""):
            value = meta.get(key, default)
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            return str(value) if value is not None else default

        name = _text(F_CHANNEL_NAME) or fallback_name
        perms_field = meta.get(F_CHANNEL_PERMISSIONS)
        if perms_field is None:
            perms_field = meta.get(F_CHANNEL_ACCESS, b"invite")
        if isinstance(perms_field, bytes):
            perms_field = perms_field.decode("utf-8", errors="replace")

        self._storage.upsert_channel(
            hash=channel_hash_hex,
            name=name,
            description=_text(F_CHANNEL_DESC),
            creator_hash=_text(F_CHANNEL_CREATOR),
            permissions=perms_field,
            created_at=meta.get(F_CHANNEL_CREATED_AT, time.time()),
        )
        self._storage.subscribe(channel_hash_hex)
        RNS.log(f"TrenchChat [invite]: joined channel {name!r} "
                f"({channel_hash_hex[:12]}…) after confirmation", RNS.LOG_NOTICE)
        for cb in self._channel_callbacks:
            try:
                cb(channel_hash_hex, name)
            except Exception as e:
                RNS.log(f"TrenchChat: channel callback error: {e}", RNS.LOG_ERROR)

    def _validate_document(self, doc: dict, channel_hash_hex: str) -> bytes | None:
        """Return the hash of the trusted signer that validated, or None.

        The signer must be recognised as an admin or owner in the *previously
        stored* member list for this channel (or be the channel creator when no
        stored list exists yet).  Checking only the incoming doc's own admin/owner
        lists would allow a malicious peer to grant themselves signing authority
        by simply listing themselves as an admin in the doc they craft.

        The signer identity is returned rather than a bare bool so the caller
        can check what that specific signer is actually permitted to change --
        being a trusted signer authorises signing, not every mutation. Server
        roster additions are authorised that way too.
        """
        admins_in_doc: list[bytes] = doc.get("admins", [])
        owners_in_doc: list[bytes] = doc.get("owners", [])
        sigs: dict = doc.get("signatures", {})

        # Determine the set of identities that are *currently* authorised to
        # sign member list updates for this channel.
        #
        # Priority order:
        #   1. Previously stored member list doc — most secure; prevents a peer
        #      from granting themselves signing authority in a crafted doc.
        #   2. Channel creator from local storage — used when we have a channel
        #      record but no stored member list yet (e.g. first publish).
        #   3. The doc's own admins/owners — bootstrap fallback for peers that
        #      receive a member list before they have any local channel record
        #      (e.g. auto-join on first invite).  The cryptographic signature
        #      check still applies; we just can't cross-reference a stored list.
        existing = self._storage.get_member_list_version(channel_hash_hex)
        if existing:
            old_doc = msgpack.unpackb(existing["document_blob"], raw=True)
            trusted_signers: set[bytes] = (
                set(old_doc.get(b"admins", []))
                | set(old_doc.get(b"owners", []))
            )
        else:
            # get_scope_creator_hash checks servers before channels: without
            # that a server document would skip this tier entirely and fall
            # through to the weaker anchors below.
            creator_hex = self._storage.get_scope_creator_hash(channel_hash_hex)
            trusted_signers = set()
            if creator_hex:
                try:
                    trusted_signers = {bytes.fromhex(creator_hex)}
                except ValueError:
                    trusted_signers = set()
            if not trusted_signers:
                # Next anchor: an invite this user actively accepted, which
                # names the admin we sent the join request to.
                admin_hex = self._storage.get_accepted_invite_admin(channel_hash_hex)
                if admin_hex:
                    try:
                        trusted_signers = {bytes.fromhex(admin_hex)}
                    except ValueError:
                        trusted_signers = set()

            if not trusted_signers:
                RNS.log(
                    f"TrenchChat [invite]: no trust anchor for member list doc "
                    f"on channel {channel_hash_hex[:12]}… — rejected",
                    RNS.LOG_WARNING,
                )
                return None

        is_v2 = "owners" in doc
        joined_at_in_doc = doc.get("joined_at") if "joined_at" in doc else None
        departed_in_doc = doc.get("departed") if "departed" in doc else None
        channels_in_doc = doc.get("channels") if "channels" in doc else None
        if is_v2:
            payload = _signed_payload(
                doc["channel_hash"], doc["version"], doc["published_at"],
                doc["members"], admins_in_doc,
                owners=owners_in_doc, permissions_blob=doc.get("permissions", b""),
                joined_at=joined_at_in_doc, departed=departed_in_doc,
                channels_blob=channels_in_doc,
            )
        else:
            payload = _signed_payload(
                doc["channel_hash"], doc["version"], doc["published_at"],
                doc["members"], admins_in_doc,
            )

        for signer_hash_bytes, sig in sorted(sigs.items()):
            if signer_hash_bytes not in trusted_signers:
                continue
            if signer_hash_bytes == self._identity.hash:
                signer_identity = self._identity.rns_identity
            else:
                delivery_hash = RNS.Destination.hash(signer_hash_bytes, "lxmf", "delivery")
                signer_identity = RNS.Identity.recall(delivery_hash)
            if signer_identity is None:
                continue
            if _verify(signer_identity, payload, sig):
                return signer_hash_bytes
        return None

    def _signer_may_apply(self, doc: dict, channel_hash_hex: str,
                          signer: bytes) -> bool:
        """Check the signer is permitted to make the changes this doc contains.

        A valid signature proves who wrote the document, not that they were
        allowed to write it.  Every comparison is against stored state, never
        the document's own claims.  Returns True when there is no stored state
        to diff against, where _validate_document's signer-trust rules are the
        only available control.
        """
        existing = self._storage.get_member_list_version(channel_hash_hex)
        if not existing:
            return True

        try:
            old_doc = unpack_wire(existing["document_blob"], raw=True)
        except Exception as e:
            # We wrote this blob, so it should always parse. If it doesn't,
            # there is no stored state left to diff against and no way to
            # judge the signer -- refuse rather than wave the document through.
            RNS.log(
                f"TrenchChat [invite]: refusing member list doc for "
                f"{channel_hash_hex[:12]}… — stored document unreadable: {e}",
                RNS.LOG_ERROR,
            )
            return False

        signer_hex = signer.hex()
        old_members = set(old_doc.get(b"members", []))
        old_admins  = set(old_doc.get(b"admins", []))
        old_owners  = set(old_doc.get(b"owners", []))
        new_members = set(doc.get("members", []))
        new_admins  = set(doc.get("admins", []))
        new_owners  = set(doc.get("owners", []))

        def _deny(what: str, permission: str) -> bool:
            RNS.log(
                f"TrenchChat [invite]: rejecting member list doc from "
                f"{signer_hex[:12]}… — {what} requires {permission}",
                RNS.LOG_WARNING,
            )
            return False

        # Additions are governed by the invite/join-request path, not here.
        removed = old_members - new_members
        if removed:
            if not self._storage.has_permission(channel_hash_hex, signer_hex, KICK):
                return _deny("removing members", KICK)

            # A role is derived from membership: an identity absent from
            # members gets no row, and no row means no permission -- including
            # the owner short-circuit. So removing someone from members is how
            # far their authority reaches, and asking only "may you remove
            # *someone*" lets KICK alone depose the owner who granted it,
            # leaving the owners list untouched so its own gate never fires.
            if removed & old_owners and signer not in old_owners:
                RNS.log(
                    f"TrenchChat [invite]: rejecting member list doc from "
                    f"{signer_hex[:12]}… — only an owner may remove an owner",
                    RNS.LOG_WARNING,
                )
                return False
            if removed & old_admins and not self._storage.has_permission(
                    channel_hash_hex, signer_hex, MANAGE_ROLES):
                return _deny("removing an admin", MANAGE_ROLES)

        # Any change to the admin set requires MANAGE_ROLES.
        if old_admins != new_admins:
            if not self._storage.has_permission(channel_hash_hex, signer_hex,
                                                MANAGE_ROLES):
                return _deny("changing admins", MANAGE_ROLES)

        # Only an existing owner may alter the owner set; MANAGE_ROLES is
        # not sufficient.
        if old_owners != new_owners:
            if signer not in old_owners:
                RNS.log(
                    f"TrenchChat [invite]: rejecting member list doc from "
                    f"{signer_hex[:12]}… — only an owner may change the owner list",
                    RNS.LOG_WARNING,
                )
                return False

        # An empty authority set makes trusted_signers empty, after which no
        # future update can validate.
        if not new_admins and not new_owners:
            RNS.log(
                f"TrenchChat [invite]: rejecting member list doc that would "
                f"leave {channel_hash_hex[:12]}… with no admins or owners",
                RNS.LOG_WARNING,
            )
            return False

        # An empty blob asserts nothing; _accept_document only applies a
        # non-empty one.
        old_perms = old_doc.get(b"permissions", b"") or b""
        new_perms = doc.get("permissions", b"") or b""
        if new_perms and new_perms != old_perms:
            if not self._storage.has_permission(channel_hash_hex, signer_hex,
                                                MANAGE_CHANNEL):
                return _deny("changing channel permissions", MANAGE_CHANNEL)

        # Adding a channel to a server's roster is a mutation like any other.
        if self._roster_additions(old_doc, doc):
            if not self._storage.has_permission(channel_hash_hex, signer_hex,
                                                CREATE_CHANNEL):
                return _deny("adding channels to the server", CREATE_CHANNEL)

        return True

    @staticmethod
    def _roster_hashes(blob) -> set[str]:
        if not blob:
            return set()
        try:
            return {e[0].hex() for e in unpack_wire(blob)
                    if isinstance(e, list) and e and isinstance(e[0], bytes)}
        except Exception:
            return set()

    def _roster_additions(self, old_doc: dict, doc: dict) -> set[str]:
        """Channel hashes the incoming document adds to the stored roster."""
        return (self._roster_hashes(doc.get("channels"))
                - self._roster_hashes(old_doc.get(b"channels")))

    def _accept_document(self, doc: dict, channel_hash_hex: str) -> bool:
        """Validate and apply a member list document under the accept lock.

        Callbacks fire outside the lock: they run arbitrary listener code
        (including GUI marshalling) and must not be able to deadlock the
        ingestion path by re-entering it.
        """
        with self._accept_lock:
            accepted = self._accept_document_locked(doc, channel_hash_hex)

        if accepted:
            for cb in self._member_list_callbacks:
                try:
                    cb(channel_hash_hex)
                except Exception as e:
                    RNS.log(f"TrenchChat: member list callback error: {e}",
                            RNS.LOG_ERROR)
        return accepted

    def _ordering_fields_are_sane(self, doc: dict, channel_hash_hex: str) -> bool:
        """False if version or published_at could not order a document.

        Both are signed, so a trusted signer can put anything in them, and both
        drive the comparison that decides which document wins. A version of
        float("inf") is stored, then compares greater than every later document
        for good -- no kick, promotion or join can be applied to the channel
        again, and the poisoned peer re-broadcasts it.
        """
        version = doc.get("version")
        if isinstance(version, bool) or not isinstance(version, int) \
                or not 0 <= version <= MAX_MEMBER_LIST_VERSION:
            RNS.log(
                f"TrenchChat [invite]: member list doc for {channel_hash_hex[:12]}… "
                f"has an unusable version {version!r} — rejected",
                RNS.LOG_WARNING,
            )
            return False

        if wire_timestamp(doc.get("published_at")) is None:
            RNS.log(
                f"TrenchChat [invite]: member list doc for {channel_hash_hex[:12]}… "
                f"has an implausible published_at {doc.get('published_at')!r} — rejected",
                RNS.LOG_WARNING,
            )
            return False
        return True

    def _accept_document_locked(self, doc: dict, channel_hash_hex: str) -> bool:
        """
        Apply acceptance rules. Returns True if accepted.
        Rules (in order):
          1. doc["channel_hash"] must match the expected channel.
          2. version and published_at must be orderable values.
          3. At least one valid admin signature.
          4. version > local_version  → accept.
          5. version == local_version, higher published_at → accept.
          6. version == local_version, same published_at, lower admin hash → accept.
        """
        doc_channel_hex = doc.get("channel_hash", b"").hex() \
            if isinstance(doc.get("channel_hash"), bytes) else str(doc.get("channel_hash", ""))
        if doc_channel_hex != channel_hash_hex:
            RNS.log(
                f"TrenchChat [invite]: member list doc channel hash mismatch "
                f"(doc={doc_channel_hex[:12]}… expected={channel_hash_hex[:12]}…) — rejected",
                RNS.LOG_WARNING,
            )
            return False

        if not self._ordering_fields_are_sane(doc, channel_hash_hex):
            return False

        signer = self._validate_document(doc, channel_hash_hex)
        if signer is None:
            return False

        if not self._signer_may_apply(doc, channel_hash_hex, signer):
            return False

        existing = self._storage.get_member_list_version(channel_hash_hex)
        new_v = doc["version"]
        new_ts = doc["published_at"]

        if existing is None:
            pass  # no existing — accept
        else:
            old_v = existing["version"]
            old_ts = existing["published_at"]
            if new_v < old_v:
                return False
            if new_v == old_v:
                if new_ts < old_ts:
                    return False
                if new_ts == old_ts:
                    # Tiebreak: lowest signing admin hash wins
                    new_min = min(doc.get("signatures", {}).keys(), default=b"\xff" * 16)
                    old_doc = msgpack.unpackb(existing["document_blob"], raw=True)
                    old_min = min(old_doc.get(b"signatures", {}).keys(),
                                  default=b"\xff" * 16)
                    if new_min >= old_min:
                        return False

        # Build the roster before committing anything: a malformed entry here
        # must not leave the version advanced and the members table stale.
        owners_set = set(doc.get("owners", []))
        admins_set = set(doc.get("admins", []))
        member_rows: list[tuple[str, str, str]] = []
        new_member_hashes: set[str] = set()
        for m in doc["members"]:
            if not isinstance(m, bytes):
                RNS.log(
                    f"TrenchChat [invite]: member list doc for "
                    f"{channel_hash_hex[:12]}… contains a non-bytes member "
                    f"entry — rejected",
                    RNS.LOG_WARNING,
                )
                return False
            m_hex = m.hex()
            new_member_hashes.add(m_hex)
            if m in owners_set:
                role = ROLE_OWNER
            elif m in admins_set:
                role = ROLE_ADMIN
            else:
                role = ROLE_MEMBER
            member_rows.append((m_hex, "", role))

        # Capture the old member set for tenure diffing before we replace it
        old_member_hashes: set[str] = {
            row["identity_hash"]
            for row in self._storage.get_members(channel_hash_hex)
        }

        # Persist
        blob = msgpack.packb(doc, use_bin_type=True)
        self._storage.upsert_member_list_version(
            channel_hash_hex, new_v, new_ts, blob
        )
        self._storage.replace_members(channel_hash_hex, member_rows)

        # Update tenure log: close intervals for removed members, open for
        # added ones. Prefer each member's true historical joined_at, signed
        # into the document itself (so not spoofable by an untrusted party --
        # the signer must already be a trusted admin/owner per the check
        # above), over new_ts (this document version's publish time) --
        # otherwise the first version of the document a peer ever processes
        # makes everyone in it, including the owner, look like they joined
        # "now", hiding all of their prior history. Falls back to new_ts for
        # documents from before this field existed. float() coercion with a
        # skip-on-failure guards against a malformed (not necessarily
        # malicious -- the signature check already rules that out) timestamp
        # value in an older or hand-crafted document.
        joined_at_map: dict[str, float] = {}
        for m, ts in (doc.get("joined_at") or {}).items():
            m_hex = m.hex() if isinstance(m, bytes) else str(m)
            try:
                joined_at_map[m_hex] = float(ts)
            except (TypeError, ValueError):
                continue
        self._storage.update_tenure(
            channel_hash_hex, old_member_hashes, new_member_hashes, new_ts,
            joined_at_map=joined_at_map,
        )

        # Departed-member tenure, signed into the document the same way
        # joined_at is: lets a brand-new joiner validate messages sent by
        # someone who left before they joined, which update_tenure's
        # added/removed diff can never teach them (it only fires relative to
        # what a peer already knew). INSERT OR IGNORE means this can only
        # ever add intervals not already on file, never override them.
        for m, interval in (doc.get("departed") or {}).items():
            m_hex = m.hex() if isinstance(m, bytes) else str(m)
            try:
                joined_at_val, left_at_val = float(interval[0]), float(interval[1])
            except (TypeError, ValueError, IndexError):
                continue
            self._storage.record_departed_tenure(
                channel_hash_hex, m_hex, joined_at_val, left_at_val
            )

        # v2 only: the v1 signed payload does not cover the permissions
        # field, so a v1 doc's blob is unauthenticated.
        perms_blob = doc.get("permissions", b"")
        if perms_blob and "owners" not in doc:
            RNS.log(
                f"TrenchChat [invite]: ignoring permissions on a v1 member "
                f"list doc for {channel_hash_hex[:12]}… — not covered by the "
                f"signature",
                RNS.LOG_WARNING,
            )
        elif perms_blob:
            try:
                perms = unpack_wire(perms_blob)
            except Exception as e:
                RNS.log(f"TrenchChat [invite]: permissions blob unpack error: {e}",
                        RNS.LOG_WARNING)
            else:
                if is_valid_permissions(perms):
                    if self._storage.is_server(channel_hash_hex):
                        # Also rewrites channels.permissions for every child
                        # channel; rebuilding that mirror from each accepted
                        # document is what makes drift self-healing.
                        self._storage.set_server_permissions(channel_hash_hex, perms)
                    else:
                        self._storage.set_channel_permissions(channel_hash_hex, perms)
                else:
                    RNS.log(
                        f"TrenchChat [invite]: rejecting malformed permissions "
                        f"for {channel_hash_hex[:12]}…",
                        RNS.LOG_WARNING,
                    )

        new_channels: list[tuple[str, str]] = []
        if "channels" in doc and self._storage.is_server(channel_hash_hex):
            new_channels = self._materialise_roster(
                channel_hash_hex, doc, signer, existing,
            )

        for hash_hex, name in new_channels:
            for cb in self._channel_callbacks:
                try:
                    cb(hash_hex, name)
                except Exception as e:
                    RNS.log(f"TrenchChat: channel joined callback error: {e}",
                            RNS.LOG_ERROR)

        return True

    def _creator_binds(self, channel_hash_hex: str, creator_hex: str,
                       name: str) -> bool:
        """True if this creator could have minted this channel hash for *name*."""
        try:
            creator_bytes = bytes.fromhex(creator_hex)
        except (TypeError, ValueError):
            return False
        if channel_hash_hex == channel_hash_for(creator_bytes, name):
            return True
        RNS.log(
            f"TrenchChat [invite]: channel {channel_hash_hex[:12]}… does not bind "
            f"to claimed creator {creator_hex[:12]}… and name {name!r} — ignored",
            RNS.LOG_WARNING,
        )
        return False

    def _materialise_roster(self, server_hash_hex: str, doc: dict,
                            signer: bytes, existing) -> list[tuple[str, str]]:
        """Create local channel rows for a server document's roster.

        Union-merge only: a channel present locally but absent from the roster
        is never deleted, so roster propagation stays monotonic and converges
        even when a concurrent publish loses the version tiebreak.

        Returns (channel_hash_hex, name) for channels newly created here.
        """
        # unpack_wire, not msgpack.unpackb: this came off the network.
        try:
            roster = unpack_wire(doc["channels"])
        except Exception as e:
            RNS.log(f"TrenchChat [invite]: unreadable channel roster: {e}",
                    RNS.LOG_WARNING)
            return []
        if not isinstance(roster, list):
            return []

        old_hashes: set[str] = set()
        if existing:
            try:
                old_doc = unpack_wire(existing["document_blob"], raw=True)
                old_hashes = self._roster_hashes(old_doc.get(b"channels"))
            except Exception:
                pass

        # Whether the signer was allowed to add these at all is decided in
        # _signer_may_apply, which rejects the whole document; by here the
        # additions are authorised.
        added = {e[0].hex() for e in roster
                 if isinstance(e, list) and len(e) >= 5} - old_hashes

        perms_blob = doc.get("permissions", b"")
        try:
            channel_perms = msgpack.unpackb(perms_blob, raw=False) if perms_blob else None
        except Exception:
            channel_perms = None

        am_member = self._identity.hash in set(doc.get("members", []))
        created: list[tuple[str, str]] = []

        for entry in roster:
            if not isinstance(entry, list) or len(entry) < 5:
                continue
            ch_hash, name, desc, creator, created_at = entry[:5]
            if not isinstance(ch_hash, bytes) or not isinstance(creator, bytes):
                continue
            ch_hex = ch_hash.hex()
            if ch_hex not in added and ch_hex not in old_hashes:
                continue
            if not isinstance(name, str):
                continue

            # Defence: the hash must be one this creator could actually have
            # minted. Preimage resistance means a forged entry cannot name a
            # hash the claimed creator never derived.
            if ch_hex != channel_hash_for(creator, name):
                RNS.log(
                    f"TrenchChat [invite]: roster entry {ch_hex[:12]}… does not "
                    f"bind to its claimed creator and name — skipped",
                    RNS.LOG_WARNING,
                )
                continue

            # Defence: never adopt a channel we already know under another
            # parent. Without this a roster could capture a private standalone
            # channel and hand the server's members its membership and history.
            row = self._storage.get_channel(ch_hex)
            if row is not None and row["server_hash"] != server_hash_hex:
                RNS.log(
                    f"TrenchChat [invite]: refusing to re-parent existing channel "
                    f"{ch_hex[:12]}… into server {server_hash_hex[:12]}…",
                    RNS.LOG_WARNING,
                )
                continue

            is_new = row is None
            self._storage.upsert_channel(
                hash=ch_hex,
                name=name,
                description=desc if isinstance(desc, str) else "",
                creator_hash=creator.hex(),
                permissions=channel_perms,
                created_at=float(created_at) if created_at else time.time(),
                server_hash=server_hash_hex,
            )
            if am_member:
                self._storage.subscribe(ch_hex)
            if is_new:
                created.append((ch_hex, name))
        return created

    def _materialise_server(self, fields: dict, server_hash_hex: str) -> None:
        """Create the local servers row for an inbound server document.

        The name and creator ride in unsigned LXMF fields, so they are only
        trusted once they hash back to the scope itself: RNS.Destination.hash
        takes a raw identity hash, making that check computable offline, and
        preimage resistance means nobody can name a server hash they did not
        mint. Without it a peer could impersonate someone else's server.
        """
        if self._storage.get_server(server_hash_hex) is not None:
            return
        name = _decode(fields.get(F_CHANNEL_NAME, ""))
        creator_hex = _decode(fields.get(F_CHANNEL_CREATOR, ""))
        if not name or not creator_hex:
            return
        try:
            creator = bytes.fromhex(creator_hex)
        except ValueError:
            return
        if server_hash_for(creator, name) != server_hash_hex:
            RNS.log(
                f"TrenchChat [invite]: server {server_hash_hex[:12]}… does not bind "
                f"to its claimed creator and name — not materialised",
                RNS.LOG_WARNING,
            )
            return
        perms = _decode(fields.get(F_CHANNEL_PERMISSIONS, "")) or ""
        self._storage.upsert_server(
            hash=server_hash_hex,
            name=name,
            description=_decode(fields.get(F_CHANNEL_DESC, "")),
            creator_hash=creator_hex,
            permissions=perms,
            created_at=fields.get(F_CHANNEL_CREATED_AT, time.time()),
        )
        RNS.log(f"TrenchChat [invite]: joined server {name!r} "
                f"({server_hash_hex[:12]}…)", RNS.LOG_NOTICE)

    # --- publish a new member list (admin action) ---

    def _removable_by(self, channel_hash_hex: str, actor_hex: str,
                      targets: list[bytes]) -> list[bytes]:
        """Drop targets the actor holds KICK over but not enough authority for.

        Outbound mirror of _signer_may_apply's removal rules: KICK says you may
        remove a member, not that you may remove the owner who granted it.
        """
        actor_is_owner = self._storage.get_role(channel_hash_hex, actor_hex) == ROLE_OWNER
        may_manage_roles = self._storage.has_permission(
            channel_hash_hex, actor_hex, MANAGE_ROLES)
        allowed = []
        for target in targets:
            target_role = self._storage.get_role(channel_hash_hex, target.hex())
            if target_role == ROLE_OWNER and not actor_is_owner:
                RNS.log(
                    f"TrenchChat [invite]: {actor_hex[:12]}… attempted to remove "
                    f"owner {target.hex()[:12]}… — ignored",
                    RNS.LOG_WARNING,
                )
                continue
            if target_role == ROLE_ADMIN and not may_manage_roles:
                RNS.log(
                    f"TrenchChat [invite]: {actor_hex[:12]}… attempted to remove "
                    f"admin {target.hex()[:12]}… without {MANAGE_ROLES} — ignored",
                    RNS.LOG_WARNING,
                )
                continue
            allowed.append(target)
        return allowed

    def publish_member_list(self, channel_hash_hex: str,
                            add_members: list[bytes] | None = None,
                            remove_members: list[bytes] | None = None,
                            add_admins: list[bytes] | None = None,
                            remove_admins: list[bytes] | None = None,
                            add_owners: list[bytes] | None = None,
                            remove_owners: list[bytes] | None = None):
        """Build, sign, persist, and broadcast an updated member list.

        Mutations are silently dropped if the caller lacks the required permission:
        - remove_members requires KICK, plus MANAGE_ROLES to remove an admin
          and owner status to remove an owner
        - add_admins / remove_admins requires MANAGE_ROLES
        Owner-list mutations (add_owners / remove_owners) are always permitted
        for the channel owner and are not separately gated here.

        A channel inside a server normalises to that server's scope, so every
        existing caller publishes the server's document without knowing about
        servers at all.
        """
        channel_hash_hex = self._storage.scope_for(channel_hash_hex)
        my_hex = self._identity.hash_hex
        if remove_members and not self._storage.has_permission(
            channel_hash_hex, my_hex, KICK
        ):
            RNS.log(
                f"TrenchChat [invite]: {my_hex[:12]}… attempted remove_members "
                f"without {KICK} — ignored",
                RNS.LOG_WARNING,
            )
            remove_members = None
        if remove_members:
            remove_members = self._removable_by(
                channel_hash_hex, my_hex, remove_members) or None
        if (add_admins or remove_admins) and not self._storage.has_permission(
            channel_hash_hex, my_hex, MANAGE_ROLES
        ):
            RNS.log(
                f"TrenchChat [invite]: {my_hex[:12]}… attempted role change "
                f"without {MANAGE_ROLES} — ignored",
                RNS.LOG_WARNING,
            )
            add_admins = None
            remove_admins = None
        if (add_owners or remove_owners) and \
                self._storage.get_role(channel_hash_hex, my_hex) != ROLE_OWNER:
            # The owner set is the root of authority: MANAGE_ROLES is not
            # enough, or any admin could promote themselves to owner and
            # demote the real one.  Receivers enforce this independently in
            # _signer_may_apply.
            RNS.log(
                f"TrenchChat [invite]: {my_hex[:12]}… attempted owner change "
                f"without being an owner — ignored",
                RNS.LOG_WARNING,
            )
            add_owners = None
            remove_owners = None

        existing = self._storage.get_member_list_version(channel_hash_hex)
        if existing:
            old_doc = msgpack.unpackb(existing["document_blob"], raw=True)
            members = list(old_doc[b"members"])
            admins  = list(old_doc[b"admins"])
            owners  = _recover_owners(
                list(old_doc.get(b"owners", [])), admins,
                self._storage.get_channel(channel_hash_hex),
            )
            version = existing["version"] + 1
        else:
            members = [self._identity.hash]
            admins  = [self._identity.hash]
            owners  = [self._identity.hash]
            version = 1

        for m in (add_members or []):
            if m not in members:
                members.append(m)
        for m in (remove_members or []):
            if m in members:
                members.remove(m)
            # trusted_signers is derived from admins/owners, so a kick has
            # to revoke signing authority too.
            if m in admins:
                admins.remove(m)
            if m in owners:
                owners.remove(m)
        for a in (add_admins or []):
            if a not in admins:
                admins.append(a)
        for a in (remove_admins or []):
            if a in admins:
                admins.remove(a)
        for o in (add_owners or []):
            if o not in owners:
                owners.append(o)
        for o in (remove_owners or []):
            if o in owners:
                owners.remove(o)

        is_server = self._storage.is_server(channel_hash_hex)
        if is_server:
            perms = self._storage.get_server_permissions(channel_hash_hex) or None
        else:
            channel = self._storage.get_channel(channel_hash_hex)
            perms = (permissions_from_json(channel["permissions"])
                     if channel and channel["permissions"] else None)

        published_at = time.time()
        # Carry each member's true join time, not just this publish's
        # timestamp -- preserves everyone's real history (including our
        # own, e.g. the channel's actual creation time) for whichever peer
        # processes this document first. Continuing members keep the
        # joined_at already on file (our own local tenure log is
        # authoritative for that); a member appearing for the first time is
        # genuinely joining right now. Uses "is not None" rather than "or"
        # so a legitimately-stored joined_at of exactly 0.0 isn't mistaken
        # for "no data on file" and overwritten with published_at.
        joined_at: dict[bytes, float] = {}
        for m in members:
            existing_joined = self._storage.get_open_tenure_joined_at(channel_hash_hex, m.hex())
            joined_at[m] = existing_joined if existing_joined is not None else published_at

        # Recently departed members, so a brand-new joiner -- who has no
        # prior local state for update_tenure's added/removed diff to act
        # on -- can still validate messages sent by someone who left before
        # they joined. See Storage.get_departed_within.
        departed: dict[bytes, tuple[float, float]] = {
            bytes.fromhex(row["identity_hash"]): (row["joined_at"], row["left_at"])
            for row in self._storage.get_departed_within(
                channel_hash_hex, published_at - SYNC_WINDOW_SECS
            )
        }

        # Block any token still outstanding for a removed member.
        for m in (remove_members or []):
            self._storage.revoke_invite_tokens_for(
                channel_hash_hex, m.hex(), published_at + DEFAULT_TOKEN_TTL
            )

        channels_blob = self._roster_blob(channel_hash_hex) if is_server else None

        doc = self._build_document(channel_hash_hex, members, admins,
                                   version, published_at,
                                   owners=owners, permissions=perms,
                                   joined_at=joined_at, departed=departed,
                                   channels_blob=channels_blob)
        self._accept_document(doc, channel_hash_hex)
        self._broadcast_member_list(channel_hash_hex, doc)

    def _roster_blob(self, server_hash_hex: str) -> bytes:
        """The server's channel roster, filtered by the local CREATE_CHANNEL grant.

        Built from local storage rather than carried forward from the previous
        document, so a publisher who lost a version race re-asserts its own
        channels on the next publish.
        """
        rows = list(self._storage.get_server_channels(server_hash_hex))
        my_hex = self._identity.hash_hex
        if not self._storage.has_permission(server_hash_hex, my_hex, CREATE_CHANNEL):
            # Sender-side mirror of how remove_members is nulled above: a
            # publisher without CREATE_CHANNEL can still publish membership
            # changes, but cannot smuggle a new channel into the roster.
            allowed: set[str] = set()
            existing = self._storage.get_member_list_version(server_hash_hex)
            if existing:
                try:
                    old_doc = msgpack.unpackb(existing["document_blob"], raw=True)
                    if b"channels" in old_doc:
                        for entry in msgpack.unpackb(old_doc[b"channels"], raw=False):
                            allowed.add(entry[0].hex())
                except Exception:
                    pass
            dropped = [r for r in rows if r["hash"] not in allowed]
            if dropped:
                RNS.log(
                    f"TrenchChat [invite]: {my_hex[:12]}… attempted roster additions "
                    f"without {CREATE_CHANNEL} — ignored",
                    RNS.LOG_WARNING,
                )
            rows = [r for r in rows if r["hash"] in allowed]
        return encode_roster(rows)

    def broadcast_permissions(self, channel_hash_hex: str):
        """Publish a new member list doc carrying updated permissions without
        touching the local members table.

        Call this after saving new permissions via
        ``Storage.set_channel_permissions`` to propagate the change to peers.
        The local DB is already correct; only the version counter and the
        broadcast need to happen.

        Requires MANAGE_CHANNEL.  This method previously had no permission
        check of any kind, so it was the one mutation in this class with no
        core-side gate on either the sending or the receiving end.
        """
        # Normalise to the owning scope first, so the permission check below
        # is made against the server for a channel that belongs to one.
        channel_hash_hex = self._storage.scope_for(channel_hash_hex)
        my_hex = self._identity.hash_hex
        if not self._storage.has_permission(channel_hash_hex, my_hex, MANAGE_CHANNEL):
            RNS.log(
                f"TrenchChat [invite]: {my_hex[:12]}… attempted to broadcast "
                f"permissions without {MANAGE_CHANNEL} — ignored",
                RNS.LOG_WARNING,
            )
            return

        existing = self._storage.get_member_list_version(channel_hash_hex)
        is_server = self._storage.is_server(channel_hash_hex)
        channel = self._storage.get_channel(channel_hash_hex)
        if existing:
            old_doc = msgpack.unpackb(existing["document_blob"], raw=True)
            members = list(old_doc[b"members"])
            admins  = list(old_doc[b"admins"])
            owners  = _recover_owners(
                list(old_doc.get(b"owners", [])), admins, channel,
            )
            version = existing["version"] + 1
        else:
            members = [self._identity.hash]
            admins  = [self._identity.hash]
            owners  = [self._identity.hash]
            version = 1

        if is_server:
            perms = self._storage.get_server_permissions(channel_hash_hex) or None
        else:
            perms = (permissions_from_json(channel["permissions"])
                     if channel and channel["permissions"] else None)

        published_at = time.time()
        joined_at: dict[bytes, float] = {}
        for m in members:
            existing_joined = self._storage.get_open_tenure_joined_at(channel_hash_hex, m.hex())
            joined_at[m] = existing_joined if existing_joined is not None else published_at
        departed: dict[bytes, tuple[float, float]] = {
            bytes.fromhex(row["identity_hash"]): (row["joined_at"], row["left_at"])
            for row in self._storage.get_departed_within(
                channel_hash_hex, published_at - SYNC_WINDOW_SECS
            )
        }

        doc = self._build_document(channel_hash_hex, members, admins,
                                   version, published_at,
                                   owners=owners, permissions=perms,
                                   joined_at=joined_at, departed=departed,
                                   channels_blob=(self._roster_blob(channel_hash_hex)
                                                  if is_server else None))

        # Persist the new version so peers cannot replay an older doc, but do
        # NOT call _accept_document — the local members table is already correct
        # and replace_members would wipe display names unnecessarily.
        blob = msgpack.packb(doc, use_bin_type=True)
        self._storage.upsert_member_list_version(
            channel_hash_hex, version, published_at, blob
        )
        self._broadcast_member_list(channel_hash_hex, doc)

    def _broadcast_member_list(self, channel_hash_hex: str, doc: dict):
        blob = msgpack.packb(doc, use_bin_type=True)
        scope = self._storage.get_server(channel_hash_hex)
        is_server = scope is not None
        if scope is None:
            scope = self._storage.get_channel(channel_hash_hex)
        fields = {
            F_MSG_TYPE:        MT_MEMBER_LIST_UPDATE,
            F_CHANNEL_HASH:    bytes.fromhex(channel_hash_hex),
            F_MEMBER_LIST_DOC: blob,
        }
        if is_server:
            fields[F_SCOPE_KIND] = "server"
        if scope:
            fields[F_CHANNEL_NAME]        = scope["name"]
            fields[F_CHANNEL_DESC]        = scope["description"] or ""
            fields[F_CHANNEL_CREATOR]     = scope["creator_hash"]
            fields[F_CHANNEL_PERMISSIONS] = scope["permissions"]
            fields[F_CHANNEL_CREATED_AT]  = scope["created_at"]
        # get_members resolves to the server scope, so this is one document per
        # server member rather than one per member per channel.
        for row in self._storage.get_members(channel_hash_hex):
            dest_hex = row["identity_hash"]
            if dest_hex == self._identity.hash_hex:
                continue
            self._send_raw(dest_hex, fields)

    # --- invite token ---

    def generate_invite_token(self, channel_hash_hex: str,
                               invitee_hash: bytes,
                               ttl: float = DEFAULT_TOKEN_TTL,
                               issued_at: float | None = None) -> tuple[bytes, float]:
        """Returns (token_bytes, expiry_timestamp).

        *issued_at* is bound into the signature so a departure recorded after
        it invalidates the token at every peer. Omitting it produces the
        original unbound payload, which peers predating the field still send.
        """
        expiry = (time.time() if issued_at is None else issued_at) + ttl
        token = _sign(self._identity.rns_identity,
                      _token_payload(invitee_hash, channel_hash_hex, expiry, issued_at))
        return token, expiry

    def send_invite(self, channel_hash_hex: str, invitee_hash_hex: str,
                    ttl: float = DEFAULT_TOKEN_TTL):
        """Generate a token and send it to the invitee via LXMF."""
        RNS.log(f"TrenchChat: sending invite for channel {channel_hash_hex[:12]}… "
                f"to {invitee_hash_hex[:12]}…", RNS.LOG_NOTICE)
        invitee_hash = bytes.fromhex(invitee_hash_hex)
        issued_at = time.time()
        token, expiry = self.generate_invite_token(
            channel_hash_hex, invitee_hash, ttl, issued_at=issued_at)
        fields = {
            F_MSG_TYPE:          MT_INVITE,
            F_CHANNEL_HASH:      bytes.fromhex(channel_hash_hex),
            F_INVITE_TOKEN:      token,
            F_INVITEE_HASH:      invitee_hash,
            F_EXPIRY_TS:         expiry,
            F_ADMIN_HASH:        self._identity.hash,
            F_INVITE_ISSUED_TS:  issued_at,
        }
        # Invite-only scopes are never announced, so the invitee has no local
        # record yet -- without the name here, the MT_INVITE handler has
        # nothing to show but the raw hash.
        server = self._storage.get_server(channel_hash_hex)
        if server:
            fields[F_SCOPE_KIND]   = "server"
            fields[F_CHANNEL_NAME] = server["name"]
        else:
            channel = self._storage.get_channel(channel_hash_hex)
            if channel:
                fields[F_CHANNEL_NAME] = channel["name"]
        self._send_raw(invitee_hash_hex, fields)

    def send_join_request(self, channel_hash_hex: str, token: bytes,
                          expiry: float, admin_hash_hex: str,
                          issued_at: float | None = None):
        """Send a join request to an admin using a received invite token.

        *issued_at* is part of what the token signs over, so it has to travel
        with it. Callers that hold only the token read it back from the stored
        invite; a token with none predates the field.
        """
        RNS.log(f"TrenchChat: sending join request for channel {channel_hash_hex[:12]}… "
                f"to admin {admin_hash_hex[:12]}…", RNS.LOG_NOTICE)
        if issued_at is None:
            issued_at = self._storage.get_pending_invite_issued_at(channel_hash_hex)
        # Anchors the first member list document we receive for this scope to
        # this admin; see _validate_document. Durable, so it survives a restart
        # between accepting the invite and the document arriving.
        self._storage.record_accepted_invite(
            channel_hash_hex, admin_hash_hex, expiry
        )
        self._storage.clear_pending_invite(channel_hash_hex)
        request = {
            F_MSG_TYPE:     MT_JOIN_REQUEST,
            F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
            F_INVITE_TOKEN: token,
            F_INVITEE_HASH: self._identity.hash,
            F_EXPIRY_TS:    expiry,
            F_ADMIN_HASH:   bytes.fromhex(admin_hash_hex),
        }
        if issued_at is not None:
            request[F_INVITE_ISSUED_TS] = issued_at
        self._send_raw(admin_hash_hex, request)

    def _verify_invite_token(self, token: bytes, invitee_hash: bytes,
                              channel_hash_hex: str, expiry: float,
                              admin_hash: bytes,
                              issued_at: float | None = None) -> bool:
        """Verify a token against exactly the payload form the sender used.

        A token carrying no issue time is checked against the original
        unbound payload, so stripping the field off a bound one fails rather
        than downgrading it.
        """
        if time.time() > expiry:
            return False
        if admin_hash == self._identity.hash:
            admin_identity = self._identity.rns_identity
        else:
            admin_delivery_hash = RNS.Destination.hash(admin_hash, "lxmf", "delivery")
            admin_identity = RNS.Identity.recall(admin_delivery_hash)
        if admin_identity is None:
            RNS.log(f"TrenchChat [invite]: cannot verify token — admin identity "
                    f"{admin_hash.hex()[:12]}… not known", RNS.LOG_WARNING)
            return False
        if not self._storage.has_permission(channel_hash_hex, admin_hash.hex(), INVITE):
            return False
        payload = _token_payload(invitee_hash, channel_hash_hex, expiry, issued_at)
        return _verify(admin_identity, payload, token)

    # --- inbound handler ---

    def _on_lxmf_message(self, message: LXMF.LXMessage):
        fields = message.fields or {}
        msg_type = fields.get(F_MSG_TYPE)
        if msg_type is None:
            return
        if isinstance(msg_type, bytes):
            msg_type = msg_type.decode(errors="replace")

        # Presence beacons and goodbyes carry no channel hash by design; they're
        # handled entirely by PresenceManager.record_inbound via the router's
        # delivery callback, so there's nothing for invite.py to do with them.
        if msg_type in (MT_PRESENCE, MT_GOODBYE):
            return

        RNS.log(f"TrenchChat [invite]: received control message type={msg_type!r}",
                RNS.LOG_DEBUG)

        channel_hash_bytes = fields.get(F_CHANNEL_HASH)
        if not channel_hash_bytes:
            RNS.log("TrenchChat [invite]: control message missing channel hash, dropping",
                    RNS.LOG_WARNING)
            return
        channel_hash_hex = channel_hash_bytes.hex() \
            if isinstance(channel_hash_bytes, bytes) else str(channel_hash_bytes)

        if msg_type == MT_JOIN_REQUEST:
            RNS.log(f"TrenchChat [invite]: join request received for channel {channel_hash_hex[:12]}…",
                    RNS.LOG_NOTICE)
            sender_identity = (RNS.Identity.recall(message.source_hash)
                               if message.source_hash else None)
            sender_hex = sender_identity.hash.hex() if sender_identity else ""
            self._handle_join_request(fields, channel_hash_hex, sender_hex)

        elif msg_type == MT_MEMBER_LIST_UPDATE:
            blob = fields.get(F_MEMBER_LIST_DOC)
            if blob:
                try:
                    doc = unpack_wire(blob, raw=True)
                    doc_clean = {
                        "channel_hash": doc[b"channel_hash"],
                        "version":      doc[b"version"],
                        "published_at": doc[b"published_at"],
                        "members":      list(doc[b"members"]),
                        "admins":       list(doc[b"admins"]),
                        "signatures":   dict(doc[b"signatures"]),
                    }
                    if b"owners" in doc:
                        doc_clean["owners"] = list(doc[b"owners"])
                    if b"permissions" in doc:
                        doc_clean["permissions"] = doc[b"permissions"]
                    if b"joined_at" in doc:
                        doc_clean["joined_at"] = dict(doc[b"joined_at"])
                    if b"departed" in doc:
                        doc_clean["departed"] = {
                            k: tuple(v) for k, v in doc[b"departed"].items()
                        }
                    if b"channels" in doc:
                        doc_clean["channels"] = doc[b"channels"]

                    # Anchor check first, and strictly before the server row is
                    # created below: _can_anchor consults the stored creator, so
                    # materialising the server from this message's own unsigned
                    # fields would let an unanchored document mint its own
                    # trust anchor.
                    if not self._can_anchor(channel_hash_hex):
                        self._hold_for_confirmation(
                            doc_clean, channel_hash_hex, fields
                        )
                        return

                    # The server row must exist before _accept_document so
                    # is_server() is true when the permissions branch runs and
                    # the roster is materialised.
                    if _decode(fields.get(F_SCOPE_KIND, "")) == "server":
                        self._materialise_server(fields, channel_hash_hex)

                    accepted = self._accept_document(doc_clean, channel_hash_hex)
                    RNS.log(f"TrenchChat [invite]: member list update v{doc_clean['version']} "
                            f"for {channel_hash_hex[:12]}… — {'accepted' if accepted else 'rejected'}",
                            RNS.LOG_NOTICE)

                    # If channel metadata was included and we don't know this channel yet,
                    # upsert it and subscribe so it appears in the sidebar.
                    if accepted and not self._storage.is_server(channel_hash_hex):
                        channel_name = fields.get(F_CHANNEL_NAME)
                        if channel_name and not self._storage.get_channel(channel_hash_hex):
                            if isinstance(channel_name, bytes):
                                channel_name = channel_name.decode("utf-8", errors="replace")
                            desc = fields.get(F_CHANNEL_DESC, b"")
                            if isinstance(desc, bytes):
                                desc = desc.decode("utf-8", errors="replace")
                            creator = fields.get(F_CHANNEL_CREATOR, b"")
                            if isinstance(creator, bytes):
                                creator = creator.decode("utf-8", errors="replace")
                            perms_field = fields.get(F_CHANNEL_PERMISSIONS)
                            if perms_field is None:
                                perms_field = fields.get(F_CHANNEL_ACCESS, b"invite")
                            if isinstance(perms_field, bytes):
                                perms_field = perms_field.decode("utf-8", errors="replace")
                            created_at = fields.get(F_CHANNEL_CREATED_AT, time.time())
                            # These fields are unsigned, and creator_hash goes
                            # on to serve as a trusted-signer fallback for
                            # later documents. Bind it the same way roster
                            # entries and servers are bound, so the hash has
                            # to be one this creator could have minted.
                            if not self._creator_binds(channel_hash_hex, creator,
                                                       channel_name):
                                return
                            self._storage.upsert_channel(
                                hash=channel_hash_hex,
                                name=channel_name,
                                description=desc,
                                creator_hash=creator,
                                permissions=perms_field,
                                created_at=created_at,
                            )
                            self._storage.subscribe(channel_hash_hex)
                            RNS.log(f"TrenchChat [invite]: auto-joined channel "
                                    f"{channel_name!r} ({channel_hash_hex[:12]}…)",
                                    RNS.LOG_NOTICE)
                            for cb in self._channel_callbacks:
                                try:
                                    cb(channel_hash_hex, channel_name)
                                except Exception as e:
                                    RNS.log(f"TrenchChat: channel callback error: {e}",
                                            RNS.LOG_ERROR)
                except Exception as e:
                    RNS.log(f"TrenchChat: member list update parse error: {e}",
                            RNS.LOG_WARNING)

        elif msg_type == MT_INVITE:
            token        = fields.get(F_INVITE_TOKEN)
            expiry       = fields.get(F_EXPIRY_TS)
            admin_hash   = fields.get(F_ADMIN_HASH)
            RNS.log(f"TrenchChat [invite]: invite received for channel {channel_hash_hex[:12]}… "
                    f"token={'present' if token else 'MISSING'} "
                    f"expiry={'present' if expiry else 'MISSING'} "
                    f"admin={'present' if admin_hash else 'MISSING'}",
                    RNS.LOG_NOTICE)
            if token and expiry and admin_hash:
                admin_hex = admin_hash.hex() if isinstance(admin_hash, bytes) else str(admin_hash)
                self._invite_scope_kinds[channel_hash_hex] = (
                    _decode(fields.get(F_SCOPE_KIND, "")) or "channel"
                )
                channel_name = _decode(fields.get(F_CHANNEL_NAME, ""))
                if not channel_name:
                    channel = self._storage.get_channel(channel_hash_hex)
                    channel_name = channel["name"] if channel else channel_hash_hex[:12]
                self._storage.record_pending_invite(
                    channel_hash_hex, channel_name, token, float(expiry), admin_hex,
                    issued_at=wire_timestamp(fields.get(F_INVITE_ISSUED_TS)) or 0.0,
                )
                for cb in self._invite_callbacks:
                    try:
                        cb(channel_hash_hex, channel_name, token, expiry, admin_hex)
                    except Exception as e:
                        RNS.log(f"TrenchChat: invite callback error: {e}", RNS.LOG_ERROR)
            else:
                RNS.log("TrenchChat [invite]: invite message missing required fields, dropping",
                        RNS.LOG_WARNING)

    def _handle_join_request(self, fields: dict, channel_hash_hex: str,
                             sender_hex: str = ""):
        token        = fields.get(F_INVITE_TOKEN)
        invitee_hash = fields.get(F_INVITEE_HASH)
        expiry       = fields.get(F_EXPIRY_TS)
        admin_hash   = fields.get(F_ADMIN_HASH)
        issued_at    = wire_timestamp(fields.get(F_INVITE_ISSUED_TS)) \
            if F_INVITE_ISSUED_TS in fields else None

        if not all([token, invitee_hash, expiry, admin_hash]):
            return
        if not isinstance(invitee_hash, bytes):
            return

        if not self._storage.has_permission(channel_hash_hex, self._identity.hash_hex, INVITE):
            return

        # The request must come from the identity the token was issued to.
        if sender_hex and sender_hex != invitee_hash.hex():
            RNS.log(
                f"TrenchChat [invite]: join request for "
                f"{invitee_hash.hex()[:12]}… submitted by "
                f"{sender_hex[:12]}… — rejected",
                RNS.LOG_WARNING,
            )
            return

        if not self._verify_invite_token(token, invitee_hash, channel_hash_hex,
                                         expiry, admin_hash, issued_at):
            RNS.log("TrenchChat: invalid or expired invite token rejected",
                    RNS.LOG_WARNING)
            return

        invitee_hex = invitee_hash.hex()

        # Both sources say the same thing -- "this identity was removed at
        # time T" -- so a token issued at or before T is dead. The revocation
        # sentinel is written only by the peer that performed the kick, which
        # is why the departure matters: it rides in the signed member list
        # document, so a second admin can refuse a replayed token instead of
        # publishing the sender straight back in. A token carrying no issue
        # time predates the field and cannot be dated, so it loses.
        removed_at = max(
            self._storage.get_last_departure_at(channel_hash_hex, invitee_hex) or 0.0,
            self._storage.invite_revoked_at(channel_hash_hex, invitee_hex) or 0.0,
        )
        if removed_at and removed_at >= (issued_at or 0.0):
            RNS.log(
                f"TrenchChat [invite]: refusing invite token for "
                f"{invitee_hex[:12]}… issued before they were removed from "
                f"{channel_hash_hex[:12]}… — a fresh invite is required",
                RNS.LOG_WARNING,
            )
            return

        # The insert is the atomic claim; a replay cannot also win it.
        token_hash = hashlib.sha256(token).hexdigest()
        if not self._storage.spend_invite_token(
            channel_hash_hex, token_hash, invitee_hex, float(expiry)
        ):
            RNS.log(
                f"TrenchChat [invite]: invite token already redeemed for "
                f"{invitee_hex[:12]}… — replay rejected",
                RNS.LOG_WARNING,
            )
            return

        self.publish_member_list(channel_hash_hex,
                                 add_members=[invitee_hash])

    # --- helpers ---

    def flush_pending(self, dest_hex: str) -> int:
        """Re-send control messages held while this peer had no known path."""
        return self._retry.flush(dest_hex, self._send_raw)

    def _send_raw(self, dest_hex: str, fields: dict) -> bool:
        """Send a control message. Returns False if it had to be queued instead."""
        msg_type = fields.get(F_MSG_TYPE, "unknown")
        try:
            identity_hash = bytes.fromhex(dest_hex)

            # Compute the LXMF delivery destination hash from the identity hash.
            # RNS.Identity.recall() takes a *destination* hash, not an identity hash.
            delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")

            dest_identity = RNS.Identity.recall(delivery_dest_hash)

            if dest_identity is None:
                RNS.Transport.request_path(delivery_dest_hash)
                self._retry.queue(dest_hex, fields)
                RNS.log(f"TrenchChat [invite]: {msg_type!r} to {dest_hex[:12]}… "
                        f"held — identity not known, path requested",
                        RNS.LOG_WARNING)
                return False

            dest = RNS.Destination(
                dest_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "lxmf",
                "delivery",
            )
            lxm = LXMF.LXMessage(
                dest,
                self._router.delivery_destination,
                "",
                desired_method=LXMF.LXMessage.DIRECT,
            )
            lxm.fields = fields
            RNS.log(f"TrenchChat [invite]: queuing {msg_type!r} → {dest_hex[:12]}…",
                    RNS.LOG_NOTICE)
            self._router.send(lxm)
            return True
        except Exception as e:
            RNS.log(f"TrenchChat: invite send error ({msg_type}): {e}", RNS.LOG_WARNING)
            return False
