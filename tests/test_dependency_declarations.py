"""
The shipped app's dependency lists must cover what main_flutter.py imports.

Regression guard for a real failure: fastapi, uvicorn and websockets were
declared only in devtools/testenv/requirements.txt, labelled "dev-only".
main_flutter.py serves that same API, so a from-source Windows install --
setup.bat installed requirements.txt only -- produced an app whose event
socket could never come up, and every live update silently stopped.
"""

import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

# What main_flutter.py needs to serve the client, by distribution name.
SERVER_STACK = ("fastapi", "uvicorn", "websockets")


def _requirement_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name = line.split("==")[0].split(">=")[0].split("[")[0].strip()
        names.add(name.lower())
    return names


@pytest.mark.parametrize("package", SERVER_STACK)
def test_requirements_declares_the_server_stack(package):
    assert package in _requirement_names(_ROOT / "requirements.txt")


@pytest.mark.parametrize("package", SERVER_STACK)
def test_pyproject_declares_the_server_stack(package):
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    declared = {
        dep.split(">=")[0].split("==")[0].split("[")[0].strip().lower()
        for dep in data["project"]["dependencies"]
    }
    assert package in declared


def test_windows_setup_installs_the_same_files_as_the_unix_one():
    """setup.bat installed only requirements.txt while setup.sh installed
    both, so the two platforms provisioned different environments."""
    bat = (_ROOT / "setup.bat").read_text()
    sh = (_ROOT / "setup.sh").read_text()
    for req in ("requirements.txt", "testenv"):
        assert req in bat, f"setup.bat does not install {req}"
        assert req in sh, f"setup.sh does not install {req}"


def test_ci_installs_the_test_dependencies():
    """CI installed requirements.txt only, so the API tests -- which skip
    themselves when the test client is missing -- never ran there."""
    workflow = (_ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "pip install -r devtools/testenv/requirements.txt" in workflow


def test_launcher_refuses_to_start_without_a_websocket_library(monkeypatch):
    import importlib.util

    import main_flutter

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(SystemExit) as exc:
        main_flutter._require_websocket_support()
    assert "WebSocket" in str(exc.value)


def test_launcher_starts_when_a_websocket_library_is_present():
    import main_flutter

    assert main_flutter._require_websocket_support() is None
