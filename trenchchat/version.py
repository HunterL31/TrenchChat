"""
The running build's version, and what it means for the profile on disk.

A release's version comes from the git tag CI builds it from; the frozen
bundle carries it in a data file (written by trenchchat.spec) and a source
checkout falls back to pyproject.toml with a "+source" build tag, so the two
are never mistaken for each other.

Every launch compares that version against the one recorded in the profile
and stores the result in ``version.json`` beside it. Nothing else on disk
says which build wrote the profile, so without this an installer that rolls
a user back (or swaps one same-version build for another) looks exactly
like an ordinary restart, and the app opens a database a newer build may
have migrated with no idea that is what it is doing.
"""

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import RNS

from trenchchat.config import DATA_DIR
from trenchchat.core.fileutils import atomic_write_bytes

VERSION_FILE_NAME = "version.json"

# Written into the PyInstaller bundle by trenchchat.spec from APP_VERSION.
BUNDLED_VERSION_FILE = "app_version.txt"

VERSION_ENV_VAR = "TRENCHCHAT_VERSION"

# Marks a version that came from the working tree rather than a release
# build. Build metadata is ignored for precedence, so it never makes a
# checkout look newer or older than the release it was cut from.
SOURCE_BUILD_TAG = "+source"

UNKNOWN_VERSION = "0.0.0"

MAX_HISTORY_ENTRIES = 20

CHANGE_FIRST_RUN = "first_run"
CHANGE_UNKNOWN = "unknown"
CHANGE_SAME = "same"
CHANGE_UPGRADE = "upgrade"
CHANGE_DOWNGRADE = "downgrade"
CHANGE_SIDEGRADE = "sidegrade"

# Files that mean a profile was in use before this launch.
_PROFILE_MARKERS = ("identity", "storage.db", "config.json")

_SEMVER_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$"
)

_PYPROJECT_VERSION_RE = re.compile(r"^version\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)

_cached_version: str | None = None


@dataclass(frozen=True)
class InstallState:
    """What this launch's version is, and how it differs from the last one."""

    version: str
    previous: str | None
    transition: str
    first_seen: float
    changed_at: float
    history: list[dict] = field(default_factory=list)

    @property
    def is_downgrade(self) -> bool:
        return self.transition == CHANGE_DOWNGRADE

    @property
    def is_sidegrade(self) -> bool:
        return self.transition == CHANGE_SIDEGRADE

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "previous": self.previous,
            "transition": self.transition,
            "first_seen": self.first_seen,
            "changed_at": self.changed_at,
            "history": list(self.history),
        }


def parse_version(text: str) -> tuple[int, int, int, tuple, str] | None:
    """Split a semver string into (major, minor, patch, prerelease, build).

    Returns None for anything that is not semver.
    """
    match = _SEMVER_RE.match(text.strip()) if text else None
    if match is None:
        return None
    major, minor, patch, prerelease, build = match.groups()
    parts = tuple(prerelease.split(".")) if prerelease else ()
    return int(major), int(minor), int(patch), parts, build or ""


def _compare_prerelease(a: tuple, b: tuple) -> int:
    """Semver prerelease precedence: no prerelease outranks any prerelease."""
    if not a and not b:
        return 0
    if not a:
        return 1
    if not b:
        return -1
    for left, right in zip(a, b):
        left_num, right_num = left.isdigit(), right.isdigit()
        if left_num and right_num:
            result = (int(left) > int(right)) - (int(left) < int(right))
        elif left_num != right_num:
            result = -1 if left_num else 1
        else:
            result = (left > right) - (left < right)
        if result:
            return result
    return (len(a) > len(b)) - (len(a) < len(b))


def compare_versions(a: str, b: str) -> int | None:
    """Semver precedence of *a* against *b*, or None if either is unparseable.

    Build metadata carries no precedence, so two builds of one version
    compare equal however differently they are labelled.
    """
    left, right = parse_version(a), parse_version(b)
    if left is None or right is None:
        return None
    if left[:3] != right[:3]:
        return 1 if left[:3] > right[:3] else -1
    return _compare_prerelease(left[3], right[3])


def classify_change(previous: str | None, current: str,
                    profile_exists: bool = False) -> str:
    """Name the step from *previous* to *current*.

    An existing profile with no recorded version predates version tracking,
    which is not the same as a first run: the build that wrote it is simply
    unknown.
    """
    if not previous:
        return CHANGE_UNKNOWN if profile_exists else CHANGE_FIRST_RUN
    if previous == current:
        return CHANGE_SAME
    result = compare_versions(current, previous)
    if result is None:
        return CHANGE_UNKNOWN
    if result > 0:
        return CHANGE_UPGRADE
    if result < 0:
        return CHANGE_DOWNGRADE
    return CHANGE_SIDEGRADE


def _bundled_version() -> str | None:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if not bundle_root:
        return None
    try:
        text = (Path(bundle_root) / BUNDLED_VERSION_FILE).read_text().strip()
    except OSError:
        return None
    return text or None


def _source_version() -> str | None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        match = _PYPROJECT_VERSION_RE.search(pyproject.read_text())
    except OSError:
        return None
    if match is None:
        return None
    return f"{match.group(1)}{SOURCE_BUILD_TAG}"


def app_version() -> str:
    """The version of the build that is running.

    Resolved once per process: the TRENCHCHAT_VERSION override, then the
    frozen bundle's stamp, then pyproject.toml for a source checkout.
    """
    global _cached_version
    if _cached_version is None:
        override = os.environ.get(VERSION_ENV_VAR, "").strip()
        _cached_version = (
            override or _bundled_version() or _source_version() or UNKNOWN_VERSION
        )
    return _cached_version


def reset_version_cache() -> None:
    """Forget the resolved version so the next call re-reads its sources."""
    global _cached_version
    _cached_version = None


def _profile_exists(data_dir: Path) -> bool:
    return any((data_dir / name).exists() for name in _PROFILE_MARKERS)


def read_install_record(data_dir: Path | None = None) -> dict | None:
    """The version record stored in a profile, or None if there is none."""
    path = (data_dir or DATA_DIR) / VERSION_FILE_NAME
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        RNS.log(f"TrenchChat [version]: unreadable version record: {e}", RNS.LOG_WARNING)
        return None
    return data if isinstance(data, dict) else None


def record_launch(data_dir: Path | None = None,
                  version: str | None = None) -> InstallState:
    """Compare this build against the profile's record and update it.

    The record is only rewritten when the version actually changed, so
    ``changed_at`` stays the time of the change rather than the last launch.
    A profile that cannot be written is logged and otherwise ignored, a
    version note must never keep the app from starting.
    """
    data_dir = data_dir or DATA_DIR
    current = version or app_version()
    record = read_install_record(data_dir) or {}

    stored = record.get("version")
    previous = stored if isinstance(stored, str) and stored else None
    transition = classify_change(previous, current, _profile_exists(data_dir))

    history = [entry for entry in record.get("history", []) if isinstance(entry, dict)]
    first_seen = record.get("first_seen")
    if not isinstance(first_seen, (int, float)):
        first_seen = time.time()
    changed_at = record.get("changed_at")
    if not isinstance(changed_at, (int, float)):
        changed_at = time.time()

    if transition != CHANGE_SAME:
        changed_at = time.time()
        history = history[-(MAX_HISTORY_ENTRIES - 1):] + [{
            "version": current,
            "previous": previous,
            "transition": transition,
            "at": changed_at,
        }]
        _write_record(data_dir, {
            "version": current,
            "first_seen": first_seen,
            "changed_at": changed_at,
            "history": history,
        })

    state = InstallState(
        version=current,
        previous=previous,
        transition=transition,
        first_seen=first_seen,
        changed_at=changed_at,
        history=history,
    )
    _log_transition(state)
    return state


def _write_record(data_dir: Path, record: dict) -> None:
    try:
        atomic_write_bytes(
            data_dir / VERSION_FILE_NAME,
            json.dumps(record, indent=2).encode("utf-8"),
        )
    except OSError as e:
        RNS.log(f"TrenchChat [version]: could not store version record: {e}",
                RNS.LOG_WARNING)


def _log_transition(state: InstallState) -> None:
    if state.transition == CHANGE_DOWNGRADE:
        RNS.log(
            f"TrenchChat [version]: running {state.version} over a profile last "
            f"used by {state.previous} — this install is a downgrade, and the "
            f"profile may hold data written by the newer build",
            RNS.LOG_WARNING,
        )
    elif state.transition == CHANGE_SIDEGRADE:
        RNS.log(
            f"TrenchChat [version]: running {state.version} over a profile last "
            f"used by {state.previous} — same version, different build",
            RNS.LOG_WARNING,
        )
    elif state.transition == CHANGE_UPGRADE:
        RNS.log(f"TrenchChat [version]: upgraded {state.previous} -> {state.version}",
                RNS.LOG_NOTICE)
    elif state.transition == CHANGE_UNKNOWN and state.previous is None:
        RNS.log(f"TrenchChat [version]: {state.version} on a profile with no "
                f"recorded version", RNS.LOG_NOTICE)
    else:
        RNS.log(f"TrenchChat [version]: {state.version}", RNS.LOG_NOTICE)
