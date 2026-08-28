"""Handing a second launch over to the node already running in the tray.

Closing the window leaves the node running without one, so the next
double-click would start a second process over the same profile: two
backends announcing one identity and writing one database. The running
launcher records where its API is; a launcher that finds a live record asks
that instance to open a window, and exits instead of starting anything.

The record carries the API token, which is how the running instance knows
the caller is this user. It sits in ~/.trenchchat beside the identity and
the message database, owner-readable only -- anyone who can read it can
already read those.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

import RNS

from trenchchat.config import DATA_DIR
from trenchchat.core.fileutils import atomic_write_bytes

RECORD_NAME = "launcher.json"
OPEN_UI_PATH = "/ui/open"
# One of the three names api.py's token middleware accepts.
TOKEN_HEADER = "X-TC-Token"
HANDOFF_TIMEOUT_SECS = 3.0


def record_path(data_dir: Path | None = None) -> Path:
    """Where the running launcher advertises its API."""
    return (data_dir or DATA_DIR) / RECORD_NAME


def publish(url: str, token: str, data_dir: Path | None = None) -> None:
    """Advertise this launcher's API to the next launch."""
    atomic_write_bytes(record_path(data_dir),
                       json.dumps({"url": url, "token": token}).encode())


def clear(data_dir: Path | None = None) -> None:
    """Withdraw the advertisement on the way out."""
    try:
        record_path(data_dir).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        RNS.log(f"TrenchChat [launcher]: could not clear the instance record: {e}",
                RNS.LOG_WARNING)


def hand_off(data_dir: Path | None = None) -> bool:
    """Ask an already-running instance to open its window.

    False means there is none to ask: no record, a stale one, or an instance
    that no longer answers. The caller starts normally then.
    """
    try:
        record = json.loads(record_path(data_dir).read_text())
        url, token = record["url"], record["token"]
    except (OSError, ValueError, KeyError, TypeError):
        return False

    request = urllib.request.Request(f"{url}{OPEN_UI_PATH}", data=b"",
                                     headers={TOKEN_HEADER: token})
    # An http_proxy in the environment would otherwise send a loopback
    # request out to the proxy, which cannot reach this machine's API.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=HANDOFF_TIMEOUT_SECS) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError) as e:
        RNS.log(f"TrenchChat [launcher]: nothing answered at {url} ({e})",
                RNS.LOG_NOTICE)
        return False
