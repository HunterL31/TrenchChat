"""
The devtools STUN responder, importable from the test suite.

The responder lives with the rest of the test environment because the
namespace harness runs it as a script in the root namespace; a test wants the
same code in-process rather than a second implementation that could disagree
with it. This is the path insert and nothing else.
"""

import sys
from pathlib import Path

_TESTENV_DIR = Path(__file__).resolve().parents[1] / "devtools" / "testenv"
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))

import pytest  # noqa: E402

from trenchchat.network.ip import candidates  # noqa: E402

from stun_responder import StunResponder, answer  # noqa: E402,F401


def routable_host() -> str:
    """An address of this host a peer elsewhere could plausibly dial.

    Loopback will not do for an echo: an address only this machine can reach
    is not one a node keeps, and that filter is the same one a real echo's
    answer goes through. A host with nothing routable skips the test rather
    than asserting about an address that was never offered.
    """
    for address in candidates.local_addresses():
        if candidates.family_of(address) == 4:
            return address
    pytest.skip("this host has no routable IPv4 address to echo")
