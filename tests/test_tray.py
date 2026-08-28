"""
The tray icon that keeps the node alive after the window closes.

The real pystray backends need a display and a notification area, neither
of which a test machine has, so the module is exercised against a stand-in.
What matters here is what the launcher depends on: a tray that cannot be
created says so instead of raising, the menu really does reach the
launcher's open and quit, and the icon loop always ends when the work does.
"""

import sys

import pytest

from trenchchat import tray


class FakeMenuItem:
    def __init__(self, text, action, default=False):
        self.text = text
        self.action = action
        self.default = default


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class FakeIcon:
    notify_error = None
    HAS_MENU = True

    def __init__(self, name, image, title, menu):
        self.name = name
        self.image = image
        self.title = title
        self.menu = menu
        self.visible = False
        self.stopped = False
        self.notifications = []

    def run(self, setup=None):
        setup(self)

    def stop(self):
        self.stopped = True

    def notify(self, message, title):
        if self.notify_error is not None:
            raise self.notify_error
        self.notifications.append((message, title))


class FakePystray:
    Icon = FakeIcon
    Menu = FakeMenu
    MenuItem = FakeMenuItem


@pytest.fixture
def fake_pystray(monkeypatch):
    monkeypatch.setitem(sys.modules, "pystray", FakePystray)
    return FakePystray


def _menu_items(icon):
    return [item for item in icon.menu.items if isinstance(item, FakeMenuItem)]


def test_icon_asset_is_shipped_and_loads():
    """The launcher has an icon to show without generating one at runtime."""
    image = tray.load_icon_image()

    assert tray.ICON_PATH.is_file()
    assert image.size[0] == image.size[1] >= 32


def test_no_tray_backend_yields_no_tray(monkeypatch):
    """A headless machine gets None, not an exception at startup."""
    monkeypatch.setitem(sys.modules, "pystray", None)

    assert tray.create_tray(on_open=lambda: None, on_quit=lambda: None) is None


def test_a_missing_icon_yields_no_tray(monkeypatch, tmp_path, fake_pystray):
    monkeypatch.setattr(tray, "ICON_PATH", tmp_path / "gone.png")

    assert tray.create_tray(on_open=lambda: None, on_quit=lambda: None) is None


def test_menu_reaches_open_and_quit(fake_pystray):
    opened, quit_calls = [], []

    background = tray.create_tray(on_open=lambda: opened.append(1),
                                  on_quit=lambda: quit_calls.append(1))
    items = _menu_items(background.icon)
    labels = [item.text for item in items]
    assert labels == [tray.OPEN_LABEL, tray.QUIT_LABEL]

    for item in items:
        item.action()
    assert opened == [1] and quit_calls == [1]


def test_opening_is_the_default_click_action(fake_pystray):
    background = tray.create_tray(on_open=lambda: None, on_quit=lambda: None)

    default = [item for item in _menu_items(background.icon) if item.default]
    assert [item.text for item in default] == [tray.OPEN_LABEL]


def test_run_shows_the_icon_and_stops_when_the_work_ends(fake_pystray):
    background = tray.create_tray(on_open=lambda: None, on_quit=lambda: None)
    seen = []

    background.run(lambda: seen.append(background.icon.visible))

    assert seen == [True]
    assert background.icon.stopped


def test_a_failing_worker_still_stops_the_icon(fake_pystray):
    """Otherwise a crash on the way out leaves an icon nothing is behind."""
    background = tray.create_tray(on_open=lambda: None, on_quit=lambda: None)

    def worker():
        raise RuntimeError("shutdown went wrong")

    with pytest.raises(RuntimeError):
        background.run(worker)

    assert background.icon.stopped


def test_the_user_is_told_where_the_app_went(fake_pystray):
    background = tray.create_tray(on_open=lambda: None, on_quit=lambda: None)

    background.notify(tray.BACKGROUND_NOTICE)

    assert background.icon.notifications == [(tray.BACKGROUND_NOTICE,
                                              tray.DEFAULT_TITLE)]


def test_a_backend_without_notifications_is_not_a_failure(fake_pystray):
    """X11's tray has none, and a desktop may refuse them."""
    background = tray.create_tray(on_open=lambda: None, on_quit=lambda: None)
    background.icon.notify_error = NotImplementedError()

    background.notify(tray.BACKGROUND_NOTICE)


def test_a_backend_with_no_menu_gets_no_tray(monkeypatch, fake_pystray):
    """X11's tray takes a click and shows no menu, so Quit would not exist:
    the window the user just closed was the only way back."""
    monkeypatch.setattr(FakeIcon, "HAS_MENU", False)

    assert tray.create_tray(on_open=lambda: None, on_quit=lambda: None) is None


def test_a_desktop_that_hides_status_icons_gets_no_tray(monkeypatch, fake_pystray):
    """GNOME dropped GtkStatusIcon in 3.26 -- the icon exists and is never
    drawn, which hides the app with no way back to it."""
    monkeypatch.setattr(FakeIcon, "__module__", tray.GTK_BACKEND)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")

    assert tray.create_tray(on_open=lambda: None, on_quit=lambda: None) is None


def test_a_desktop_that_draws_them_gets_one(monkeypatch, fake_pystray):
    monkeypatch.setattr(FakeIcon, "__module__", tray.GTK_BACKEND)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "XFCE")

    assert tray.create_tray(on_open=lambda: None, on_quit=lambda: None) is not None
