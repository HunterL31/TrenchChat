# Flutter + serious_python spike

Throwaway proof-of-concept for the UI direction in
`docs/` (see the "Cross-platform UI direction" decision record). Not part of
the app — this proves one thing: that `trenchchat/core/identity.py` and
`trenchchat/core/storage.py` can run unmodified inside a Flutter app via
`serious_python`, on-device. Nothing here is wired into `trenchchat/`.

## What it does

`python/app/main.py`:

1. Starts a sandboxed `RNS.Reticulum` instance (its own config dir, not
   `~/.reticulum`).
2. Constructs a `trenchchat.core.identity.Identity`, then constructs a second
   one pointed at the same path to prove the keypair round-trips through
   disk.
3. Opens a `trenchchat.core.storage.Storage` (plain SQLite, no SQLCipher —
   see "Known gap" below), writes one row with `upsert_server`, reads it back
   with `get_server`.
4. Writes `result.json` (pass/fail per step) next to the databases.

`lib/main.dart` runs that script via `SeriousPython.run(sync: true)` and
displays `result.json` in a single-screen UI.

## Before building: vendor the core package

`trenchchat/core/` isn't pip-installable, so it has to be copied into the
Python app bundle before packaging. Run one of:

```bash
./prepare.sh          # Linux/macOS
```
```powershell
.\prepare.ps1          # Windows
```

from this directory. It copies exactly the modules `identity.py` and
`storage.py` transitively need — `trenchchat/__init__.py`, `config.py`,
`core/__init__.py`, `core/fileutils.py`, `core/lockbox.py`,
`core/permissions.py`, `core/identity.py`, `core/storage.py` — into
`python/app/trenchchat/`. Re-run it after pulling upstream changes to those
files.

## Building

Requires the Flutter SDK and, for Android, the Android SDK/NDK — neither is
installed in this environment, so this has not been built or run yet. From a
machine that has them:

```bash
flutter pub get
dart run serious_python:main package python/app -p Android \
  -r rns==1.4.2 -r lxmf==1.1.1 -r msgpack==1.1.2 -r cryptography==46.0.5 -r cffi
flutter run -d <android-device-or-emulator>
```

iOS is a separate, harder problem: it needs Xcode, which only runs on macOS.
**This spike cannot be built for iOS from a Windows machine at all** — that's
a platform requirement of iOS development generally, independent of Flutter
or serious_python. Attempt the iOS leg from a Mac.

## Known gap found during research (see decision record, risk #1)

Checked which of `trenchchat/core`'s compiled dependencies have prebuilt
Android/iOS wheels on `pypi.flet.dev` (the index `serious_python`/Flet uses
for binary packages):

| Package | Mobile wheel available? |
|---|---|
| `rns`, `lxmf` | N/A — pure Python, no compiled deps |
| `cryptography` | Yes |
| `cffi` | Yes |
| `msgpack` | Yes |
| `Pillow` | Yes |
| `sqlcipher3` | **No** |

Everything `identity.py` and `storage.py` need in **plain** (no-PIN,
unencrypted-DB) mode is covered. `sqlcipher3` — needed only when a PIN is set
(`Storage(encryption_key=...)`) — has no prebuilt mobile wheel and no known
`python-for-android` / Mobile Forge recipe as of this check. At-rest
encryption on mobile needs its own follow-up (a custom Mobile Forge recipe
that cross-compiles SQLCipher + `sqlcipher3`, or a different at-rest
encryption approach for the mobile build specifically) before the PIN-lock
feature can port. This spike deliberately runs `Storage` unencrypted to stay
inside the currently-covered set.

## Result

The Flutter/Android build itself hasn't run — no Flutter SDK, Android SDK, or
Xcode on this machine. What has been verified: `python/app/main.py`, running
against the vendored `trenchchat/core` copy under plain desktop Python (not
on-device), produces four `"ok": true` steps — Reticulum init, identity
creation, identity reload from disk, and a storage write/read-back all
succeed unmodified. This confirms the script and the core code it drives are
correct; it does not confirm anything about the mobile runtime, packaging, or
`serious_python` bridge itself, which is the part that still needs a machine
with the actual mobile toolchains.

One real constraint surfaced while writing this: `Identity.__init__`
constructs an `RNS.Destination`, and `RNS.Transport` only allows one
`Destination` registration per identity hash + aspect *per process*.
Constructing a second `Identity` wrapper for the same on-disk key in the same
process (as an early version of this script did, to prove the disk round
trip) raises `KeyError: Attempt to register an already registered
destination`. Not a bug — `main.py` only ever builds `Identity` once per run
— but worth knowing if a future bridge/test harness ever wants to
re-instantiate `Identity` mid-process instead of once at startup.
