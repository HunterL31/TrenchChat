"""
Tests for the members-list label resolution.

member_label is a pure function so it can be exercised without a
QApplication; the suite instantiates no Qt widgets anywhere.
"""

from trenchchat.core.user_directory import UserDirectory
from trenchchat.gui.invite_dialogs import member_label

OWN = "11" * 16
PEER = "22" * 16
PREFIX = PEER[:12] + "…"


class _FakeStorage:
    """Only the method resolve_display_name() reaches for."""

    def __init__(self, names: dict[str, str] | None = None):
        self._names = names or {}

    def get_display_name_for_identity(self, identity_hex: str) -> str:
        return self._names.get(identity_hex, "")


def test_stored_member_name_is_used_when_present():
    assert member_label(PEER, "Bob", OWN, _FakeStorage()) == "Bob"


def test_falls_back_to_storage_resolution_when_member_row_is_blank():
    """
    Regression test: MembersDialog read members.display_name and nothing else.
    invite.py hardcodes "" for every member when accepting a member-list
    document, so remote peers always rendered as a bare identity hash.
    """
    storage = _FakeStorage({PEER: "Bob"})
    assert member_label(PEER, "", OWN, storage) == "Bob"


def test_falls_back_to_user_directory_when_nothing_else_knows_the_peer():
    """Covers peers heard via trenchchat.user announces but in no member row --
    exactly the set InviteDialog already displays by name."""
    directory = UserDirectory(OWN)
    directory.record_user(PEER, "Bob")

    assert member_label(PEER, "", OWN, _FakeStorage(),
                        user_directory=directory) == "Bob"


def test_hash_prefix_is_the_last_resort():
    assert member_label(PEER, "", OWN, _FakeStorage()) == PREFIX


def test_unknown_peer_in_directory_still_falls_back_to_prefix():
    directory = UserDirectory(OWN)
    assert member_label(PEER, "", OWN, _FakeStorage(),
                        user_directory=directory) == PREFIX


def test_directory_entry_for_a_different_peer_is_not_borrowed():
    """UserDirectory.search() matches on hash substring as well as name, so the
    result must be checked for an exact identity match before it is used."""
    other = "33" * 16
    directory = UserDirectory(OWN)
    directory.record_user(other, "Someone Else")

    assert member_label(PEER, "", OWN, _FakeStorage(),
                        user_directory=directory) == PREFIX
