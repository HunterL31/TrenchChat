"""
Headless, production-wired TrenchChat backend.

Serves the same FastAPI surface as devtools/testenv/api.py, but wired like
main.py rather than the dev harness: the user's real data dir and Reticulum
config, production presence timeouts, propagation-node sync on startup, and
the interface-online poller and periodic reannounce main.py drives via Qt
timers, here run as daemon threads instead.

This is the embedding target for a headless deployment (and, eventually,
the mobile bridge -- see docs/offline-sync.md and the design plan for
context): trenchchat/gui is never imported from this package.
"""
