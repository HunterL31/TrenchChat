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

# Promotion order — higher index = more privileged.
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

ALL_PERMISSIONS = (SEND_MESSAGE, INVITE, KICK, MANAGE_ROLES, MANAGE_CHANNEL,
                   CREATE_CHANNEL, FULL_SYNC)

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
    ROLE_ADMIN: [SEND_MESSAGE, INVITE, KICK, MANAGE_ROLES],
    ROLE_MEMBER: [SEND_MESSAGE],
}

PRESET_OPEN: dict[str, Any] = {
    FLAG_OPEN_JOIN: True,
    FLAG_DISCOVERABLE: True,
    ROLE_ADMIN: [SEND_MESSAGE, INVITE, KICK, MANAGE_ROLES],
    ROLE_MEMBER: [SEND_MESSAGE, INVITE],
}

PRESET_SERVER: dict[str, Any] = {
    FLAG_OPEN_JOIN: False,
    FLAG_DISCOVERABLE: False,
    ROLE_ADMIN: [SEND_MESSAGE, INVITE, KICK, MANAGE_ROLES, CREATE_CHANNEL],
    ROLE_MEMBER: [SEND_MESSAGE],
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
    return json.dumps(perms, sort_keys=True)


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
    return perms


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
