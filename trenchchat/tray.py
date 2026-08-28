"""System-tray presence, so closing the window does not close the node.

Discovery only runs while the process does: a client that exits stops
hearing announces, stops answering its channels' sync requests and goes
offline to its peers. Closing the client window therefore drops the UI and
leaves the node running behind a tray icon that reopens or quits it.

pystray picks a platform backend when it is imported and raises when the
machine has none -- no display, no notification area, a headless server.
create_tray() returns None there, and the launcher falls back to quitting
with the window.
"""

import os
from pathlib import Path
from typing import Callable

import RNS

# GNOME dropped XEmbed tray icons in 3.26. pystray's X11 backend still
# creates one and nothing ever shows it, which would leave the window as the
# only way back to an app that just hid itself -- worse than not having a
# tray at all.
X11_BACKEND = "pystray._xorg"
DESKTOPS_WITHOUT_A_TRAY = ("GNOME",)

ICON_PATH = Path(__file__).resolve().parent / "assets" / "tray.png"

DEFAULT_TITLE = "TrenchChat"
OPEN_LABEL = "Open TrenchChat"
QUIT_LABEL = "Quit TrenchChat"
BACKGROUND_NOTICE = ("Still running in the background -- announces, discovery "
                     "and messages carry on. Quit from the tray icon.")


def _icon_would_be_invisible(icon_class) -> bool:
    """Whether this desktop would swallow the icon rather than show it."""
    if icon_class.__module__ != X11_BACKEND:
        return False
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    return any(name in desktop for name in DESKTOPS_WITHOUT_A_TRAY)


def load_icon_image():
    """The tray icon as a PIL image."""
    from PIL import Image

    with Image.open(ICON_PATH) as img:
        return img.convert("RGBA")


class BackgroundTray:
    """A tray icon that outlives the client window."""

    def __init__(self, icon):
        self.icon = icon

    def run(self, worker: Callable[[], None]) -> None:
        """Show the icon, run worker on another thread, return when it does.

        macOS's tray backend only works from the main thread, so the icon
        loop takes it and the launcher's wait loop becomes the worker.
        """
        def _setup(icon):
            icon.visible = True
            try:
                worker()
            finally:
                icon.stop()

        self.icon.run(setup=_setup)

    def notify(self, message: str) -> None:
        """Say where the app went when its window closes.

        The X11 tray backend has no notifications, and a desktop can refuse
        them, so this is a courtesy and never a failure.
        """
        try:
            self.icon.notify(message, DEFAULT_TITLE)
        except Exception as e:
            RNS.log(f"TrenchChat [tray]: no notification shown ({e})",
                    RNS.LOG_DEBUG)


def create_tray(*, on_open: Callable[[], None], on_quit: Callable[[], None],
                title: str = DEFAULT_TITLE) -> BackgroundTray | None:
    """A tray icon for the running node, or None if this machine has no tray."""
    try:
        import pystray

        image = load_icon_image()
    except Exception as e:
        RNS.log(f"TrenchChat [tray]: no tray icon on this machine ({e})",
                RNS.LOG_NOTICE)
        return None

    if _icon_would_be_invisible(pystray.Icon):
        RNS.log("TrenchChat [tray]: this desktop shows no tray icons; "
                "closing the window will quit", RNS.LOG_NOTICE)
        return None

    menu = pystray.Menu(
        pystray.MenuItem(OPEN_LABEL, lambda: on_open(), default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(QUIT_LABEL, lambda: on_quit()),
    )
    return BackgroundTray(pystray.Icon("trenchchat", image, title, menu))
