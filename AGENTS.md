# AGENTS.md

## Cursor Cloud specific instructions

### Overview

TrenchChat is a decentralized encrypted group chat (Python 3.10+ / PyQt6 / Reticulum / LXMF).
All data is stored under `~/.trenchchat/` (SQLite, identity keypair, config). No external services
or databases are required.

### Running tests

```bash
.venv/bin/python -m pytest tests/ -v
```

All 126 tests must pass. Six tests in `TestChannelPermissionsDialog` require PyQt6 system
libraries (`libegl1`, `libxcb-cursor0`, etc.) — these are pre-installed in the snapshot.

Tests use an in-process `TestTransport` shim and do **not** require a running Reticulum network
or display server (except the Qt dialog tests which need a virtual framebuffer).

### Running the application

```bash
export DISPLAY=:1
.venv/bin/python main.py -v
```

The VM has a virtual display at `:1`. PyQt6 requires several system libraries that are
pre-installed in the snapshot: `libegl1`, `libxcb-cursor0`, `libxkbcommon-x11-0`, `libgl1`,
`libfontconfig1`, `libdbus-1-3`, `libxcb-shape0`, `libxcb-icccm4`, `libxcb-keysyms1`,
`libxcb-render-util0`, `libxcb-image0`.

### Linting

No automated linter is configured in the repo. Code standards are defined in
`.cursor/rules/code-standards.mdc`.

### Gotchas

- The `setup.sh` script prompts interactively for Quad4 node config — do not use it in
  automated contexts. Use `pip install -r requirements.txt` directly instead.
- PyQt6 will crash at import time if `libxcb-cursor0` is missing (`libEGL.so.1` / xcb plugin
  error). Install it via `sudo apt-get install -y libxcb-cursor0`.
