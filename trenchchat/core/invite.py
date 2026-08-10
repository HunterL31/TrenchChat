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

from trenchchat.core.identity import Identity
from trenchchat.core.permissions import (
    INVITE, KICK, MANAGE_CHANNEL, MANAGE_ROLES,
    ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER,
    is_valid_permissions, permissions_from_json, permissions_to_json,
)
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MSG_TYPE,
    F_INVITE_TOKEN, F_INVITEE_HASH, F_EXPIRY_TS, F_ADMIN_HASH,
    F_MEMBER_LIST_DOC, F_CHANNEL_NAME, F_CHANNEL_DESC,
    F_CHANNEL_CREATOR, F_CHANNEL_ACCESS, F_CHANNEL_CREATED_AT,
    F_CHANNEL_PERMISSIONS,
    MT_JOIN_REQUEST, MT_MEMBER_LIST_UPDATE, MT_INVITE,
    unpack_wire,
)
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router

DEFAULT_TOKEN_TTL = 7 * 24 * 3600  # 7 days


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


def _signed_payload(channel_hash: bytes, version: int, published_at: float,
                    members: list[bytes], admins: list[bytes],
                    owners: list[bytes] | None = None,
                    permissions_blob: bytes = b"",
                    joined_at: dict[bytes, float] | None = None) -> bytes:
    """Build the payload that gets signed.

    If *owners* is provided the v2 format is used (includes owners and
    permissions_blob).  Otherwise the v1 format is used for backward compat.
    If *joined_at* is also provided (v3, requires v2), each member's true
    historical join time is bound into the signature too, so a recipient
    can trust it came from the signing admin and wasn't added or altered
    by whoever relayed the document.
    """
    items: list = [channel_hash, version, published_at,
                   sorted(members), sorted(admins)]
    if owners is not None:
        items.extend([sorted(owners), permissions_blob])
        if joined_at is not None:
            items.append(sorted(joined_at.items()))
    return msgpack.packb(items, use_bin_type=True)


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
        router.add_delivery_callback(self._on_lxmf_message)

    def add_invite_callback(self, callback):
        """callback(channel_hash_hex, channel_name, token, expiry, admin_hash_hex)"""
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
                        joined_at: dict[bytes, float] | None = None) -> dict:
        if owners is None:
            owners = []
        if joined_at is None:
            joined_at = {}
        permissions_blob = (msgpack.packb(permissions, use_bin_type=True)
                            if permissions else b"")
        payload = _signed_payload(
            bytes.fromhex(channel_hash_hex), version, published_at,
            members, admins, owners, permissions_blob, joined_at,
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
            "signatures":   {self._identity.hash: sig},
        }
        if joined_at is not None:
            doc["joined_at"] = joined_at
        return doc

    def _validate_document(self, doc: dict, channel_hash_hex: str) -> bytes | None:
        """Return the hash of the trusted signer that validated, or None.

        The signer must be recognised as an admin or owner in the *previously
        stored* member list for this channel (or be the channel creator when no
        stored list exists yet).  Checking only the incoming doc's own admin/owner
        lists would allow a malicious peer to grant themselves signing authority
        by simply listing themselves as an admin in the doc they craft.

        The signer identity is returned rather than a bare bool so the caller
        can check what that specific signer is actually permitted to change --
        being a trusted signer authorises signing, not every mutation.
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
            channel = self._storage.get_channel(channel_hash_hex)
            trusted_signers = set()
            if channel and channel["creator_hash"]:
                try:
                    trusted_signers = {bytes.fromhex(channel["creator_hash"])}
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
                # Last resort: the document's own signers, narrowed to
                # documents that name us. Retained because an admin adding a
                # member unilaterally produces a document the recipient has no
                # other way to anchor. See docs/security-improvements.md.
                if self._identity.hash not in doc.get("members", []):
                    RNS.log(
                        f"TrenchChat [invite]: no trust anchor for member list "
                        f"doc on unknown channel {channel_hash_hex[:12]}… and "
                        f"it does not name us — rejected",
                        RNS.LOG_WARNING,
                    )
                    return None
                RNS.log(
                    f"TrenchChat [invite]: accepting first member list doc for "
                    f"{channel_hash_hex[:12]}… on its own authority — no stored "
                    f"channel record or accepted invite to anchor it",
                    RNS.LOG_WARNING,
                )
                trusted_signers = set(admins_in_doc) | set(owners_in_doc)

        is_v2 = "owners" in doc
        joined_at_in_doc = doc.get("joined_at") if "joined_at" in doc else None
        if is_v2:
            payload = _signed_payload(
                doc["channel_hash"], doc["version"], doc["published_at"],
                doc["members"], admins_in_doc,
                owners_in_doc, doc.get("permissions", b""),
                joined_at_in_doc,
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
        except Exception:
            return True

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
        if old_members - new_members:
            if not self._storage.has_permission(channel_hash_hex, signer_hex, KICK):
                return _deny("removing members", KICK)

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

        return True

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

    def _accept_document_locked(self, doc: dict, channel_hash_hex: str) -> bool:
        """
        Apply acceptance rules. Returns True if accepted.
        Rules (in order):
          1. doc["channel_hash"] must match the expected channel.
          2. At least one valid admin signature.
          3. version > local_version  → accept.
          4. version == local_version, higher published_at → accept.
          5. version == local_version, same published_at, lower admin hash → accept.
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
                    self._storage.set_channel_permissions(channel_hash_hex, perms)
                else:
                    RNS.log(
                        f"TrenchChat [invite]: rejecting malformed permissions "
                        f"for {channel_hash_hex[:12]}…",
                        RNS.LOG_WARNING,
                    )

        return True

    # --- publish a new member list (admin action) ---

    def publish_member_list(self, channel_hash_hex: str,
                            add_members: list[bytes] | None = None,
                            remove_members: list[bytes] | None = None,
                            add_admins: list[bytes] | None = None,
                            remove_admins: list[bytes] | None = None,
                            add_owners: list[bytes] | None = None,
                            remove_owners: list[bytes] | None = None):
        """Build, sign, persist, and broadcast an updated member list.

        Mutations are silently dropped if the caller lacks the required permission:
        - remove_members requires KICK
        - add_admins / remove_admins requires MANAGE_ROLES
        Owner-list mutations (add_owners / remove_owners) are always permitted
        for the channel owner and are not separately gated here.
        """
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

        # Block any token still outstanding for a removed member.
        for m in (remove_members or []):
            self._storage.revoke_invite_tokens_for(
                channel_hash_hex, m.hex(), published_at + DEFAULT_TOKEN_TTL
            )

        doc = self._build_document(channel_hash_hex, members, admins,
                                   version, published_at,
                                   owners=owners, permissions=perms,
                                   joined_at=joined_at)
        self._accept_document(doc, channel_hash_hex)
        self._broadcast_member_list(channel_hash_hex, doc)

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
        my_hex = self._identity.hash_hex
        if not self._storage.has_permission(channel_hash_hex, my_hex, MANAGE_CHANNEL):
            RNS.log(
                f"TrenchChat [invite]: {my_hex[:12]}… attempted to broadcast "
                f"permissions without {MANAGE_CHANNEL} — ignored",
                RNS.LOG_WARNING,
            )
            return

        existing = self._storage.get_member_list_version(channel_hash_hex)
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

        perms = (permissions_from_json(channel["permissions"])
                 if channel and channel["permissions"] else None)

        published_at = time.time()
        joined_at: dict[bytes, float] = {}
        for m in members:
            existing_joined = self._storage.get_open_tenure_joined_at(channel_hash_hex, m.hex())
            joined_at[m] = existing_joined if existing_joined is not None else published_at

        doc = self._build_document(channel_hash_hex, members, admins,
                                   version, published_at,
                                   owners=owners, permissions=perms,
                                   joined_at=joined_at)

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
        channel = self._storage.get_channel(channel_hash_hex)
        fields = {
            F_MSG_TYPE:        MT_MEMBER_LIST_UPDATE,
            F_CHANNEL_HASH:    bytes.fromhex(channel_hash_hex),
            F_MEMBER_LIST_DOC: blob,
        }
        if channel:
            fields[F_CHANNEL_NAME]        = channel["name"]
            fields[F_CHANNEL_DESC]        = channel["description"] or ""
            fields[F_CHANNEL_CREATOR]     = channel["creator_hash"]
            fields[F_CHANNEL_PERMISSIONS] = channel["permissions"]
            fields[F_CHANNEL_CREATED_AT]  = channel["created_at"]
        for row in self._storage.get_members(channel_hash_hex):
            dest_hex = row["identity_hash"]
            if dest_hex == self._identity.hash_hex:
                continue
            self._send_raw(dest_hex, fields)

    # --- invite token ---

    def generate_invite_token(self, channel_hash_hex: str,
                               invitee_hash: bytes,
                               ttl: float = DEFAULT_TOKEN_TTL) -> tuple[bytes, float]:
        """Returns (token_bytes, expiry_timestamp)."""
        expiry = time.time() + ttl
        payload = (invitee_hash
                   + bytes.fromhex(channel_hash_hex)
                   + struct.pack(">d", expiry))
        token = _sign(self._identity.rns_identity, payload)
        return token, expiry

    def send_invite(self, channel_hash_hex: str, invitee_hash_hex: str,
                    ttl: float = DEFAULT_TOKEN_TTL):
        """Generate a token and send it to the invitee via LXMF."""
        RNS.log(f"TrenchChat: sending invite for channel {channel_hash_hex[:12]}… "
                f"to {invitee_hash_hex[:12]}…", RNS.LOG_NOTICE)
        invitee_hash = bytes.fromhex(invitee_hash_hex)
        token, expiry = self.generate_invite_token(channel_hash_hex, invitee_hash, ttl)
        fields = {
            F_MSG_TYPE:     MT_INVITE,
            F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
            F_INVITE_TOKEN: token,
            F_INVITEE_HASH: invitee_hash,
            F_EXPIRY_TS:    expiry,
            F_ADMIN_HASH:   self._identity.hash,
        }
        # Invite-only channels are never announced, so the invitee has no
        # local record of this channel yet -- without its name here, the
        # MT_INVITE handler has nothing to show but the raw hash.
        channel = self._storage.get_channel(channel_hash_hex)
        if channel:
            fields[F_CHANNEL_NAME] = channel["name"]
        self._send_raw(invitee_hash_hex, fields)

    def send_join_request(self, channel_hash_hex: str, token: bytes,
                          expiry: float, admin_hash_hex: str):
        """Send a join request to an admin using a received invite token."""
        RNS.log(f"TrenchChat: sending join request for channel {channel_hash_hex[:12]}… "
                f"to admin {admin_hash_hex[:12]}…", RNS.LOG_NOTICE)
        # Anchors the first member list document we receive for this channel
        # to this admin; see _validate_document.
        self._storage.record_accepted_invite(
            channel_hash_hex, admin_hash_hex, expiry
        )
        self._send_raw(admin_hash_hex, {
            F_MSG_TYPE:     MT_JOIN_REQUEST,
            F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
            F_INVITE_TOKEN: token,
            F_INVITEE_HASH: self._identity.hash,
            F_EXPIRY_TS:    expiry,
            F_ADMIN_HASH:   bytes.fromhex(admin_hash_hex),
        })

    def _verify_invite_token(self, token: bytes, invitee_hash: bytes,
                              channel_hash_hex: str, expiry: float,
                              admin_hash: bytes) -> bool:
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
        payload = (invitee_hash
                   + bytes.fromhex(channel_hash_hex)
                   + struct.pack(">d", expiry))
        return _verify(admin_identity, payload, token)

    # --- inbound handler ---

    def _on_lxmf_message(self, message: LXMF.LXMessage):
        fields = message.fields or {}
        msg_type = fields.get(F_MSG_TYPE)
        if msg_type is None:
            return
        if isinstance(msg_type, bytes):
            msg_type = msg_type.decode(errors="replace")

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
                    accepted = self._accept_document(doc_clean, channel_hash_hex)
                    RNS.log(f"TrenchChat [invite]: member list update v{doc_clean['version']} "
                            f"for {channel_hash_hex[:12]}… — {'accepted' if accepted else 'rejected'}",
                            RNS.LOG_NOTICE)

                    # If channel metadata was included and we don't know this channel yet,
                    # upsert it and subscribe so it appears in the sidebar.
                    if accepted:
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
                channel_name = fields.get(F_CHANNEL_NAME)
                if isinstance(channel_name, bytes):
                    channel_name = channel_name.decode("utf-8", errors="replace")
                if not channel_name:
                    channel = self._storage.get_channel(channel_hash_hex)
                    channel_name = channel["name"] if channel else channel_hash_hex[:12]
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
                                         expiry, admin_hash):
            RNS.log("TrenchChat: invalid or expired invite token rejected",
                    RNS.LOG_WARNING)
            return

        invitee_hex = invitee_hash.hex()

        if self._storage.are_invite_tokens_revoked_for(channel_hash_hex, invitee_hex):
            RNS.log(
                f"TrenchChat [invite]: refusing revoked invite token for "
                f"{invitee_hex[:12]}… on {channel_hash_hex[:12]}…",
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

    def _send_raw(self, dest_hex: str, fields: dict):
        msg_type = fields.get(F_MSG_TYPE, "unknown")
        try:
            identity_hash = bytes.fromhex(dest_hex)

            # Compute the LXMF delivery destination hash from the identity hash.
            # RNS.Identity.recall() takes a *destination* hash, not an identity hash.
            delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")

            dest_identity = RNS.Identity.recall(delivery_dest_hash)

            if dest_identity is None:
                RNS.Transport.request_path(delivery_dest_hash)
                RNS.log(f"TrenchChat [invite]: cannot deliver {msg_type!r} to "
                        f"{dest_hex[:12]}… — identity not known, path requested",
                        RNS.LOG_WARNING)
                return

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
        except Exception as e:
            RNS.log(f"TrenchChat: invite send error ({msg_type}): {e}", RNS.LOG_WARNING)
