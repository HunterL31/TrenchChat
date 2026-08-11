"""
On-device spike: prove trenchchat.core.identity and trenchchat.core.storage
run unmodified under serious_python. See ../../README.md.

Writes result.json next to the sandboxed data dir; the Flutter side reads it
back and renders pass/fail per step. This file is throwaway — not wired into
the real app.
"""

import json
import os
import traceback
from pathlib import Path

import RNS

_SPIKE_DIR = Path(os.environ.get("TRENCHCHAT_SPIKE_DIR", "/tmp/trenchchat_spike"))
_SPIKE_DIR.mkdir(parents=True, exist_ok=True)

_result = {"steps": []}


def _record(step: str, ok: bool, detail: str = "") -> None:
    _result["steps"].append({"step": step, "ok": ok, "detail": detail})


def _write_result() -> None:
    (_SPIKE_DIR / "result.json").write_text(json.dumps(_result, indent=2))


def main() -> None:
    try:
        RNS.Reticulum(configdir=str(_SPIKE_DIR / "reticulum"))
        _record("RNS.Reticulum init", True)
    except Exception as e:
        _record("RNS.Reticulum init", False, repr(e))
        _write_result()
        raise

    from trenchchat.config import Config
    from trenchchat.core.identity import Identity
    from trenchchat.core.storage import Storage

    identity_path = _SPIKE_DIR / "identity"
    try:
        config = Config(data_dir=_SPIKE_DIR / "config")
        identity_a = Identity(config, identity_path=identity_path)
        hash_a = identity_a.hash_hex
        _record("Identity created", True, hash_a)

        # A second Identity() would construct a second RNS.Destination for the
        # same identity hash + aspect, and RNS.Transport only allows one
        # registration per process (real app code only ever builds Identity
        # once per run, per main.py). To prove the on-disk round trip without
        # hitting that, load the raw keypair back with a bare RNS.Identity
        # instead of a second Identity wrapper.
        raw_identity = RNS.Identity()
        raw_identity.load_private_key(identity_path.read_bytes())
        hash_b = raw_identity.hash.hex()
        _record("Identity reloaded from disk, hash matches", hash_a == hash_b, hash_b)
    except Exception as e:
        _record("Identity round trip", False, repr(e))
        _write_result()
        raise

    try:
        storage = Storage(db_path=_SPIKE_DIR / "storage.db")
        storage.upsert_server(
            hash="spike0000",
            name="Spike Server",
            description="written by the mobile spike",
            creator_hash=hash_a,
            created_at=0.0,
        )
        row = storage.get_server("spike0000")
        readback_ok = row is not None and row["name"] == "Spike Server"
        _record("Storage write + read back", readback_ok,
                dict(row) if row is not None else "no row returned")
        storage.close()
    except Exception as e:
        _record("Storage write + read back", False, repr(e))
        _write_result()
        raise

    _write_result()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
