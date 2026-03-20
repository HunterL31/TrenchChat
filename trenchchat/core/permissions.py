"""
Role and permission constants for TrenchChat channels.

This module is the single source of truth for role names, permission names,
and default permission presets.  It has no local imports so it can be safely
imported by any layer without circular dependencies.
"""

import json
from typing import Any

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

ALL_PERMISSIONS = (SEND_MESSAGE, INVITE, KICK, MANAGE_ROLES, MANAGE_CHANNEL)

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

PRESETS = {
    "private": PRESET_PRIVATE,
    "open": PRESET_OPEN,
}

DEFAULT_PRESET = "private"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def permissions_to_json(perms: dict) -> str:
    return json.dumps(perms, sort_keys=True)


def permissions_from_json(blob: str) -> dict:
    return json.loads(blob)


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
