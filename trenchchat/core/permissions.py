"""
Role and permission constants for TrenchChat channels.

This module is the single source of truth for role names, permission names,
and default permission presets.  It has no local imports so it can be safely
imported by any layer without circular dependencies.
"""

import json
from typing import Any

import RNS

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"

ALL_ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER)

# Promotion order: higher index = more privileged.
_ROLE_RANK = {ROLE_MEMBER: 0, ROLE_ADMIN: 1, ROLE_OWNER: 2}


def role_rank(role: str) -> int:
    return _ROLE_RANK.get(role, -1)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

SEND_MESSAGE = "send_message"
INVITE = "invite"
KICK = "kick"
MANAGE_ROLES = "manage_roles"
MANAGE_CHANNEL = "manage_channel"
CREATE_CHANNEL = "create_channel"
FULL_SYNC = "full_sync"
VOICE_CHAT = "voice_chat"

# Permissions a plain member may never hold, whatever a permissions blob says.
#
# Removing someone from the member list strips every permission they had, so
# KICK is the authority to unmake other people's; granting it to the base role
# makes every member able to do that to every other. MANAGE_ROLES rides along
# because it is the way to grant yourself KICK: a member who can edit the admin
# list can promote themselves and take it that way, so restricting one without
# the other closes nothing.
ADMIN_ONLY_PERMISSIONS = (KICK, MANAGE_ROLES)

ALL_PERMISSIONS = (SEND_MESSAGE, INVITE, KICK, MANAGE_ROLES, MANAGE_CHANNEL,
                   CREATE_CHANNEL, FULL_SYNC, VOICE_CHAT)

# ---------------------------------------------------------------------------
# Channel-level flags
# ---------------------------------------------------------------------------

FLAG_OPEN_JOIN = "open_join"
FLAG_DISCOVERABLE = "discoverable"

# ---------------------------------------------------------------------------
# Default presets
# ---------------------------------------------------------------------------

PRESET_PRIVATE: dict[str, Any] = {
    FLAG_OPEN_JOIN: False,
    FLAG_DISCOVERABLE: False,
    ROLE_ADMIN: [SEND_MESSAGE, INVITE, KICK, MANAGE_ROLES, VOICE_CHAT],
    ROLE_MEMBER: [SEND_MESSAGE, VOICE_CHAT],
}

PRESET_OPEN: dict[str, Any] = {
    FLAG_OPEN_JOIN: True,
    FLAG_DISCOVERABLE: True,
    ROLE_ADMIN: [SEND_MESSAGE, INVITE, KICK, MANAGE_ROLES, VOICE_CHAT],
    ROLE_MEMBER: [SEND_MESSAGE, INVITE, VOICE_CHAT],
}

PRESET_SERVER: dict[str, Any] = {
    FLAG_OPEN_JOIN: False,
    FLAG_DISCOVERABLE: False,
    ROLE_ADMIN: [SEND_MESSAGE, INVITE, KICK, MANAGE_ROLES, CREATE_CHANNEL, VOICE_CHAT],
    ROLE_MEMBER: [SEND_MESSAGE, VOICE_CHAT],
}

PRESETS = {
    "private": PRESET_PRIVATE,
    "open": PRESET_OPEN,
    "server": PRESET_SERVER,
}

DEFAULT_PRESET = "private"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def permissions_to_json(perms: dict) -> str:
    """Serialise a permissions dict, dropping grants a role may never hold.

    Sanitised on the way out as well as the way in, so a disallowed grant is
    never stored locally nor broadcast to anyone else.
    """
    return json.dumps(sanitise_permissions(perms), sort_keys=True)


def is_valid_permissions(perms: object) -> bool:
    """True if *perms* has the shape this module expects."""
    if not isinstance(perms, dict):
        return False
    for flag in (FLAG_OPEN_JOIN, FLAG_DISCOVERABLE):
        if flag in perms and not isinstance(perms[flag], bool):
            return False
    for role in (ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER):
        if role not in perms:
            continue
        granted = perms[role]
        if not isinstance(granted, list):
            return False
        if not all(isinstance(p, str) for p in granted):
            return False
    return True


def grantable_to(role: str) -> tuple[str, ...]:
    """Permissions that may be granted to *role* at all."""
    if role == ROLE_MEMBER:
        return tuple(p for p in ALL_PERMISSIONS if p not in ADMIN_ONLY_PERMISSIONS)
    return ALL_PERMISSIONS


def offered_permissions(perms: dict, role: str) -> tuple[str, ...]:
    """Permissions worth showing for *role* on a channel with *perms*.

    Narrower than grantable_to: FULL_SYNC decides how much history a member
    may pull, and an open-join channel serves its history to any subscriber,
    so offering the toggle there presents a privacy control that is not one.
    """
    offered = grantable_to(role)
    if is_open_join(perms):
        offered = tuple(p for p in offered if p != FULL_SYNC)
    return offered


def sanitise_permissions(perms: dict) -> dict:
    """Drop grants a role may never hold.

    Applied on every read rather than only where permissions are edited: a
    blob reaches us from a signed member list document as well as from local
    storage, and a signature proves who wrote it, not that what it says is
    allowed.
    """
    granted = perms.get(ROLE_MEMBER)
    if not isinstance(granted, list):
        return perms
    allowed = [p for p in granted if p not in ADMIN_ONLY_PERMISSIONS]
    if len(allowed) == len(granted):
        return perms
    RNS.log(
        f"TrenchChat [permissions]: dropping "
        f"{sorted(set(granted) - set(allowed))} from the member role",
        RNS.LOG_WARNING,
    )
    cleaned = dict(perms)
    cleaned[ROLE_MEMBER] = allowed
    return cleaned


def permissions_from_json(blob: str) -> dict:
    """Parse a stored permissions blob, falling back to the private preset.

    Must not raise: every read path calls this, including one on the GUI
    thread outside any try/except. The fallback is the most restrictive
    preset, so a bad blob fails closed.
    """
    try:
        perms = json.loads(blob)
    except (TypeError, ValueError):
        RNS.log(
            "TrenchChat [permissions]: malformed permissions blob — "
            "falling back to the private preset",
            RNS.LOG_WARNING,
        )
        return dict(PRESET_PRIVATE)
    if not is_valid_permissions(perms):
        RNS.log(
            "TrenchChat [permissions]: permissions blob failed validation — "
            "falling back to the private preset",
            RNS.LOG_WARNING,
        )
        return dict(PRESET_PRIVATE)
    return sanitise_permissions(perms)


def has_permission(perms: dict, role: str, permission: str) -> bool:
    """Check whether *role* grants *permission* under the given config.

    The owner role always has every permission.
    """
    if role == ROLE_OWNER:
        return True
    return permission in perms.get(role, [])


def is_open_join(perms: dict) -> bool:
    return bool(perms.get(FLAG_OPEN_JOIN, False))


def is_discoverable(perms: dict) -> bool:
    return bool(perms.get(FLAG_DISCOVERABLE, True))
