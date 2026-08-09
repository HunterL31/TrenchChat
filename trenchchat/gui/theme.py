"""Central color/style constants for the TrenchChat desktop GUI.

The palette matches the "TrenchChat Main Window" design
(claude.ai/design project 51f83382-9a47-4a5c-a7af-b36c6dd80db2). There is no
shared .qss file — every widget still sets its own inline stylesheet string —
so this module exists purely as the single source of truth for the hex
values referenced from those inline strings, instead of each widget file
duplicating (and inevitably drifting from) the same literals.
"""

from PyQt6.QtGui import QColor


def rgba(hex_color: str, alpha: float) -> str:
    """Return a Qt-stylesheet-compatible rgba() string for hex_color at the given alpha."""
    c = QColor(hex_color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


ACCENT = "#9184d9"

BG = "#161826"
SIDEBAR_BG = "#1b1d29"
PANEL_BG = "#1f2130"
INPUT_BG = "#232532"
DIALOG_BG = "#232532"

TEXT = "#e9e9ed"

BORDER = rgba(TEXT, 0.16)
BORDER_SOFT = rgba(TEXT, 0.10)
BORDER_STRONG = rgba(TEXT, 0.25)
DIVIDER = rgba(TEXT, 0.14)

TEXT_MUTED = rgba(TEXT, 0.6)
TEXT_FAINT = rgba(TEXT, 0.45)
TEXT_SUBTLE = rgba(TEXT, 0.32)
MESSAGE_TEXT = "#dcddde"

INVITE_BG = "#2b2741"
INVITE_BORDER = "#423a6a"
INVITE_TEXT = "#d2cefd"

ONLINE_DOT = "#5fbf8a"
AWAY_DOT = "#d3a35a"
OFFLINE_DOT = "#666666"

DANGER_BG = "#5a2020"
DANGER_HOVER = "#7a2d2d"

ACCENT_WASH_SELECTED = rgba(ACCENT, 0.16)
ACCENT_WASH_HOVER = rgba(ACCENT, 0.10)
ACCENT_WASH_REACTED = rgba(ACCENT, 0.22)
ACCENT_WASH_REACTED_HOVER = rgba(ACCENT, 0.30)

FONT_FAMILY = "'Inter','Segoe UI',sans-serif"
MONO_FONT_FAMILY = "'Cascadia Mono','Consolas',monospace"
